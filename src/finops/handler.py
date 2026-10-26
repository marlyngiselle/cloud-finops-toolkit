"""Scheduled shutdown handler.

Designed to run unattended on a timer: an Azure Functions timer trigger (see
``examples/azure-function``), a container job, or cron. It reads the
``finops-shutdown-*`` tags, decides which VMs are due, and deallocates them.

Configuration comes from environment variables so the same code runs anywhere:

==========================  ===========================================  =========
Variable                    Meaning                                      Default
==========================  ===========================================  =========
``FINOPS_DRY_RUN``          ``false`` to actually deallocate VMs         ``true``
``FINOPS_WINDOW_MINUTES``   How long after the shutdown time a VM is     ``30``
                            still considered due (>= timer interval)
``FINOPS_SUBSCRIPTIONS``    Comma-separated subscription IDs to scan     all visible
==========================  ===========================================  =========

Dry-run is the default on purpose: a misconfigured deployment logs what it
would do instead of stopping machines.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from finops.providers.base import ComputeProvider, ResourceGraphProvider
from finops.queries import load_query
from finops.schedule import ActionResult, execute_shutdowns, plan_shutdowns

logger = logging.getLogger("finops.handler")


def run(
    graph: ResourceGraphProvider,
    compute: ComputeProvider,
    *,
    now: datetime | None = None,
    dry_run: bool = True,
    window: timedelta = timedelta(minutes=30),
    subscriptions: Sequence[str] | None = None,
) -> list[ActionResult]:
    now = now or datetime.now(UTC)
    vms = graph.query(load_query("schedule/virtual-machines"), subscriptions)
    decisions = plan_shutdowns(vms, now, window=window)
    results = execute_shutdowns(decisions, compute, dry_run=dry_run)
    for result in results:
        logger.info(json.dumps({"dry_run": dry_run, **result.to_row()}))
    return results


def settings_from_env(
    env: Mapping[str, str],
) -> tuple[bool, timedelta, list[str] | None]:
    dry_run = env.get("FINOPS_DRY_RUN", "true").strip().lower() != "false"
    window_text = env.get("FINOPS_WINDOW_MINUTES", "30")
    try:
        window_minutes = int(window_text)
    except ValueError as exc:
        raise ValueError(f"FINOPS_WINDOW_MINUTES must be an integer, got {window_text!r}") from exc
    if window_minutes <= 0:
        raise ValueError("FINOPS_WINDOW_MINUTES must be positive")
    subs = [s.strip() for s in env.get("FINOPS_SUBSCRIPTIONS", "").split(",") if s.strip()]
    return dry_run, timedelta(minutes=window_minutes), subs or None


def main(env: Mapping[str, str] | None = None) -> int:
    """Entry point for timers and ``python -m finops.handler``. Returns a process exit code."""
    from finops.providers.azure import azure_providers

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dry_run, window, subscriptions = settings_from_env(env if env is not None else os.environ)
    providers = azure_providers()
    results = run(
        providers.graph,
        providers.compute,
        dry_run=dry_run,
        window=window,
        subscriptions=subscriptions,
    )
    failed = [r for r in results if r.outcome == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
