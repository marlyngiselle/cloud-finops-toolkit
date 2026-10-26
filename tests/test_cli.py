"""End-to-end CLI tests: the bundled ``--demo`` data and injected fakes, never Azure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import FakeCompute, FakeGraph, resource
from finops import __version__, cli
from finops.providers.base import Providers
from finops.schedule import TAG_DAYS, TAG_TIME, TAG_TZ

POLICY = Path(__file__).resolve().parents[1] / "examples" / "tag-policy.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch, providers: Providers) -> Providers:
    monkeypatch.setattr(cli, "build_providers", lambda demo: providers)
    return providers


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_commands(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("idle", "tags", "rightsize", "schedule", "hybrid-benefit"):
        assert command in result.output


class TestDemo:
    def test_idle(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.app, ["--demo", "idle"])
        assert result.exit_code == 0, result.output
        assert "disk-build-agent-old" in result.output
        assert "disk-legal-hold-2025" not in result.output  # finops-ignore
        assert "9 finding(s)" in result.output

    def test_idle_json_to_file(self, runner: CliRunner, tmp_path: Path) -> None:
        out = tmp_path / "idle.json"
        result = runner.invoke(
            cli.app, ["--demo", "idle", "-c", "stopped-vm", "-f", "json", "-o", str(out)]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(out.read_text())
        assert [d["name"] for d in data] == ["vm-report-batch"]

    def test_idle_bad_category(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.app, ["--demo", "idle", "-c", "everything"])
        assert result.exit_code == 2

    def test_tags(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.app, ["--demo", "tags", "-p", str(POLICY)])
        assert result.exit_code == 0, result.output
        assert "vm-build-agent-02" in result.output
        assert "2/10 resources compliant" in result.output

    def test_tags_fail_on_violations_and_all(self, runner: CliRunner) -> None:
        args = ["--demo", "tags", "-p", str(POLICY), "--all", "--fail-on-violations"]
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1
        assert "kv-shop-dev" in result.output  # compliant rows included with --all

    def test_tags_bad_policy(self, runner: CliRunner, tmp_path: Path) -> None:
        bad = tmp_path / "p.yaml"
        bad.write_text("required_tags: []")
        result = runner.invoke(cli.app, ["--demo", "tags", "-p", str(bad)])
        assert result.exit_code == 2

    def test_rightsize(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.app, ["--demo", "rightsize"])
        assert result.exit_code == 0, result.output
        assert "Standard_D4s_v5" in result.output
        assert "vm-shop-web-02" not in result.output  # keep, hidden by default
        result_all = runner.invoke(cli.app, ["--demo", "rightsize", "--all", "--percentile", "90"])
        assert "vm-shop-web-02" in result_all.output
        assert "CPU_P90" in result_all.output

    def test_rightsize_invalid_settings(self, runner: CliRunner) -> None:
        args = ["--demo", "rightsize", "--idle-cpu-threshold", "50", "--cpu-threshold", "10"]
        assert runner.invoke(cli.app, args).exit_code == 2

    def test_hybrid_benefit_csv(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.app, ["--demo", "hybrid-benefit", "-w", "sql-vm", "-f", "csv"])
        assert result.exit_code == 0, result.output
        assert result.output.startswith("workload,name,resource_group")
        assert "sqlvm-erp-01" in result.output

    def test_schedule_run_at(self, runner: CliRunner) -> None:
        args = ["--demo", "schedule", "run", "--at", "2026-10-27T18:10:00Z"]
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert "vm-shop-dev-01" in result.output
        assert "Dry run" in result.output

    def test_schedule_run_bad_at(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.app, ["--demo", "schedule", "run", "--at", "teatime"])
        assert result.exit_code == 2


class TestScheduleWithFakes:
    def _vms(self, graph: FakeGraph) -> None:
        graph.responses["schedule/virtual-machines"] = [
            resource("vm-dev", rg="rg-dev", powerState="PowerState/running",
                     tags={"env": "dev"}),
            resource("vm-prod", rg="rg-prod", powerState="PowerState/running",
                     tags={"env": "prod"}),
        ]  # fmt: skip

    def test_apply_requires_a_selector(self, runner: CliRunner, fake: Providers) -> None:
        result = runner.invoke(cli.app, ["schedule", "apply", "--time", "19:00"])
        assert result.exit_code == 2
        assert "--all-vms" in result.output

    def test_apply_dry_run_then_apply(self, runner: CliRunner, fake: Providers) -> None:
        assert isinstance(fake.graph, FakeGraph)
        assert isinstance(fake.compute, FakeCompute)
        self._vms(fake.graph)
        base = ["schedule", "apply", "--time", "19:00", "--tz", "Europe/Paris", "--tag", "env=dev"]

        dry = runner.invoke(cli.app, base)
        assert dry.exit_code == 0, dry.output
        assert "dry-run" in dry.output
        assert fake.compute.tag_calls == []

        live = runner.invoke(cli.app, [*base, "--apply"])
        assert live.exit_code == 0, live.output
        [(rid, tags)] = fake.compute.tag_calls
        assert rid.endswith("/vm-dev")
        assert tags == {
            "env": "dev",
            TAG_TIME: "19:00",
            TAG_TZ: "Europe/Paris",
            TAG_DAYS: "mon-fri",
        }

    @pytest.mark.parametrize(
        "extra",
        [
            ["--time", "7pm"],
            ["--time", "19:00", "--tz", "Nowhere/City"],
            ["--time", "19:00", "--skip-until", "soon"],
            ["--time", "19:00", "--tag", "novalue"],
        ],
    )
    def test_apply_rejects_bad_input(
        self, runner: CliRunner, fake: Providers, extra: list[str]
    ) -> None:
        result = runner.invoke(cli.app, ["schedule", "apply", "--all-vms", *extra])
        assert result.exit_code == 2

    def test_remove(self, runner: CliRunner, fake: Providers) -> None:
        assert isinstance(fake.graph, FakeGraph)
        self._vms(fake.graph)
        result = runner.invoke(cli.app, ["schedule", "remove", "-g", "rg-prod", "-n", "vm-prod"])
        assert result.exit_code == 0, result.output
        assert "vm-prod" in result.output
        assert "unchanged" in result.output

    def test_run_naive_at_is_utc_and_apply(self, runner: CliRunner, fake: Providers) -> None:
        assert isinstance(fake.graph, FakeGraph)
        assert isinstance(fake.compute, FakeCompute)
        fake.graph.responses["schedule/virtual-machines"] = [
            resource("vm-1", powerState="PowerState/running", tags={TAG_TIME: "18:00"})
        ]
        args = ["schedule", "run", "--at", "2026-10-27T18:05:00", "--apply"]
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert "done" in result.output
        assert "Dry run" not in result.output
        assert len(fake.compute.deallocated) == 1


def test_state_without_callback_defaults_to_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def factory(demo: bool) -> Providers:
        seen.append(demo)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "build_providers", factory)
    state = cli.State()
    with pytest.raises(RuntimeError):
        _ = state.providers
    assert seen == [False]


def test_build_providers_demo() -> None:
    providers = cli.build_providers(demo=True)
    assert type(providers.graph).__name__ == "DemoResourceGraph"
