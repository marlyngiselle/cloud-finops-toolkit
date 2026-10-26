from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from conftest import FakeCompute, FakeGraph, FakeMetrics, resource
from finops.handler import main, run, settings_from_env
from finops.output import OutputFormat, render, render_table
from finops.providers import azure as azure_module
from finops.providers.base import Providers
from finops.schedule import TAG_DAYS, TAG_TIME, TAG_TZ


class TestHandler:
    now = datetime(2026, 10, 27, 18, 5, tzinfo=UTC)  # 19:05 in Paris

    def _graph(self) -> FakeGraph:
        tags = {TAG_TIME: "19:00", TAG_TZ: "Europe/Paris"}
        return FakeGraph(
            {
                "schedule/virtual-machines": [
                    resource("vm-1", powerState="PowerState/running", tags=tags),
                    resource("vm-2", powerState="PowerState/running"),
                ]
            }
        )

    def test_dry_run_by_default(
        self, compute: FakeCompute, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="finops.handler"):
            results = run(self._graph(), compute, now=self.now)
        assert [(r.name, r.outcome) for r in results] == [("vm-1", "dry-run")]
        assert compute.deallocated == []
        logged = json.loads(caplog.records[0].getMessage())
        assert logged["dry_run"] is True
        assert logged["name"] == "vm-1"

    def test_live_run_deallocates(self, compute: FakeCompute) -> None:
        graph = self._graph()
        results = run(graph, compute, now=self.now, dry_run=False, subscriptions=["s1"])
        assert results[0].outcome == "done"
        assert len(compute.deallocated) == 1
        assert graph.calls == [("schedule/virtual-machines", ("s1",))]

    def test_window_controls_lateness(self, compute: FakeCompute) -> None:
        late = self.now + timedelta(minutes=20)
        results = run(self._graph(), compute, now=late, window=timedelta(minutes=15))
        assert [r.outcome for r in results] == ["not-due"]

    @pytest.mark.parametrize(("fail", "code"), [(False, 0), (True, 1)])
    def test_main_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, compute: FakeCompute, fail: bool, code: int
    ) -> None:
        graph = FakeGraph(
            {
                "schedule/virtual-machines": [
                    resource(
                        "vm-1",
                        powerState="PowerState/running",
                        tags={TAG_TIME: "00:00", TAG_DAYS: "daily"},
                    )
                ]
            }
        )
        if fail:
            compute.fail_ids = {graph.responses["schedule/virtual-machines"][0]["id"]}
        providers = Providers(graph=graph, metrics=FakeMetrics(), compute=compute)
        monkeypatch.setattr(azure_module, "azure_providers", lambda: providers)
        env = {"FINOPS_DRY_RUN": "false", "FINOPS_WINDOW_MINUTES": str(24 * 60)}
        assert main(env) == code

    def test_settings_from_env_defaults(self) -> None:
        assert settings_from_env({}) == (True, timedelta(minutes=30), None)

    def test_settings_from_env_values(self) -> None:
        env = {
            "FINOPS_DRY_RUN": "False",
            "FINOPS_WINDOW_MINUTES": "15",
            "FINOPS_SUBSCRIPTIONS": "a, b,,",
        }
        assert settings_from_env(env) == (False, timedelta(minutes=15), ["a", "b"])

    def test_anything_but_false_stays_dry_run(self) -> None:
        assert settings_from_env({"FINOPS_DRY_RUN": "no"})[0] is True

    @pytest.mark.parametrize("value", ["abc", "0", "-5"])
    def test_settings_from_env_rejects_bad_window(self, value: str) -> None:
        with pytest.raises(ValueError, match="FINOPS_WINDOW_MINUTES"):
            settings_from_env({"FINOPS_WINDOW_MINUTES": value})


class TestOutput:
    rows = [  # noqa: RUF012
        {"name": "a", "size": 1.5, "note": None, "id": "/x/a"},
        {"name": "bbbb", "size": 10, "note": "y" * 100, "id": "/x/b"},
    ]

    def test_table_aligns_and_truncates(self) -> None:
        out = render_table(self.rows, ["name", "size", "note"])
        lines = out.splitlines()
        assert lines[0].split() == ["NAME", "SIZE", "NOTE"]
        assert set(lines[1].replace(" ", "")) == {"-"}
        assert lines[2].startswith("a     1.5")
        assert lines[3].endswith("...")
        assert len(lines[3].split()[-1]) == 64

    def test_empty_table(self) -> None:
        assert render([], fmt=OutputFormat.TABLE, table_columns=["a"]) == "No findings."

    def test_csv_has_every_column(self) -> None:
        out = render(self.rows, fmt=OutputFormat.CSV, table_columns=["name"])
        parsed = list(csv.DictReader(io.StringIO(out)))
        assert list(parsed[0]) == ["name", "size", "note", "id"]
        assert parsed[1]["note"] == "y" * 100
        assert parsed[0]["note"] == ""

    def test_json_list_and_summary(self) -> None:
        plain = json.loads(render(self.rows, fmt=OutputFormat.JSON, table_columns=[]))
        assert plain[0]["id"] == "/x/a"
        wrapped = json.loads(
            render(self.rows, fmt=OutputFormat.JSON, table_columns=[], summary={"total": 2})
        )
        assert wrapped["summary"] == {"total": 2}
        assert len(wrapped["items"]) == 2
