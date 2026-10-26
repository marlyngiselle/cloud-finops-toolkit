"""``finops idle``: resources that cost money (or block clean-up) while doing nothing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from finops.providers.base import ResourceGraphProvider
from finops.queries import load_query
from finops.resources import IGNORE_TAG, Row, age_in_days, is_ignored, text


@dataclass(frozen=True)
class IdleCheck:
    category: str
    query_name: str
    recommendation: str
    describe: Callable[[Row], str]
    age_field: str | None = None


def _describe_disk(row: Row) -> str:
    size = row.get("diskSizeGB")
    size_text = f"{size} GiB" if size is not None else "unknown size"
    return f"{size_text} {text(row, 'skuName', 'unknown SKU')}"


def _describe_public_ip(row: Row) -> str:
    address = text(row, "ipAddress") or "no address"
    return f"{text(row, 'skuName', 'unknown')} SKU, {text(row, 'allocationMethod', '?')}, {address}"


def _describe_nic(row: Row) -> str:
    suffix = ", holds a public IP" if row.get("hasPublicIp") else ""
    return f"not attached, private IP {text(row, 'privateIp', 'none')}{suffix}"


def _describe_plan(row: Row) -> str:
    workers = row.get("numberOfWorkers") or 1
    return f"{text(row, 'skuName')} ({text(row, 'skuTier')}), {workers} instance(s), 0 apps"


def _describe_stopped_vm(row: Row) -> str:
    return f"{text(row, 'vmSize', 'unknown size')} stopped but still allocated"


CHECKS: tuple[IdleCheck, ...] = (
    IdleCheck(
        category="unattached-disk",
        query_name="idle/unattached-disks",
        recommendation="Snapshot if the data matters, then delete the disk.",
        describe=_describe_disk,
        age_field="lastOwnershipUpdateTime",
    ),
    IdleCheck(
        category="unassociated-public-ip",
        query_name="idle/unassociated-public-ips",
        recommendation="Check DNS records and allow-lists for the address, then delete.",
        describe=_describe_public_ip,
    ),
    IdleCheck(
        category="orphaned-nic",
        query_name="idle/orphaned-nics",
        recommendation="Delete; releases its IPs and unblocks subnet clean-up.",
        describe=_describe_nic,
    ),
    IdleCheck(
        category="empty-app-service-plan",
        query_name="idle/empty-app-service-plans",
        recommendation="Delete the plan, or consolidate apps onto it from other plans.",
        describe=_describe_plan,
    ),
    IdleCheck(
        category="stopped-vm",
        query_name="idle/stopped-vms",
        recommendation="Deallocate (stopped VMs are billed for compute) or delete.",
        describe=_describe_stopped_vm,
    ),
)

CATEGORIES: tuple[str, ...] = tuple(c.category for c in CHECKS)


@dataclass(frozen=True)
class IdleFinding:
    category: str
    name: str
    resource_group: str
    subscription_id: str
    location: str
    detail: str
    age_days: int | None
    recommendation: str
    resource_id: str

    def to_row(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "name": self.name,
            "resource_group": self.resource_group,
            "location": self.location,
            "detail": self.detail,
            "age_days": "" if self.age_days is None else self.age_days,
            "recommendation": self.recommendation,
            "subscription_id": self.subscription_id,
            "resource_id": self.resource_id,
        }


def _select_checks(categories: Iterable[str] | None) -> list[IdleCheck]:
    if not categories:
        return list(CHECKS)
    wanted = set(categories)
    unknown = wanted.difference(CATEGORIES)
    if unknown:
        raise ValueError(
            f"unknown idle categories: {', '.join(sorted(unknown))} "
            f"(choose from {', '.join(CATEGORIES)})"
        )
    return [c for c in CHECKS if c.category in wanted]


def find_idle(
    graph: ResourceGraphProvider,
    *,
    subscriptions: Sequence[str] | None = None,
    categories: Iterable[str] | None = None,
    min_age_days: int = 0,
    ignore_tag: str = IGNORE_TAG,
    now: datetime | None = None,
) -> list[IdleFinding]:
    """Run every selected idle check and return findings sorted by category and name.

    ``min_age_days`` only filters checks that can measure age (disks today);
    findings whose age is unknown are kept so nothing is silently hidden.
    """
    now = now or datetime.now(UTC)
    findings: list[IdleFinding] = []
    for check in _select_checks(categories):
        for row in graph.query(load_query(check.query_name), subscriptions):
            if is_ignored(row, ignore_tag):
                continue
            age = (
                age_in_days(row.get(check.age_field) or row.get("timeCreated"), now)
                if check.age_field
                else None
            )
            if age is not None and age < min_age_days:
                continue
            findings.append(
                IdleFinding(
                    category=check.category,
                    name=text(row, "name"),
                    resource_group=text(row, "resourceGroup"),
                    subscription_id=text(row, "subscriptionId"),
                    location=text(row, "location"),
                    detail=check.describe(row),
                    age_days=age,
                    recommendation=check.recommendation,
                    resource_id=text(row, "id"),
                )
            )
    order = {c: i for i, c in enumerate(CATEGORIES)}
    return sorted(findings, key=lambda f: (order[f.category], f.name.lower()))
