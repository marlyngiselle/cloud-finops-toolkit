"""``finops rightsize``: VMs whose sustained CPU and memory use fit a smaller size.

The decision uses a high percentile (p95 by default) rather than the average:
a VM that idles at 3% but peaks every night at 90% is not a downsize candidate,
and the average would hide that.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from finops.providers.base import ComputeProvider, MetricsProvider, ResourceGraphProvider
from finops.queries import load_query
from finops.resources import IGNORE_TAG, Row, is_ignored, text

CPU_METRIC = "Percentage CPU"
MEMORY_METRIC = "Available Memory Bytes"

_SIZE_PATTERN = re.compile(
    r"^(?P<prefix>Standard_)(?P<family>[A-Z]+)(?P<vcpus>\d+)(?P<rest>[a-z]*(?:_v\d+)?)$"
)


class Verdict(StrEnum):
    IDLE = "idle"
    DOWNSIZE = "downsize"
    KEEP = "keep"
    INSUFFICIENT_DATA = "insufficient-data"


@dataclass(frozen=True)
class RightsizeSettings:
    lookback_days: int = 14
    percentile: float = 95.0
    cpu_threshold: float = 20.0
    memory_threshold: float = 40.0
    idle_cpu_threshold: float = 5.0
    interval_minutes: int = 60
    min_coverage: float = 0.5

    def __post_init__(self) -> None:
        if not 1 <= self.lookback_days <= 93:
            raise ValueError("lookback_days must be between 1 and 93 (Azure Monitor retention)")
        if not 0 < self.percentile <= 100:
            raise ValueError("percentile must be in (0, 100]")
        for name in ("cpu_threshold", "memory_threshold", "idle_cpu_threshold"):
            if not 0 <= getattr(self, name) <= 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if self.idle_cpu_threshold > self.cpu_threshold:
            raise ValueError("idle_cpu_threshold cannot exceed cpu_threshold")
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        if not 0 <= self.min_coverage <= 1:
            raise ValueError("min_coverage must be between 0 and 1")

    @property
    def lookback(self) -> timedelta:
        return timedelta(days=self.lookback_days)

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=self.interval_minutes)

    @property
    def expected_points(self) -> int:
        return self.lookback_days * 24 * 60 // self.interval_minutes


def percentile(values: Sequence[float], pct: float) -> float:
    """Percentile with linear interpolation between closest ranks (NumPy's default)."""
    if not values:
        raise ValueError("percentile of an empty series")
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def suggest_smaller_size(vm_size: str) -> str | None:
    """Halve the vCPU count within the same series, e.g. ``Standard_D8s_v5 -> Standard_D4s_v5``.

    Returns ``None`` when no smaller size exists in the series or the name does
    not follow the standard pattern (constrained-core sizes, GPU sizes with
    unusual names). Always confirm regional availability before resizing.
    """
    match = _SIZE_PATTERN.match(vm_size)
    if not match:
        return None
    vcpus = int(match["vcpus"])
    family = match["family"]
    minimum = 1 if family == "B" else 2
    if vcpus % 2 or vcpus // 2 < minimum:
        return None
    return f"{match['prefix']}{family}{vcpus // 2}{match['rest']}"


@dataclass(frozen=True)
class RightsizeResult:
    name: str
    resource_group: str
    subscription_id: str
    location: str
    vm_size: str
    cpu_pct: float | None
    memory_pct: float | None
    verdict: Verdict
    suggested_size: str | None
    note: str
    resource_id: str

    def to_row(self, pct_label: str = "p95") -> dict[str, Any]:
        return {
            "name": self.name,
            "resource_group": self.resource_group,
            "vm_size": self.vm_size,
            f"cpu_{pct_label}": "" if self.cpu_pct is None else f"{self.cpu_pct:.1f}",
            f"mem_{pct_label}": "" if self.memory_pct is None else f"{self.memory_pct:.1f}",
            "verdict": str(self.verdict),
            "suggested_size": self.suggested_size or "",
            "note": self.note,
            "subscription_id": self.subscription_id,
            "resource_id": self.resource_id,
        }


def memory_used_pct(available_bytes: Sequence[float], memory_mb: int) -> list[float]:
    total = memory_mb * 1024 * 1024
    return [min(100.0, max(0.0, 100.0 - (a / total) * 100.0)) for a in available_bytes]


def evaluate_vm(
    vm: Row,
    cpu_series: Sequence[float],
    available_memory_series: Sequence[float],
    memory_mb: int | None,
    settings: RightsizeSettings,
) -> RightsizeResult:
    """Classify one VM from its metric series. Pure function: no I/O."""
    vm_size = text(vm, "vmSize")

    def result(
        verdict: Verdict,
        note: str,
        cpu: float | None = None,
        mem: float | None = None,
        suggested: str | None = None,
    ) -> RightsizeResult:
        return RightsizeResult(
            name=text(vm, "name"),
            resource_group=text(vm, "resourceGroup"),
            subscription_id=text(vm, "subscriptionId"),
            location=text(vm, "location"),
            vm_size=vm_size,
            cpu_pct=cpu,
            memory_pct=mem,
            verdict=verdict,
            suggested_size=suggested,
            note=note,
            resource_id=text(vm, "id"),
        )

    coverage = len(cpu_series) / settings.expected_points if settings.expected_points else 0
    if coverage < settings.min_coverage:
        return result(
            Verdict.INSUFFICIENT_DATA,
            f"only {len(cpu_series)} of {settings.expected_points} CPU data points",
        )

    cpu = percentile(cpu_series, settings.percentile)
    mem: float | None = None
    if memory_mb and available_memory_series:
        mem = percentile(memory_used_pct(available_memory_series, memory_mb), settings.percentile)
    memory_ok = mem is None or mem < settings.memory_threshold
    memory_note = "" if mem is not None else "memory not measured"

    if cpu < settings.idle_cpu_threshold and memory_ok:
        note = "near-idle: consider a shutdown schedule or decommissioning"
        return result(Verdict.IDLE, _join(note, memory_note), cpu, mem)
    if cpu < settings.cpu_threshold and memory_ok:
        smaller = suggest_smaller_size(vm_size)
        note = (
            "check regional availability before resizing"
            if smaller
            else "no smaller size in this series, consider another family"
        )
        return result(Verdict.DOWNSIZE, _join(note, memory_note), cpu, mem, smaller)
    return result(Verdict.KEEP, "", cpu, mem)


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def find_rightsizing(
    graph: ResourceGraphProvider,
    metrics: MetricsProvider,
    compute: ComputeProvider,
    settings: RightsizeSettings,
    *,
    subscriptions: Sequence[str] | None = None,
    ignore_tag: str = IGNORE_TAG,
) -> list[RightsizeResult]:
    results: list[RightsizeResult] = []
    for vm in graph.query(load_query("rightsize/running-vms"), subscriptions):
        if is_ignored(vm, ignore_tag):
            continue
        resource_id = text(vm, "id")
        cpu = metrics.series(
            resource_id, CPU_METRIC, lookback=settings.lookback, interval=settings.interval
        )
        memory_mb = compute.vm_memory_mb(
            text(vm, "subscriptionId"), text(vm, "location"), text(vm, "vmSize")
        )
        available = (
            metrics.series(
                resource_id, MEMORY_METRIC, lookback=settings.lookback, interval=settings.interval
            )
            if memory_mb
            else []
        )
        results.append(evaluate_vm(vm, cpu, available, memory_mb, settings))
    order = {Verdict.IDLE: 0, Verdict.DOWNSIZE: 1, Verdict.INSUFFICIENT_DATA: 2, Verdict.KEEP: 3}
    return sorted(results, key=lambda r: (order[r.verdict], r.name.lower()))
