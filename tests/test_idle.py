from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import FakeGraph, resource
from finops.idle import CATEGORIES, find_idle

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def populated() -> FakeGraph:
    return FakeGraph(
        {
            "idle/unattached-disks": [
                resource("disk-b", "microsoft.compute/disks", skuName="Premium_LRS",
                         diskSizeGB=128, lastOwnershipUpdateTime=_ago(40)),
                resource("disk-a", "microsoft.compute/disks", skuName="Standard_LRS",
                         diskSizeGB=None, timeCreated=_ago(2)),
                resource("disk-kept", "microsoft.compute/disks",
                         tags={"finops-ignore": "true"}, lastOwnershipUpdateTime=_ago(300)),
            ],
            "idle/unassociated-public-ips": [
                resource("pip-1", "microsoft.network/publicipaddresses", skuName="Standard",
                         allocationMethod="Static", ipAddress=None),
            ],
            "idle/orphaned-nics": [
                resource("nic-1", "microsoft.network/networkinterfaces", hasPublicIp=True,
                         privateIp="10.0.0.4"),
            ],
            "idle/empty-app-service-plans": [
                resource("asp-1", "microsoft.web/serverfarms", skuName="S1", skuTier="Standard",
                         numberOfWorkers=None),
            ],
            "idle/stopped-vms": [resource("vm-1", vmSize="Standard_D2s_v5")],
        }
    )  # fmt: skip


def test_reports_every_category_in_stable_order(populated: FakeGraph) -> None:
    findings = find_idle(populated, now=NOW)
    assert [f.category for f in findings] == [
        "unattached-disk",
        "unattached-disk",
        "unassociated-public-ip",
        "orphaned-nic",
        "empty-app-service-plan",
        "stopped-vm",
    ]
    assert [f.name for f in findings[:2]] == ["disk-a", "disk-b"]


def test_ignore_tag_excludes_resource(populated: FakeGraph) -> None:
    names = {f.name for f in find_idle(populated, now=NOW)}
    assert "disk-kept" not in names


def test_details_are_human_readable(populated: FakeGraph) -> None:
    by_name = {f.name: f for f in find_idle(populated, now=NOW)}
    assert by_name["disk-b"].detail == "128 GiB Premium_LRS"
    assert by_name["disk-a"].detail == "unknown size Standard_LRS"
    assert by_name["pip-1"].detail == "Standard SKU, Static, no address"
    assert by_name["nic-1"].detail.endswith("holds a public IP")
    assert by_name["asp-1"].detail == "S1 (Standard), 1 instance(s), 0 apps"
    assert "stopped but still allocated" in by_name["vm-1"].detail


def test_disk_age_from_ownership_then_creation(populated: FakeGraph) -> None:
    by_name = {f.name: f for f in find_idle(populated, now=NOW)}
    assert by_name["disk-b"].age_days == 40
    assert by_name["disk-a"].age_days == 2
    assert by_name["pip-1"].age_days is None


def test_min_age_filters_young_disks_but_keeps_unknown_ages(populated: FakeGraph) -> None:
    names = {f.name for f in find_idle(populated, now=NOW, min_age_days=30)}
    assert "disk-a" not in names
    assert {"disk-b", "pip-1", "vm-1"} <= names


def test_category_filter_queries_only_what_is_needed(populated: FakeGraph) -> None:
    findings = find_idle(populated, categories=["stopped-vm"], now=NOW)
    assert [f.name for f in findings] == ["vm-1"]
    assert [c[0] for c in populated.calls] == ["idle/stopped-vms"]


def test_unknown_category_is_rejected(graph: FakeGraph) -> None:
    with pytest.raises(ValueError, match="unknown idle categories: nope"):
        find_idle(graph, categories=["nope"])


def test_subscriptions_are_passed_to_provider(graph: FakeGraph) -> None:
    find_idle(graph, subscriptions=["s1", "s2"])
    assert len(graph.calls) == len(CATEGORIES)
    assert all(c[1] == ("s1", "s2") for c in graph.calls)


def test_to_row_contains_full_resource_id(populated: FakeGraph) -> None:
    row = find_idle(populated, categories=["stopped-vm"], now=NOW)[0].to_row()
    assert row["resource_id"].endswith("/vm-1")
    assert row["age_days"] == ""
