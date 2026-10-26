"""Synthetic, in-memory providers behind ``finops --demo``.

Every name, ID and address here is invented. Subscription IDs use an obviously
fake pattern, IP addresses come from the documentation ranges reserved by
RFC 5737, and e-mail addresses use ``example.com``. The demo lets anyone run
every command end to end without an Azure account.
"""

from __future__ import annotations

import math
import random
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from finops.providers.base import Providers
from finops.queries import Query
from finops.resources import Row

SUB_PROD = "00000000-0000-4000-8000-000000000001"
SUB_NONPROD = "00000000-0000-4000-8000-000000000002"


def _rid(sub: str, rg: str, provider: str, name: str) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{name}"


def _res(
    sub: str,
    rg: str,
    rtype: str,
    name: str,
    location: str = "westeurope",
    tags: Mapping[str, str] | None = None,
    **extra: object,
) -> Row:
    return {
        "id": _rid(sub, rg, rtype, name),
        "name": name,
        "type": rtype,
        "resourceGroup": rg,
        "subscriptionId": sub,
        "location": location,
        "tags": dict(tags or {}),
        **extra,
    }


def _rg(sub: str, name: str, tags: Mapping[str, str]) -> Row:
    return {
        "id": f"/subscriptions/{sub}/resourceGroups/{name}",
        "name": name,
        "type": "microsoft.resources/subscriptions/resourcegroups",
        "resourceGroup": name,
        "subscriptionId": sub,
        "location": "westeurope",
        "tags": dict(tags),
    }


VM = "microsoft.compute/virtualmachines"


def demo_rows(now: datetime) -> dict[str, list[Row]]:
    def ago(days: int) -> str:
        return (now - timedelta(days=days)).isoformat()

    shop_tags = {"owner": "shop-team@example.com", "cost-center": "CC-1042"}
    return {
        "idle/unattached-disks": [
            _res(
                SUB_NONPROD,
                "rg-ci-dev",
                "microsoft.compute/disks",
                "disk-build-agent-old",
                skuName="Premium_LRS",
                diskSizeGB=256,
                timeCreated=ago(400),
                lastOwnershipUpdateTime=ago(96),
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                "microsoft.compute/disks",
                "disk-sqltest-data02",
                skuName="StandardSSD_LRS",
                diskSizeGB=512,
                timeCreated=ago(200),
                lastOwnershipUpdateTime=ago(41),
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                "microsoft.compute/disks",
                "disk-temp-restore",
                skuName="Standard_LRS",
                diskSizeGB=128,
                timeCreated=ago(3),
                lastOwnershipUpdateTime=ago(3),
            ),
            _res(
                SUB_PROD,
                "rg-shop-prod",
                "microsoft.compute/disks",
                "disk-legal-hold-2025",
                tags={"finops-ignore": "true", "reason": "retention"},
                skuName="Standard_LRS",
                diskSizeGB=1024,
                timeCreated=ago(300),
                lastOwnershipUpdateTime=ago(250),
            ),
        ],
        "idle/unassociated-public-ips": [
            _res(
                SUB_PROD,
                "rg-shop-prod",
                "microsoft.network/publicipaddresses",
                "pip-old-appgw",
                skuName="Standard",
                allocationMethod="Static",
                ipAddress="203.0.113.10",
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                "microsoft.network/publicipaddresses",
                "pip-vm-jump-01",
                skuName="Standard",
                allocationMethod="Static",
                ipAddress="198.51.100.24",
            ),
        ],
        "idle/orphaned-nics": [
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                "microsoft.network/networkinterfaces",
                "nic-vm-jump-01",
                hasPublicIp=True,
                privateIp="10.20.1.4",
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                "microsoft.network/networkinterfaces",
                "nic-etl-old-7731",
                hasPublicIp=False,
                privateIp="10.30.2.9",
            ),
        ],
        "idle/empty-app-service-plans": [
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                "microsoft.web/serverfarms",
                "asp-shop-staging",
                skuName="P1v3",
                skuTier="PremiumV3",
                numberOfWorkers=2,
            ),
        ],
        "idle/stopped-vms": [
            _res(
                SUB_NONPROD,
                "rg-data-test",
                VM,
                "vm-report-batch",
                powerState="PowerState/stopped",
                vmSize="Standard_E8s_v5",
            ),
        ],
        "tags/resources-and-groups": [
            _rg(SUB_PROD, "rg-shop-prod", {**shop_tags, "environment": "prod"}),
            _rg(SUB_NONPROD, "rg-shop-dev", {**shop_tags, "environment": "dev"}),
            _rg(
                SUB_NONPROD, "rg-data-test", {"Owner": "data-team@example.com", "environment": "qa"}
            ),
            _rg(SUB_NONPROD, "rg-ci-dev", {}),
            _res(SUB_PROD, "rg-shop-prod", VM, "vm-shop-web-01", tags={"application": "shop"}),
            _res(
                SUB_PROD,
                "rg-shop-prod",
                "microsoft.sql/servers",
                "sql-shop-prod",
                tags={"application": "shop", "environment": "production"},
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                "microsoft.keyvault/vaults",
                "kv-shop-dev",
                tags={"application": "shop"},
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                "microsoft.storage/storageaccounts",
                "stdataraw001",
                tags={"application": "lake"},
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                "microsoft.containerservice/managedclusters",
                "aks-data-test",
                tags={"application": "", "cost-center": "CC-77"},
            ),
            _res(SUB_NONPROD, "rg-ci-dev", VM, "vm-build-agent-02", tags={"owner": "build"}),
            _res(
                SUB_NONPROD,
                "rg-ci-dev",
                "microsoft.network/networkwatchers",
                "NetworkWatcher_westeurope",
            ),
        ],
        "rightsize/running-vms": [
            _res(
                SUB_PROD,
                "rg-shop-prod",
                VM,
                "vm-shop-web-01",
                vmSize="Standard_D8s_v5",
                powerState="PowerState/running",
            ),
            _res(
                SUB_PROD,
                "rg-shop-prod",
                VM,
                "vm-shop-web-02",
                vmSize="Standard_D8s_v5",
                powerState="PowerState/running",
            ),
            _res(
                SUB_NONPROD,
                "rg-ci-dev",
                VM,
                "vm-build-agent-02",
                vmSize="Standard_F16s_v2",
                powerState="PowerState/running",
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                VM,
                "vm-data-etl",
                vmSize="Standard_E16s_v5",
                powerState="PowerState/running",
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                VM,
                "vm-jump-01",
                vmSize="Standard_B4ms",
                powerState="PowerState/running",
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                VM,
                "vm-new-api-01",
                vmSize="Standard_D4s_v5",
                powerState="PowerState/running",
            ),
        ],
        "schedule/virtual-machines": [
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                VM,
                "vm-shop-dev-01",
                powerState="PowerState/running",
                vmSize="Standard_D4s_v5",
                tags={
                    **shop_tags,
                    "finops-shutdown-time": "19:00",
                    "finops-shutdown-tz": "Europe/Paris",
                    "finops-shutdown-days": "mon-fri",
                },
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                VM,
                "vm-shop-dev-02",
                powerState="PowerState/running",
                vmSize="Standard_D4s_v5",
                tags={
                    **shop_tags,
                    "finops-shutdown-time": "19:00",
                    "finops-shutdown-tz": "Europe/Paris",
                    "finops-shutdown-skip-until": (now + timedelta(days=5)).date().isoformat(),
                },
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                VM,
                "vm-data-test-01",
                powerState="PowerState/deallocated",
                vmSize="Standard_E4s_v5",
                tags={"finops-shutdown-time": "20:00", "finops-shutdown-tz": "America/New_York"},
            ),
            _res(
                SUB_NONPROD,
                "rg-ci-dev",
                VM,
                "vm-ci-agent-03",
                powerState="PowerState/running",
                vmSize="Standard_F8s_v2",
                tags={"finops-shutdown-time": "7pm"},
            ),
            _res(
                SUB_NONPROD,
                "rg-ci-dev",
                VM,
                "vm-build-agent-02",
                powerState="PowerState/running",
                vmSize="Standard_F16s_v2",
                tags={"owner": "build"},
            ),
            _res(
                SUB_PROD,
                "rg-shop-prod",
                VM,
                "vm-shop-web-01",
                powerState="PowerState/running",
                vmSize="Standard_D8s_v5",
                tags={"application": "shop"},
            ),
        ],
        "hybrid-benefit/windows-vms": [
            _res(
                SUB_PROD,
                "rg-erp-prod",
                VM,
                "vm-erp-app-01",
                vmSize="Standard_D4s_v5",
                licenseType="",
                imageOffer="WindowsServer",
                imageSku="2022-datacenter-azure-edition",
            ),
            _res(
                SUB_PROD,
                "rg-erp-prod",
                VM,
                "vm-erp-app-02",
                vmSize="Standard_D4s_v5",
                licenseType="Windows_Server",
                imageOffer="WindowsServer",
                imageSku="2022-datacenter-azure-edition",
            ),
            _res(
                SUB_NONPROD,
                "rg-vdi-dev",
                VM,
                "vm-vdi-pool-01",
                vmSize="Standard_D8s_v5",
                licenseType="",
                imageOffer="windows-11",
                imageSku="win11-23h2-avd",
            ),
        ],
        "hybrid-benefit/sql-vms": [
            _res(
                SUB_PROD,
                "rg-erp-prod",
                "microsoft.sqlvirtualmachine/sqlvirtualmachines",
                "sqlvm-erp-01",
                licenseType="PAYG",
                sqlImageSku="Enterprise",
            ),
            _res(
                SUB_PROD,
                "rg-erp-prod",
                "microsoft.sqlvirtualmachine/sqlvirtualmachines",
                "sqlvm-erp-02-dr",
                licenseType="DR",
                sqlImageSku="Enterprise",
            ),
            _res(
                SUB_NONPROD,
                "rg-data-test",
                "microsoft.sqlvirtualmachine/sqlvirtualmachines",
                "sqlvm-dev-01",
                licenseType="PAYG",
                sqlImageSku="Developer",
            ),
        ],
        "hybrid-benefit/sql-paas": [
            _res(
                SUB_PROD,
                "rg-shop-prod",
                "microsoft.sql/servers/databases",
                "sql-shop-prod/orders",
                licenseType="LicenseIncluded",
                skuName="GP_Gen5",
                skuTier="GeneralPurpose",
                vCores=4,
            ),
            _res(
                SUB_NONPROD,
                "rg-shop-dev",
                "microsoft.sql/servers/databases",
                "sql-shop-dev/orders",
                licenseType="LicenseIncluded",
                skuName="GP_S_Gen5",
                skuTier="GeneralPurpose",
                vCores=2,
            ),
            _res(
                SUB_PROD,
                "rg-data-prod",
                "microsoft.sql/managedinstances",
                "sqlmi-data-prod",
                licenseType="LicenseIncluded",
                skuName="BC_Gen5",
                skuTier="BusinessCritical",
                vCores=8,
            ),
            _res(
                SUB_PROD,
                "rg-shop-prod",
                "microsoft.sql/servers/elasticpools",
                "sql-shop-prod/pool-tenants",
                licenseType="BasePrice",
                skuName="GP_Gen5",
                skuTier="GeneralPurpose",
                vCores=8,
            ),
        ],
    }


@dataclass
class DemoResourceGraph:
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def query(self, query: Query, subscriptions: Sequence[str] | None = None) -> list[Row]:
        rows = demo_rows(self.now).get(query.name, [])
        if subscriptions:
            wanted = {s.lower() for s in subscriptions}
            rows = [r for r in rows if str(r["subscriptionId"]).lower() in wanted]
        return rows


@dataclass(frozen=True)
class _Profile:
    cpu_base: float
    cpu_peak: float
    memory_used_pct: float
    memory_mb: int = 16384
    coverage: float = 1.0


_PROFILES: dict[str, _Profile] = {
    "vm-shop-web-01": _Profile(cpu_base=6, cpu_peak=14, memory_used_pct=31, memory_mb=32768),
    "vm-shop-web-02": _Profile(cpu_base=35, cpu_peak=72, memory_used_pct=58, memory_mb=32768),
    "vm-build-agent-02": _Profile(cpu_base=1, cpu_peak=3, memory_used_pct=12, memory_mb=32768),
    "vm-data-etl": _Profile(cpu_base=4, cpu_peak=15, memory_used_pct=82, memory_mb=131072),
    "vm-jump-01": _Profile(cpu_base=2, cpu_peak=9, memory_used_pct=20),
    "vm-new-api-01": _Profile(cpu_base=10, cpu_peak=25, memory_used_pct=40, coverage=0.2),
}

_SIZE_MEMORY_MB: dict[str, int] = {
    "standard_d4s_v5": 16384,
    "standard_d8s_v5": 32768,
    "standard_e16s_v5": 131072,
    "standard_f16s_v2": 32768,
}


@dataclass
class DemoMetrics:
    """Deterministic, daily-cycle metric series: busy in office hours, quiet at night."""

    def series(
        self,
        resource_id: str,
        metric: str,
        *,
        lookback: timedelta,
        interval: timedelta,
        aggregation: str = "Average",
    ) -> list[float]:
        name = resource_id.rsplit("/", 1)[-1]
        profile = _PROFILES.get(name)
        if profile is None:
            return []
        points = int(lookback / interval * profile.coverage)
        rng = random.Random(zlib.crc32(f"{resource_id}|{metric}".encode()))
        hours_per_point = interval / timedelta(hours=1)
        values: list[float] = []
        for i in range(points):
            hour = (i * hours_per_point) % 24
            daylight = max(0.0, math.sin(math.pi * (hour - 7) / 12))
            if metric == "Percentage CPU":
                span = profile.cpu_peak - profile.cpu_base
                values.append(profile.cpu_base + span * daylight * rng.uniform(0.6, 1.0))
            else:
                total = profile.memory_mb * 1024 * 1024
                used = profile.memory_used_pct * rng.uniform(0.85, 1.0) / 100
                values.append(total * (1 - used))
        return values


@dataclass
class DemoCompute:
    """Records requested actions instead of performing them."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def vm_memory_mb(self, subscription_id: str, location: str, vm_size: str) -> int | None:
        return _SIZE_MEMORY_MB.get(vm_size.lower())

    def set_vm_tags(self, resource_id: str, tags: Mapping[str, str]) -> None:
        self.calls.append(("set_vm_tags", resource_id))

    def deallocate_vm(self, resource_id: str) -> None:
        self.calls.append(("deallocate_vm", resource_id))


def demo_providers(now: datetime | None = None) -> Providers:
    return Providers(
        graph=DemoResourceGraph(now=now or datetime.now(UTC)),
        metrics=DemoMetrics(),
        compute=DemoCompute(),
    )
