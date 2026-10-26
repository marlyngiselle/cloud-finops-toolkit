"""Fakes for the provider protocols. No test touches the network or Azure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from finops.providers.base import Providers
from finops.queries import Query
from finops.resources import Row

SUB = "00000000-0000-4000-8000-0000000000aa"


def resource(
    name: str,
    rtype: str = "microsoft.compute/virtualmachines",
    rg: str = "rg-test",
    tags: Mapping[str, str] | None = None,
    sub: str = SUB,
    **extra: Any,
) -> Row:
    provider = rtype if "/" in rtype else f"microsoft.compute/{rtype}"
    return {
        "id": f"/subscriptions/{sub}/resourceGroups/{rg}/providers/{provider}/{name}",
        "name": name,
        "type": rtype,
        "resourceGroup": rg,
        "subscriptionId": sub,
        "location": "westeurope",
        "tags": dict(tags) if tags is not None else {},
        **extra,
    }


@dataclass
class FakeGraph:
    responses: dict[str, list[Row]] = field(default_factory=dict)
    calls: list[tuple[str, tuple[str, ...] | None]] = field(default_factory=list)

    def query(self, query: Query, subscriptions: Sequence[str] | None = None) -> list[Row]:
        self.calls.append((query.name, tuple(subscriptions) if subscriptions else None))
        return list(self.responses.get(query.name, []))


@dataclass
class FakeMetrics:
    data: dict[tuple[str, str], list[float]] = field(default_factory=dict)
    calls: list[tuple[str, str, timedelta, timedelta]] = field(default_factory=list)

    def series(
        self,
        resource_id: str,
        metric: str,
        *,
        lookback: timedelta,
        interval: timedelta,
        aggregation: str = "Average",
    ) -> list[float]:
        self.calls.append((resource_id, metric, lookback, interval))
        return list(self.data.get((resource_id, metric), []))


@dataclass
class FakeCompute:
    memory_mb: dict[str, int] = field(default_factory=dict)
    fail_ids: set[str] = field(default_factory=set)
    tag_calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    deallocated: list[str] = field(default_factory=list)

    def vm_memory_mb(self, subscription_id: str, location: str, vm_size: str) -> int | None:
        return self.memory_mb.get(vm_size)

    def set_vm_tags(self, resource_id: str, tags: Mapping[str, str]) -> None:
        if resource_id in self.fail_ids:
            raise RuntimeError("simulated API failure")
        self.tag_calls.append((resource_id, dict(tags)))

    def deallocate_vm(self, resource_id: str) -> None:
        if resource_id in self.fail_ids:
            raise RuntimeError("simulated API failure")
        self.deallocated.append(resource_id)


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def metrics() -> FakeMetrics:
    return FakeMetrics()


@pytest.fixture
def compute() -> FakeCompute:
    return FakeCompute()


@pytest.fixture
def providers(graph: FakeGraph, metrics: FakeMetrics, compute: FakeCompute) -> Providers:
    return Providers(graph=graph, metrics=metrics, compute=compute)
