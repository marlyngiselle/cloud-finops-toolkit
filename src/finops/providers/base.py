"""The three seams between the FinOps logic and the cloud.

Every command is written against these protocols, never against an Azure SDK
client. Production code wires in :mod:`finops.providers.azure`; tests and the
``--demo`` mode wire in in-memory fakes. That keeps the logic fast to test and
impossible to point at a real subscription by accident.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from finops.queries import Query
from finops.resources import Row


class ResourceGraphProvider(Protocol):
    def query(self, query: Query, subscriptions: Sequence[str] | None = None) -> list[Row]:
        """Run a Resource Graph query and return every row (pagination handled)."""
        ...


class MetricsProvider(Protocol):
    def series(
        self,
        resource_id: str,
        metric: str,
        *,
        lookback: timedelta,
        interval: timedelta,
        aggregation: str = "Average",
    ) -> list[float]:
        """Return the metric's data points over ``lookback``, one per ``interval``."""
        ...


class ComputeProvider(Protocol):
    def vm_memory_mb(self, subscription_id: str, location: str, vm_size: str) -> int | None:
        """Memory of a VM size in MiB, or ``None`` if the size is unknown."""
        ...

    def set_vm_tags(self, resource_id: str, tags: Mapping[str, str]) -> None:
        """Replace the full tag set of a VM."""
        ...

    def deallocate_vm(self, resource_id: str) -> None:
        """Stop and deallocate a VM so compute is no longer billed."""
        ...


@dataclass(frozen=True)
class Providers:
    graph: ResourceGraphProvider
    metrics: MetricsProvider
    compute: ComputeProvider
