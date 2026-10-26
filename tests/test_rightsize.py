from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import FakeCompute, FakeGraph, FakeMetrics, resource
from finops.rightsize import (
    CPU_METRIC,
    MEMORY_METRIC,
    RightsizeSettings,
    Verdict,
    evaluate_vm,
    find_rightsizing,
    memory_used_pct,
    percentile,
    suggest_smaller_size,
)

GIB = 1024 * 1024 * 1024
SETTINGS = RightsizeSettings(lookback_days=1, interval_minutes=60)  # 24 expected points


def flat(value: float, n: int = 24) -> list[float]:
    return [value] * n


class TestPercentile:
    def test_interpolates_like_numpy(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        assert percentile(values, 50) == pytest.approx(2.5)
        assert percentile(values, 95) == pytest.approx(3.85)
        assert percentile(values, 100) == 4.0
        assert percentile(values, 0) == 1.0

    def test_unsorted_input(self) -> None:
        assert percentile([9.0, 1.0, 5.0], 50) == 5.0

    def test_p95_ignores_rare_spikes_but_not_daily_peaks(self) -> None:
        one_spike = [5.0] * 99 + [100.0]
        assert percentile(one_spike, 95) == pytest.approx(5.0)
        daily_peaks = [5.0] * 90 + [90.0] * 10
        assert percentile(daily_peaks, 95) == pytest.approx(90.0)

    @pytest.mark.parametrize(("values", "pct"), [([], 50.0), ([1.0], 101.0), ([1.0], -1.0)])
    def test_invalid(self, values: list[float], pct: float) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            percentile(values, pct)


class TestSuggestSmallerSize:
    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            ("Standard_D8s_v5", "Standard_D4s_v5"),
            ("Standard_E16ds_v5", "Standard_E8ds_v5"),
            ("Standard_F16s_v2", "Standard_F8s_v2"),
            ("Standard_B2ms", "Standard_B1ms"),
            ("Standard_D4", "Standard_D2"),
            ("Standard_D2s_v5", None),  # nothing below 2 vCPUs in the D series
            ("Standard_DS3_v2", None),  # odd vCPU count
            ("Standard_E8-4s_v5", None),  # constrained-core size
            ("not-a-size", None),
        ],
    )
    def test_suggestions(self, size: str, expected: str | None) -> None:
        assert suggest_smaller_size(size) == expected


class TestSettings:
    def test_defaults(self) -> None:
        s = RightsizeSettings()
        assert s.expected_points == 14 * 24
        assert s.lookback == timedelta(days=14)
        assert s.interval == timedelta(hours=1)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"lookback_days": 0},
            {"lookback_days": 94},
            {"percentile": 0},
            {"cpu_threshold": 120},
            {"idle_cpu_threshold": 30, "cpu_threshold": 20},
            {"interval_minutes": 0},
            {"min_coverage": 1.5},
        ],
    )
    def test_validation(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):  # noqa: PT011
            RightsizeSettings(**kwargs)  # type: ignore[arg-type]


class TestEvaluateVm:
    vm = resource("vm-1", vmSize="Standard_D8s_v5")

    def test_memory_used_pct_is_clamped(self) -> None:
        assert memory_used_pct([8 * GIB, 0, 20 * GIB], 16 * 1024) == [50.0, 100.0, 0.0]

    def test_downsize_when_cpu_and_memory_low(self) -> None:
        result = evaluate_vm(self.vm, flat(12), flat(24 * GIB), 32 * 1024, SETTINGS)
        assert result.verdict is Verdict.DOWNSIZE
        assert result.suggested_size == "Standard_D4s_v5"
        assert result.cpu_pct == pytest.approx(12)
        assert result.memory_pct == pytest.approx(25.0)  # 8 of 32 GiB in use
        assert "check regional availability" in result.note

    def test_idle_when_cpu_tiny(self) -> None:
        result = evaluate_vm(self.vm, flat(2), flat(30 * GIB), 32 * 1024, SETTINGS)
        assert result.verdict is Verdict.IDLE
        assert result.suggested_size is None

    def test_keep_when_memory_busy(self) -> None:
        result = evaluate_vm(self.vm, flat(10), flat(2 * GIB), 32 * 1024, SETTINGS)
        assert result.verdict is Verdict.KEEP
        assert result.memory_pct == pytest.approx(93.75)

    def test_keep_when_cpu_busy(self) -> None:
        result = evaluate_vm(self.vm, flat(55), [], None, SETTINGS)
        assert result.verdict is Verdict.KEEP

    def test_cpu_only_decision_is_flagged(self) -> None:
        result = evaluate_vm(self.vm, flat(10), [], None, SETTINGS)
        assert result.verdict is Verdict.DOWNSIZE
        assert result.memory_pct is None
        assert "memory not measured" in result.note

    def test_no_smaller_size(self) -> None:
        vm = resource("vm-2", vmSize="Standard_D2s_v5")
        result = evaluate_vm(vm, flat(10), [], None, SETTINGS)
        assert result.verdict is Verdict.DOWNSIZE
        assert result.suggested_size is None
        assert "another family" in result.note

    def test_insufficient_data(self) -> None:
        result = evaluate_vm(self.vm, flat(1, n=5), [], None, SETTINGS)
        assert result.verdict is Verdict.INSUFFICIENT_DATA
        assert result.cpu_pct is None
        assert "5 of 24" in result.note

    def test_to_row_uses_percentile_label(self) -> None:
        row = evaluate_vm(self.vm, flat(12), [], None, SETTINGS).to_row("p90")
        assert row["cpu_p90"] == "12.0"
        assert row["mem_p90"] == ""


def test_find_rightsizing_wires_providers(
    graph: FakeGraph, metrics: FakeMetrics, compute: FakeCompute
) -> None:
    busy = resource("vm-busy", vmSize="Standard_D4s_v5")
    small = resource("vm-small", vmSize="Standard_D8s_v5")
    unknown = resource("vm-unknown", vmSize="Standard_X9")
    ignored = resource("vm-ignored", vmSize="Standard_D8s_v5", tags={"finops-ignore": "1"})
    graph.responses["rightsize/running-vms"] = [busy, small, unknown, ignored]
    compute.memory_mb = {"Standard_D4s_v5": 16384, "Standard_D8s_v5": 32768}
    metrics.data = {
        (busy["id"], CPU_METRIC): flat(80),
        (busy["id"], MEMORY_METRIC): flat(8 * GIB),
        (small["id"], CPU_METRIC): flat(10),
        (small["id"], MEMORY_METRIC): flat(28 * GIB),
        (unknown["id"], CPU_METRIC): flat(1),
    }

    results = find_rightsizing(graph, metrics, compute, SETTINGS, subscriptions=["s"])

    assert [(r.name, r.verdict) for r in results] == [
        ("vm-unknown", Verdict.IDLE),
        ("vm-small", Verdict.DOWNSIZE),
        ("vm-busy", Verdict.KEEP),
    ]
    # memory is not queried when the size's memory is unknown
    assert (unknown["id"], MEMORY_METRIC) not in {(c[0], c[1]) for c in metrics.calls}
    assert all(c[2] == timedelta(days=1) for c in metrics.calls)
    assert graph.calls == [("rightsize/running-vms", ("s",))]
