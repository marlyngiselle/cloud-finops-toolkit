from __future__ import annotations

import pytest

from conftest import FakeGraph, resource
from finops.hybrid_benefit import (
    Workload,
    evaluate_sql_paas,
    evaluate_sql_vm,
    evaluate_windows_vm,
    find_hybrid_benefit_gaps,
)


def win(
    license_type: str | None, offer: str = "WindowsServer", sku: str = "2022"
) -> dict[str, object]:
    return resource("vm", vmSize="Standard_D4s_v5", licenseType=license_type,
                    imageOffer=offer, imageSku=sku)  # fmt: skip


class TestWindowsVm:
    @pytest.mark.parametrize("license_type", [None, "", "None"])
    def test_pay_as_you_go_is_a_finding(self, license_type: str | None) -> None:
        finding = evaluate_windows_vm(win(license_type))
        assert finding is not None
        assert finding.workload is Workload.WINDOWS_VM
        assert finding.size == "Standard_D4s_v5"
        assert "Windows_Server" in finding.recommendation

    @pytest.mark.parametrize("license_type", ["Windows_Server", "windows_client"])
    def test_already_licensed(self, license_type: str) -> None:
        assert evaluate_windows_vm(win(license_type)) is None

    @pytest.mark.parametrize(("offer", "sku"), [("windows-11", "win11-23h2"),
                                                ("Windows-10", "20h2-ent")])  # fmt: skip
    def test_client_os_is_skipped(self, offer: str, sku: str) -> None:
        assert evaluate_windows_vm(win("", offer, sku)) is None


class TestSqlVm:
    def test_payg_is_a_finding(self) -> None:
        finding = evaluate_sql_vm(resource("s", licenseType="PAYG", sqlImageSku="Enterprise"))
        assert finding is not None
        assert finding.current_license == "PAYG"

    @pytest.mark.parametrize(
        ("license_type", "edition"),
        [("AHUB", "Standard"), ("DR", "Enterprise"), ("PAYG", "Developer"), ("PAYG", "Express")],
    )
    def test_not_findings(self, license_type: str, edition: str) -> None:
        row = resource("s", licenseType=license_type, sqlImageSku=edition)
        assert evaluate_sql_vm(row) is None


class TestSqlPaas:
    @pytest.mark.parametrize(
        ("rtype", "workload"),
        [
            ("microsoft.sql/servers/databases", Workload.SQL_DATABASE),
            ("Microsoft.Sql/servers/elasticPools", Workload.SQL_ELASTIC_POOL),
            ("microsoft.sql/managedinstances", Workload.SQL_MANAGED_INSTANCE),
        ],
    )
    def test_license_included_is_a_finding(self, rtype: str, workload: Workload) -> None:
        row = resource("db", rtype, licenseType="LicenseIncluded", skuName="GP_Gen5", vCores=4)
        finding = evaluate_sql_paas(row)
        assert finding is not None
        assert finding.workload is workload
        assert finding.size == "GP_Gen5 (4 vCores)"

    def test_not_findings(self) -> None:
        base = "microsoft.sql/servers/databases"
        assert evaluate_sql_paas(resource("d", base, licenseType="BasePrice")) is None
        serverless = resource("d", base, licenseType="LicenseIncluded", skuName="GP_S_Gen5")
        assert evaluate_sql_paas(serverless) is None
        assert evaluate_sql_paas(resource("d", "microsoft.web/sites")) is None

    def test_size_without_vcores(self) -> None:
        row = resource("d", "microsoft.sql/managedinstances", licenseType="LicenseIncluded",
                       skuName="GP_Gen5")  # fmt: skip
        finding = evaluate_sql_paas(row)
        assert finding is not None
        assert finding.size == "GP_Gen5"


def test_find_gaps_filters_workloads_and_ignored(graph: FakeGraph) -> None:
    graph.responses = {
        "hybrid-benefit/windows-vms": [
            resource("vm-b", licenseType=""),
            resource("vm-a", licenseType=""),
            resource("vm-x", licenseType="", tags={"finops-ignore": "true"}),
        ],
        "hybrid-benefit/sql-vms": [resource("sqlvm", licenseType="PAYG")],
        "hybrid-benefit/sql-paas": [
            resource("mi", "microsoft.sql/managedinstances", licenseType="LicenseIncluded"),
            resource("db", "microsoft.sql/servers/databases", licenseType="LicenseIncluded"),
        ],
    }
    all_findings = find_hybrid_benefit_gaps(graph)
    assert [(f.workload.value, f.name) for f in all_findings] == [
        ("windows-vm", "vm-a"),
        ("windows-vm", "vm-b"),
        ("sql-vm", "sqlvm"),
        ("sql-database", "db"),
        ("sql-managed-instance", "mi"),
    ]
    assert all_findings[0].to_row()["workload"] == "windows-vm"

    graph.calls.clear()
    only_mi = find_hybrid_benefit_gaps(graph, workloads=[Workload.SQL_MANAGED_INSTANCE])
    assert [f.name for f in only_mi] == ["mi"]
    assert [c[0] for c in graph.calls] == ["hybrid-benefit/sql-paas"]
