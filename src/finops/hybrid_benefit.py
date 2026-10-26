"""``finops hybrid-benefit``: Windows and SQL workloads paying for a license twice.

Azure Hybrid Benefit lets an organisation reuse on-premises Windows Server and
SQL Server licenses that have Software Assurance (or qualifying subscriptions).
Enabling it is a one-line change, but it is a licensing decision: every finding
here must be checked against the license entitlement before it is applied.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from finops.providers.base import ResourceGraphProvider
from finops.queries import load_query
from finops.resources import IGNORE_TAG, Row, is_ignored, text

_CLIENT_OS_MARKERS = ("windows-10", "windows-11", "windows-7", "windows-8")
_FREE_SQL_EDITIONS = {"developer", "express"}


class Workload(StrEnum):
    WINDOWS_VM = "windows-vm"
    SQL_VM = "sql-vm"
    SQL_DATABASE = "sql-database"
    SQL_ELASTIC_POOL = "sql-elastic-pool"
    SQL_MANAGED_INSTANCE = "sql-managed-instance"


@dataclass(frozen=True)
class HybridBenefitFinding:
    workload: Workload
    name: str
    resource_group: str
    subscription_id: str
    location: str
    size: str
    current_license: str
    recommendation: str
    resource_id: str

    def to_row(self) -> dict[str, Any]:
        return {
            "workload": str(self.workload),
            "name": self.name,
            "resource_group": self.resource_group,
            "location": self.location,
            "size": self.size,
            "current_license": self.current_license,
            "recommendation": self.recommendation,
            "subscription_id": self.subscription_id,
            "resource_id": self.resource_id,
        }


def _finding(
    row: Row, workload: Workload, size: str, current: str, rec: str
) -> HybridBenefitFinding:
    return HybridBenefitFinding(
        workload=workload,
        name=text(row, "name"),
        resource_group=text(row, "resourceGroup"),
        subscription_id=text(row, "subscriptionId"),
        location=text(row, "location"),
        size=size,
        current_license=current,
        recommendation=rec,
        resource_id=text(row, "id"),
    )


def evaluate_windows_vm(row: Row) -> HybridBenefitFinding | None:
    license_type = text(row, "licenseType")
    if license_type.lower() in {"windows_server", "windows_client"}:
        return None
    image = f"{text(row, 'imageOffer')} {text(row, 'imageSku')}".lower()
    if any(marker in image for marker in _CLIENT_OS_MARKERS):
        return None  # client OS: Windows_Client rights, not Windows Server AHB
    return _finding(
        row,
        Workload.WINDOWS_VM,
        text(row, "vmSize"),
        license_type or "none (pay-as-you-go)",
        "Set licenseType=Windows_Server if covered by Windows Server licenses with SA.",
    )


def evaluate_sql_vm(row: Row) -> HybridBenefitFinding | None:
    if text(row, "sqlImageSku").lower() in _FREE_SQL_EDITIONS:
        return None
    license_type = text(row, "licenseType")
    if license_type.upper() != "PAYG":
        return None  # AHUB already applied, or DR (free passive replica)
    return _finding(
        row,
        Workload.SQL_VM,
        text(row, "sqlImageSku"),
        license_type,
        "Switch the SQL IaaS license model to AHUB if covered by SQL Server licenses with SA.",
    )


_PAAS_TYPES = {
    "microsoft.sql/servers/databases": Workload.SQL_DATABASE,
    "microsoft.sql/servers/elasticpools": Workload.SQL_ELASTIC_POOL,
    "microsoft.sql/managedinstances": Workload.SQL_MANAGED_INSTANCE,
}


def evaluate_sql_paas(row: Row) -> HybridBenefitFinding | None:
    workload = _PAAS_TYPES.get(text(row, "type").lower())
    if workload is None:
        return None
    if text(row, "licenseType") != "LicenseIncluded":
        return None
    sku = text(row, "skuName")
    if sku.upper().startswith("GP_S_"):
        return None  # serverless compute does not support Hybrid Benefit
    vcores = row.get("vCores")
    size = f"{sku} ({vcores} vCores)" if vcores else sku
    return _finding(
        row,
        workload,
        size,
        "LicenseIncluded",
        "Set licenseType=BasePrice if covered by SQL Server licenses with SA.",
    )


Evaluator = Callable[[Row], HybridBenefitFinding | None]

_SOURCES: tuple[tuple[str, Evaluator, frozenset[Workload]], ...] = (
    ("hybrid-benefit/windows-vms", evaluate_windows_vm, frozenset({Workload.WINDOWS_VM})),
    ("hybrid-benefit/sql-vms", evaluate_sql_vm, frozenset({Workload.SQL_VM})),
    (
        "hybrid-benefit/sql-paas",
        evaluate_sql_paas,
        frozenset(
            {Workload.SQL_DATABASE, Workload.SQL_ELASTIC_POOL, Workload.SQL_MANAGED_INSTANCE}
        ),
    ),
)


def find_hybrid_benefit_gaps(
    graph: ResourceGraphProvider,
    *,
    subscriptions: Sequence[str] | None = None,
    workloads: Iterable[Workload] | None = None,
    ignore_tag: str = IGNORE_TAG,
) -> list[HybridBenefitFinding]:
    wanted = frozenset(workloads) if workloads else frozenset(Workload)
    findings: list[HybridBenefitFinding] = []
    for query_name, evaluate, produces in _SOURCES:
        if not produces & wanted:
            continue
        for row in graph.query(load_query(query_name), subscriptions):
            if is_ignored(row, ignore_tag):
                continue
            finding = evaluate(row)
            if finding and finding.workload in wanted:
                findings.append(finding)
    order = {w: i for i, w in enumerate(Workload)}
    return sorted(findings, key=lambda f: (order[f.workload], f.name.lower()))
