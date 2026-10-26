"""``finops schedule``: tag-driven auto-shutdown for non-production VMs.

The convention is four tags on the VM, readable by anyone in the portal:

=============================  ==========================  =======================
Tag                            Example                     Default when absent
=============================  ==========================  =======================
``finops-shutdown-time``       ``19:00``                   (required, enables it)
``finops-shutdown-tz``         ``Europe/Paris``            ``UTC``
``finops-shutdown-days``       ``mon-fri``                 ``mon-fri``
``finops-shutdown-skip-until`` ``2026-11-15``              none
=============================  ==========================  =======================

A handler (:mod:`finops.handler`) runs on a timer, every 15 minutes for
example, and deallocates VMs whose local shutdown time falls inside the current
window. Owners opt out for a crunch week with ``skip-until`` instead of asking
someone to edit the automation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finops.providers.base import ComputeProvider
from finops.resources import IGNORE_TAG, Row, get_tag, is_ignored, tags_of, text

TAG_TIME = "finops-shutdown-time"
TAG_TZ = "finops-shutdown-tz"
TAG_DAYS = "finops-shutdown-days"
TAG_SKIP_UNTIL = "finops-shutdown-skip-until"
SCHEDULE_TAGS = (TAG_TIME, TAG_TZ, TAG_DAYS, TAG_SKIP_UNTIL)

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = frozenset(range(5))
ALL_DAYS = frozenset(range(7))

_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ScheduleError(ValueError):
    """A schedule tag has an invalid value."""


def _day_index(token: str) -> int:
    key = token.strip().lower()[:3]
    if key not in DAY_NAMES:
        raise ScheduleError(f"unknown day: {token!r}")
    return DAY_NAMES.index(key)


def parse_days(spec: str) -> frozenset[int]:
    """Parse ``mon-fri``, ``sat,sun``, ``fri-mon`` (wraps), ``daily`` or ``weekdays``."""
    value = spec.strip().lower()
    if value in {"daily", "all", "*"}:
        return ALL_DAYS
    if value == "weekdays":
        return WEEKDAYS
    if value == "weekends":
        return frozenset({5, 6})
    days: set[int] = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = _day_index(start_text), _day_index(end_text)
            span = (end - start) % 7
            days.update((start + offset) % 7 for offset in range(span + 1))
        else:
            days.add(_day_index(part))
    if not days:
        raise ScheduleError(f"no days in {spec!r}")
    return frozenset(days)


def format_days(days: frozenset[int]) -> str:
    if days == ALL_DAYS:
        return "daily"
    ordered = sorted(days)
    if len(ordered) > 2 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{DAY_NAMES[ordered[0]]}-{DAY_NAMES[ordered[-1]]}"
    return ",".join(DAY_NAMES[d] for d in ordered)


def parse_time(value: str) -> time:
    match = _TIME_PATTERN.match(value.strip())
    if not match:
        raise ScheduleError(f"time must be HH:MM (24h), got {value!r}")
    return time(int(match[1]), int(match[2]))


@dataclass(frozen=True)
class ShutdownSchedule:
    at: time
    timezone: str = "UTC"
    days: frozenset[int] = WEEKDAYS
    skip_until: date | None = None

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ScheduleError(f"unknown time zone: {self.timezone!r}") from exc
        if not self.days:
            raise ScheduleError("schedule needs at least one day")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_tags(cls, tags: Mapping[str, str]) -> ShutdownSchedule | None:
        """Build a schedule from VM tags; ``None`` when the VM has no shutdown time tag."""
        at = get_tag(tags, TAG_TIME)
        if at is None or not at.strip():
            return None
        skip_text = get_tag(tags, TAG_SKIP_UNTIL)
        skip_until: date | None = None
        if skip_text and skip_text.strip():
            try:
                skip_until = date.fromisoformat(skip_text.strip())
            except ValueError as exc:
                raise ScheduleError(f"{TAG_SKIP_UNTIL} must be YYYY-MM-DD") from exc
        return cls(
            at=parse_time(at),
            timezone=(get_tag(tags, TAG_TZ) or "UTC").strip() or "UTC",
            days=parse_days(get_tag(tags, TAG_DAYS) or "mon-fri"),
            skip_until=skip_until,
        )

    def to_tags(self) -> dict[str, str]:
        tags = {
            TAG_TIME: self.at.strftime("%H:%M"),
            TAG_TZ: self.timezone,
            TAG_DAYS: format_days(self.days),
        }
        if self.skip_until:
            tags[TAG_SKIP_UNTIL] = self.skip_until.isoformat()
        return tags

    def describe(self) -> str:
        return f"{self.at.strftime('%H:%M')} {self.timezone} {format_days(self.days)}"

    def is_skipped(self, now: datetime) -> bool:
        return self.skip_until is not None and now.astimezone(self.tz).date() <= self.skip_until

    def is_due(self, now: datetime, window: timedelta) -> bool:
        """True when ``now`` falls in ``[shutdown time, shutdown time + window)`` locally.

        Yesterday's occurrence is checked too, so a 23:50 shutdown with a
        30-minute window still fires at 00:10. Comparisons are made in UTC so
        daylight-saving transitions behave.
        """
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if self.is_skipped(now):
            return False
        tz = self.tz
        local_today = now.astimezone(tz).date()
        now_utc = now.astimezone(UTC)
        for offset in (0, 1):
            day = local_today - timedelta(days=offset)
            if day.weekday() not in self.days:
                continue
            start = datetime.combine(day, self.at, tzinfo=tz).astimezone(UTC)
            if start <= now_utc < start + window:
                return True
        return False


def _merge_tags(current: Mapping[str, str], updates: Mapping[str, str]) -> dict[str, str]:
    managed = {t.lower() for t in SCHEDULE_TAGS}
    kept = {k: v for k, v in current.items() if k.lower() not in managed}
    return {**kept, **updates}


@dataclass(frozen=True)
class TagChange:
    resource_id: str
    name: str
    resource_group: str
    before: dict[str, str]
    after: dict[str, str]

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        added = {k: v for k, v in self.after.items() if self.before.get(k) != v}
        removed = sorted(set(self.before) - set(self.after))
        parts = [f"+{k}={v}" for k, v in sorted(added.items())]
        parts += [f"-{k}" for k in removed]
        return " ".join(parts) or "no change"


def plan_apply(vms: Sequence[Row], schedule: ShutdownSchedule) -> list[TagChange]:
    """Plan the tags for ``schedule`` on each VM.

    An owner's existing ``skip-until`` is kept unless the new schedule sets
    one, so re-applying a schedule never cancels someone's opt-out.
    """
    changes: list[TagChange] = []
    for vm in vms:
        current = tags_of(vm)
        updates = schedule.to_tags()
        existing_skip = get_tag(current, TAG_SKIP_UNTIL)
        if TAG_SKIP_UNTIL not in updates and existing_skip:
            updates[TAG_SKIP_UNTIL] = existing_skip
        changes.append(_change(vm, _merge_tags(current, updates)))
    return changes


def plan_remove(vms: Sequence[Row]) -> list[TagChange]:
    return [_change(vm, _merge_tags(tags_of(vm), {})) for vm in vms]


def _change(vm: Row, after: dict[str, str]) -> TagChange:
    return TagChange(
        resource_id=text(vm, "id"),
        name=text(vm, "name"),
        resource_group=text(vm, "resourceGroup"),
        before=tags_of(vm),
        after=after,
    )


@dataclass(frozen=True)
class ActionResult:
    name: str
    resource_group: str
    action: str
    outcome: str
    detail: str
    resource_id: str

    def to_row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "resource_group": self.resource_group,
            "action": self.action,
            "outcome": self.outcome,
            "detail": self.detail,
            "resource_id": self.resource_id,
        }


def _run(action: Callable[[], None], dry_run: bool) -> tuple[str, str]:
    if dry_run:
        return "dry-run", ""
    try:
        action()
    except Exception as exc:  # one VM failing must not stop the others
        return "failed", f"{type(exc).__name__}: {exc}"
    return "done", ""


def apply_tag_changes(
    changes: Sequence[TagChange], compute: ComputeProvider, *, dry_run: bool = True
) -> list[ActionResult]:
    results: list[ActionResult] = []
    for change in changes:
        if not change.changed:
            outcome, error = "unchanged", ""
        else:
            outcome, error = _run(
                partial(compute.set_vm_tags, change.resource_id, change.after), dry_run
            )
        results.append(
            ActionResult(
                name=change.name,
                resource_group=change.resource_group,
                action="tag",
                outcome=outcome,
                detail=error or change.diff(),
                resource_id=change.resource_id,
            )
        )
    return results


class ShutdownStatus(StrEnum):
    DUE = "due"
    NOT_DUE = "not-due"
    SKIPPED = "skipped"
    ALREADY_DEALLOCATED = "already-deallocated"
    INVALID = "invalid-schedule"


@dataclass(frozen=True)
class ShutdownDecision:
    name: str
    resource_group: str
    power_state: str
    schedule: str
    status: ShutdownStatus
    message: str
    resource_id: str


_INACTIVE_STATES = {"powerstate/deallocated", "powerstate/deallocating"}


def plan_shutdowns(
    vms: Sequence[Row],
    now: datetime,
    *,
    window: timedelta = timedelta(minutes=30),
    ignore_tag: str = IGNORE_TAG,
) -> list[ShutdownDecision]:
    """Decide, for every VM carrying a shutdown schedule, whether it is due now."""
    decisions: list[ShutdownDecision] = []
    for vm in vms:
        if is_ignored(vm, ignore_tag):
            continue
        power = text(vm, "powerState")
        common = {
            "name": text(vm, "name"),
            "resource_group": text(vm, "resourceGroup"),
            "power_state": power.removeprefix("PowerState/"),
            "resource_id": text(vm, "id"),
        }
        try:
            schedule = ShutdownSchedule.from_tags(tags_of(vm))
        except ScheduleError as exc:
            decisions.append(
                ShutdownDecision(
                    schedule="?", status=ShutdownStatus.INVALID, message=str(exc), **common
                )
            )
            continue
        if schedule is None:
            continue
        if power.lower() in _INACTIVE_STATES:
            status, message = ShutdownStatus.ALREADY_DEALLOCATED, ""
        elif schedule.is_skipped(now):
            status, message = ShutdownStatus.SKIPPED, f"skip-until {schedule.skip_until}"
        elif schedule.is_due(now, window):
            status, message = ShutdownStatus.DUE, ""
        else:
            status, message = ShutdownStatus.NOT_DUE, ""
        decisions.append(
            ShutdownDecision(schedule=schedule.describe(), status=status, message=message, **common)
        )
    return sorted(decisions, key=lambda d: (d.status != ShutdownStatus.DUE, d.name.lower()))


def execute_shutdowns(
    decisions: Sequence[ShutdownDecision], compute: ComputeProvider, *, dry_run: bool = True
) -> list[ActionResult]:
    results: list[ActionResult] = []
    for decision in decisions:
        if decision.status is ShutdownStatus.DUE:
            outcome, detail = _run(partial(compute.deallocate_vm, decision.resource_id), dry_run)
            action = "deallocate"
        else:
            outcome, detail, action = str(decision.status), decision.message, "none"
        results.append(
            ActionResult(
                name=decision.name,
                resource_group=decision.resource_group,
                action=action,
                outcome=outcome,
                detail=detail or decision.schedule,
                resource_id=decision.resource_id,
            )
        )
    return results


def select_vms(
    vms: Sequence[Row],
    *,
    resource_group: str | None = None,
    names: Sequence[str] = (),
    tag_filters: Mapping[str, str] | None = None,
) -> list[Row]:
    """Filter VMs by resource group, names and ``key=value`` tags (all case-insensitive)."""
    wanted_names = {n.lower() for n in names}
    selected: list[Row] = []
    for vm in vms:
        if resource_group and text(vm, "resourceGroup").lower() != resource_group.lower():
            continue
        if wanted_names and text(vm, "name").lower() not in wanted_names:
            continue
        if tag_filters:
            tags = tags_of(vm)
            if any((get_tag(tags, k) or "").lower() != v.lower() for k, v in tag_filters.items()):
                continue
        selected.append(vm)
    return selected
