"""Azure SDK implementations of the provider protocols.

Authentication uses :class:`azure.identity.DefaultAzureCredential`, so the same
code works with ``az login`` on a laptop, a workload identity in CI, or a managed
identity in an Azure Function. SDK imports are deferred so that the rest of the
package (and the test suite) never needs network access or credentials.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from finops.providers.base import Providers
from finops.queries import Query
from finops.resources import ResourceId, Row

_PAGE_SIZE = 1000


def default_credential() -> Any:
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class AzureResourceGraph:
    def __init__(self, credential: Any | None = None) -> None:
        from azure.mgmt.resourcegraph import ResourceGraphClient

        self._client = ResourceGraphClient(credential or default_credential())

    def query(self, query: Query, subscriptions: Sequence[str] | None = None) -> list[Row]:
        from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

        rows: list[Row] = []
        skip_token: str | None = None
        while True:
            request = QueryRequest(
                subscriptions=list(subscriptions) if subscriptions else None,
                query=query.text,
                options=QueryRequestOptions(
                    result_format="objectArray", top=_PAGE_SIZE, skip_token=skip_token
                ),
            )
            response = self._client.resources(request)
            data: Any = response.data or []
            rows.extend(dict(item) for item in data)
            skip_token = response.skip_token
            if not skip_token:
                return rows


class AzureMonitorMetrics:
    def __init__(self, credential: Any | None = None) -> None:
        from azure.monitor.query import MetricsQueryClient

        self._client = MetricsQueryClient(credential or default_credential())

    def series(
        self,
        resource_id: str,
        metric: str,
        *,
        lookback: timedelta,
        interval: timedelta,
        aggregation: str = "Average",
    ) -> list[float]:
        result = self._client.query_resource(
            resource_id,
            metric_names=[metric],
            timespan=lookback,
            granularity=interval,
            aggregations=[aggregation],
        )
        attribute = aggregation.lower()
        values: list[float] = []
        for item in result.metrics:
            for series in item.timeseries:
                for point in series.data:
                    value = getattr(point, attribute, None)
                    if value is not None:
                        values.append(float(value))
        return values


class AzureCompute:
    def __init__(self, credential: Any | None = None) -> None:
        self._credential = credential or default_credential()
        self._clients: dict[str, Any] = {}
        self._sizes: dict[tuple[str, str], dict[str, int]] = {}

    def _client(self, subscription_id: str) -> Any:
        if subscription_id not in self._clients:
            from azure.mgmt.compute import ComputeManagementClient

            self._clients[subscription_id] = ComputeManagementClient(
                self._credential, subscription_id
            )
        return self._clients[subscription_id]

    def vm_memory_mb(self, subscription_id: str, location: str, vm_size: str) -> int | None:
        key = (subscription_id, location.lower())
        if key not in self._sizes:
            sizes = self._client(subscription_id).virtual_machine_sizes.list(location)
            self._sizes[key] = {s.name.lower(): int(s.memory_in_mb) for s in sizes}
        return self._sizes[key].get(vm_size.lower())

    def set_vm_tags(self, resource_id: str, tags: Mapping[str, str]) -> None:
        from azure.mgmt.compute.models import VirtualMachineUpdate

        rid = ResourceId.parse(resource_id)
        poller = self._client(rid.subscription_id).virtual_machines.begin_update(
            rid.resource_group, rid.name, VirtualMachineUpdate(tags=dict(tags))
        )
        poller.result()

    def deallocate_vm(self, resource_id: str) -> None:
        rid = ResourceId.parse(resource_id)
        poller = self._client(rid.subscription_id).virtual_machines.begin_deallocate(
            rid.resource_group, rid.name
        )
        poller.result()


def azure_providers() -> Providers:
    credential = default_credential()
    return Providers(
        graph=AzureResourceGraph(credential),
        metrics=AzureMonitorMetrics(credential),
        compute=AzureCompute(credential),
    )
