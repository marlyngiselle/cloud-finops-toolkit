from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from conftest import FakeCompute, resource
from finops.resources import Row
from finops.schedule import (
    TAG_DAYS,
    TAG_SKIP_UNTIL,
    TAG_TIME,
    TAG_TZ,
    ScheduleError,
    ShutdownSchedule,
    ShutdownStatus,
    apply_tag_changes,
    execute_shutdowns,
    format_days,
    parse_days,
    parse_time,
    plan_apply,
    plan_remove,
    plan_shutdowns,
    select_vms,
)

WINDOW = timedelta(minutes=30)
PARIS_19 = ShutdownSchedule(at=time(19, 0), timezone="Europe/Paris")


class TestParsing:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("mon-fri", {0, 1, 2, 3, 4}),
            ("Mon-Fri", {0, 1, 2, 3, 4}),
            ("sat,sun", {5, 6}),
            ("fri-mon", {4, 5, 6, 0}),
            ("daily", set(range(7))),
            ("weekdays", {0, 1, 2, 3, 4}),
            ("weekends", {5, 6}),
            ("monday, wednesday", {0, 2}),
        ],
    )
    def test_parse_days(self, spec: str, expected: set[int]) -> None:
        assert parse_days(spec) == frozenset(expected)

    @pytest.mark.parametrize("spec", ["", "funday", "mon-xyz", ","])
    def test_parse_days_invalid(self, spec: str) -> None:
        with pytest.raises(ScheduleError):
            parse_days(spec)

    @pytest.mark.parametrize(
        ("days", "text"),
        [
            ({0, 1, 2, 3, 4}, "mon-fri"),
            (set(range(7)), "daily"),
            ({5, 6}, "sat,sun"),
            ({0, 2, 4}, "mon,wed,fri"),
        ],
    )
    def test_format_days_round_trips(self, days: set[int], text: str) -> None:
        assert format_days(frozenset(days)) == text
        assert parse_days(text) == frozenset(days)

    def test_parse_time(self) -> None:
        assert parse_time("7:05") == time(7, 5)
        assert parse_time(" 23:59 ") == time(23, 59)
        for bad in ("24:00", "7pm", "19", "19:60"):
            with pytest.raises(ScheduleError):
                parse_time(bad)


class TestScheduleFromTags:
    def test_defaults(self) -> None:
        schedule = ShutdownSchedule.from_tags({TAG_TIME: "19:00"})
        assert schedule == ShutdownSchedule(at=time(19, 0))
        assert schedule.describe() == "19:00 UTC mon-fri"

    def test_full_and_case_insensitive_keys(self) -> None:
        tags = {
            "FinOps-Shutdown-Time": "20:30",
            TAG_TZ: "America/New_York",
            TAG_DAYS: "daily",
            TAG_SKIP_UNTIL: "2026-12-24",
        }
        schedule = ShutdownSchedule.from_tags(tags)
        assert schedule is not None
        assert schedule.skip_until == date(2026, 12, 24)
        assert schedule.to_tags() == {
            TAG_TIME: "20:30",
            TAG_TZ: "America/New_York",
            TAG_DAYS: "daily",
            TAG_SKIP_UNTIL: "2026-12-24",
        }

    def test_no_schedule(self) -> None:
        assert ShutdownSchedule.from_tags({}) is None
        assert ShutdownSchedule.from_tags({TAG_TIME: " "}) is None

    @pytest.mark.parametrize(
        "tags",
        [
            {TAG_TIME: "19:00", TAG_TZ: "Mars/Olympus"},
            {TAG_TIME: "19:00", TAG_SKIP_UNTIL: "next week"},
            {TAG_TIME: "7pm"},
        ],
    )
    def test_invalid(self, tags: dict[str, str]) -> None:
        with pytest.raises(ScheduleError):
            ShutdownSchedule.from_tags(tags)

    def test_needs_days(self) -> None:
        with pytest.raises(ScheduleError):
            ShutdownSchedule(at=time(1, 0), days=frozenset())


class TestIsDue:
    # 2026-10-27 is a Tuesday. Paris is UTC+1 after the DST change on 2026-10-25.
    @pytest.mark.parametrize(
        ("utc", "due"),
        [
            ("2026-10-27T17:59:00", False),
            ("2026-10-27T18:00:00", True),
            ("2026-10-27T18:29:00", True),
            ("2026-10-27T18:30:00", False),
            ("2026-10-31T18:10:00", False),  # Saturday
        ],
    )
    def test_window(self, utc: str, due: bool) -> None:
        now = datetime.fromisoformat(utc).replace(tzinfo=UTC)
        assert PARIS_19.is_due(now, WINDOW) is due

    def test_dst_summer_offset(self) -> None:
        # In July Paris is UTC+2, so 19:00 local is 17:00 UTC.
        assert PARIS_19.is_due(datetime(2026, 7, 7, 17, 5, tzinfo=UTC), WINDOW)
        assert not PARIS_19.is_due(datetime(2026, 7, 7, 18, 5, tzinfo=UTC), WINDOW)

    def test_window_crossing_midnight_uses_previous_day(self) -> None:
        late = ShutdownSchedule(at=time(23, 50), days=frozenset({0}))  # Monday, UTC
        assert late.is_due(datetime(2026, 10, 27, 0, 10, tzinfo=UTC), WINDOW)  # Tue 00:10

    def test_skip_until_is_inclusive(self) -> None:
        schedule = ShutdownSchedule(at=time(18, 0), skip_until=date(2026, 10, 27))
        assert not schedule.is_due(datetime(2026, 10, 27, 18, 5, tzinfo=UTC), WINDOW)
        assert schedule.is_due(datetime(2026, 10, 28, 18, 5, tzinfo=UTC), WINDOW)

    def test_requires_aware_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            PARIS_19.is_due(datetime(2026, 1, 1), WINDOW)


class TestTagChanges:
    def test_apply_merges_and_replaces_old_schedule_tags(self) -> None:
        vm = resource("vm-1", tags={"owner": "a", "FINOPS-SHUTDOWN-TIME": "08:00"})
        [change] = plan_apply([vm], PARIS_19)
        assert change.after == {
            "owner": "a",
            TAG_TIME: "19:00",
            TAG_TZ: "Europe/Paris",
            TAG_DAYS: "mon-fri",
        }
        assert change.changed
        assert "-FINOPS-SHUTDOWN-TIME" in change.diff()

    def test_apply_keeps_an_owner_opt_out(self) -> None:
        vm = resource("vm-1", tags={TAG_SKIP_UNTIL: "2026-12-01"})
        [change] = plan_apply([vm], PARIS_19)
        assert change.after[TAG_SKIP_UNTIL] == "2026-12-01"
        override = ShutdownSchedule(at=time(19, 0), skip_until=date(2027, 1, 1))
        [change] = plan_apply([vm], override)
        assert change.after[TAG_SKIP_UNTIL] == "2027-01-01"

    def test_remove_keeps_unrelated_tags(self) -> None:
        vm = resource("vm-1", tags={"owner": "a", TAG_TIME: "19:00"})
        [change] = plan_remove([vm])
        assert change.after == {"owner": "a"}

    def test_dry_run_calls_nothing(self, compute: FakeCompute) -> None:
        changes = plan_apply([resource("vm-1")], PARIS_19)
        [result] = apply_tag_changes(changes, compute)
        assert result.outcome == "dry-run"
        assert compute.tag_calls == []

    def test_apply_unchanged_and_failures(self, compute: FakeCompute) -> None:
        already = resource("vm-ok", tags=PARIS_19.to_tags())
        fresh = resource("vm-new")
        broken = resource("vm-broken")
        compute.fail_ids = {broken["id"]}
        results = apply_tag_changes(
            plan_apply([already, fresh, broken], PARIS_19), compute, dry_run=False
        )
        assert [(r.name, r.outcome) for r in results] == [
            ("vm-ok", "unchanged"),
            ("vm-new", "done"),
            ("vm-broken", "failed"),
        ]
        assert results[0].detail == "no change"
        assert "simulated API failure" in results[2].detail
        assert [c[0] for c in compute.tag_calls] == [fresh["id"]]


class TestShutdowns:
    now = datetime(2026, 10, 27, 18, 10, tzinfo=UTC)  # Tuesday 19:10 in Paris

    def _vm(self, name: str, power: str = "PowerState/running", **tags: str) -> Row:
        return resource(name, powerState=power, tags=tags)

    def test_plan(self) -> None:
        sched = PARIS_19.to_tags()
        vms = [
            self._vm("vm-due", **sched),
            self._vm("vm-stopped", "PowerState/stopped", **sched),
            self._vm("vm-off", "PowerState/deallocated", **sched),
            self._vm("vm-later", **{TAG_TIME: "22:00", TAG_TZ: "Europe/Paris"}),
            self._vm("vm-skip", **sched, **{TAG_SKIP_UNTIL: "2026-10-30"}),
            self._vm("vm-bad", **{TAG_TIME: "7pm"}),
            self._vm("vm-none"),
            self._vm("vm-ignored", **sched, **{"finops-ignore": "true"}),
        ]
        decisions = {d.name: d for d in plan_shutdowns(vms, self.now, window=WINDOW)}
        assert {n: d.status for n, d in decisions.items()} == {
            "vm-due": ShutdownStatus.DUE,
            "vm-stopped": ShutdownStatus.DUE,
            "vm-off": ShutdownStatus.ALREADY_DEALLOCATED,
            "vm-later": ShutdownStatus.NOT_DUE,
            "vm-skip": ShutdownStatus.SKIPPED,
            "vm-bad": ShutdownStatus.INVALID,
        }
        assert decisions["vm-due"].power_state == "running"
        assert decisions["vm-bad"].schedule == "?"

    def test_execute(self, compute: FakeCompute) -> None:
        sched = PARIS_19.to_tags()
        due = self._vm("vm-a", **sched)
        fails = self._vm("vm-b", **sched)
        later = self._vm("vm-c", **{TAG_TIME: "22:00", TAG_TZ: "Europe/Paris"})
        compute.fail_ids = {fails["id"]}
        decisions = plan_shutdowns([due, fails, later], self.now)

        dry = execute_shutdowns(decisions, compute)
        assert [r.outcome for r in dry] == ["dry-run", "dry-run", "not-due"]
        assert compute.deallocated == []

        real = execute_shutdowns(decisions, compute, dry_run=False)
        assert [(r.name, r.action, r.outcome) for r in real] == [
            ("vm-a", "deallocate", "done"),
            ("vm-b", "deallocate", "failed"),
            ("vm-c", "none", "not-due"),
        ]
        assert compute.deallocated == [due["id"]]
        assert real[2].detail == "22:00 Europe/Paris mon-fri"


def test_select_vms() -> None:
    vms = [
        resource("vm-a", rg="rg-dev", tags={"env": "dev"}),
        resource("vm-b", rg="rg-dev", tags={"Env": "Prod"}),
        resource("vm-c", rg="rg-prod", tags={"env": "dev"}),
    ]
    assert [v["name"] for v in select_vms(vms, resource_group="RG-DEV")] == ["vm-a", "vm-b"]
    assert [v["name"] for v in select_vms(vms, names=["VM-C"])] == ["vm-c"]
    assert [v["name"] for v in select_vms(vms, tag_filters={"env": "dev"})] == ["vm-a", "vm-c"]
    assert [v["name"] for v in select_vms(vms, tag_filters={"env": "prod"})] == ["vm-b"]
    assert len(select_vms(vms)) == 3
