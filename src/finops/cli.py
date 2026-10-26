"""Command-line interface: ``finops <command>``.

Reports go to stdout so they can be piped or redirected; summaries and safety
notices go to stderr so CSV and JSON output stays machine-readable.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from finops import __version__
from finops.hybrid_benefit import Workload, find_hybrid_benefit_gaps
from finops.idle import CATEGORIES, find_idle
from finops.output import OutputFormat, render
from finops.providers.base import Providers
from finops.queries import load_query
from finops.rightsize import RightsizeSettings, Verdict, find_rightsizing
from finops.schedule import (
    ScheduleError,
    ShutdownSchedule,
    apply_tag_changes,
    parse_days,
    parse_time,
    plan_apply,
    plan_remove,
    select_vms,
)
from finops.tags import PolicyError, TagPolicy, evaluate_resources

app = typer.Typer(
    name="finops",
    help="Find Azure cloud waste and act on it safely. Read-only unless a command says otherwise.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
schedule_app = typer.Typer(
    help="Auto-shutdown schedules for VMs, driven by tags. Dry-run unless --apply is given.",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")


def build_providers(demo: bool) -> Providers:
    """Create the providers for this run. Tests replace this function."""
    if demo:
        from finops.providers.demo import demo_providers

        return demo_providers()
    from finops.providers.azure import azure_providers

    return azure_providers()


@dataclass
class State:
    demo: bool = False
    _providers: Providers | None = field(default=None, repr=False)

    @property
    def providers(self) -> Providers:
        if self._providers is None:
            self._providers = build_providers(self.demo)
        return self._providers


def _state(ctx: typer.Context) -> State:
    if not isinstance(ctx.obj, State):
        ctx.obj = State()
    return ctx.obj


def _version(value: bool) -> None:
    if value:
        typer.echo(f"finops {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    demo: Annotated[
        bool,
        typer.Option("--demo", help="Use bundled synthetic data. No Azure calls are made."),
    ] = False,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    ctx.obj = State(demo=demo)


SubscriptionOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--subscription", "-s", help="Subscription ID to scan (repeatable). Default: all visible."
    ),
]
FormatOpt = Annotated[OutputFormat, typer.Option("--format", "-f", help="Output format.")]
OutputOpt = Annotated[
    Path | None, typer.Option("--output", "-o", help="Write the report to a file.")
]
ApplyOpt = Annotated[
    bool, typer.Option("--apply", help="Make the changes. Without it nothing is modified.")
]


def _emit(content: str, output: Path | None) -> None:
    if output:
        output.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        typer.echo(f"Wrote {output}", err=True)
    else:
        typer.echo(content.rstrip("\n"))


def _note(message: str) -> None:
    typer.echo(message, err=True)


def _counts(values: Sequence[str]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(Counter(values).items()))


# ------------------------------------------------------------------ idle


@app.command()
def idle(
    ctx: typer.Context,
    subscription: SubscriptionOpt = None,
    category: Annotated[
        list[str] | None,
        typer.Option(
            "--category", "-c", help=f"Limit to a category (repeatable): {', '.join(CATEGORIES)}."
        ),
    ] = None,
    min_age_days: Annotated[
        int, typer.Option(min=0, help="Skip disks detached for fewer days than this.")
    ] = 0,
    fmt: FormatOpt = OutputFormat.TABLE,
    output: OutputOpt = None,
) -> None:
    """Find idle and orphaned resources: unattached disks, unused public IPs, orphaned NICs,
    empty App Service plans and VMs stopped without being deallocated."""
    try:
        findings = find_idle(
            _state(ctx).providers.graph,
            subscriptions=subscription,
            categories=category,
            min_age_days=min_age_days,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--category") from exc
    rows = [f.to_row() for f in findings]
    columns = ["category", "name", "resource_group", "location", "detail", "age_days"]
    _emit(render(rows, fmt=fmt, table_columns=columns), output)
    if findings:
        _note(f"\n{len(findings)} finding(s). {_counts([f.category for f in findings])}")


# ------------------------------------------------------------------ tags


@app.command()
def tags(
    ctx: typer.Context,
    policy: Annotated[
        Path,
        typer.Option(
            "--policy", "-p", exists=True, dir_okay=False, help="Required-tags policy (YAML)."
        ),
    ],
    subscription: SubscriptionOpt = None,
    show_all: Annotated[
        bool, typer.Option("--all", help="Include compliant resources in the report.")
    ] = False,
    fail_on_violations: Annotated[
        bool, typer.Option(help="Exit with code 1 when any resource is non-compliant (for CI).")
    ] = False,
    fmt: FormatOpt = OutputFormat.TABLE,
    output: OutputOpt = None,
) -> None:
    """Report tag compliance against a required-tags policy."""
    try:
        tag_policy = TagPolicy.from_yaml(policy)
    except PolicyError as exc:
        raise typer.BadParameter(str(exc), param_hint="--policy") from exc
    rows_in = _state(ctx).providers.graph.query(
        load_query("tags/resources-and-groups"), subscription
    )
    report = evaluate_resources(tag_policy, rows_in)
    results = report.results if show_all else [r for r in report.results if not r.compliant]
    rows = [r.to_row() for r in results]
    columns = ["name", "type", "resource_group", "status", "violations"]
    _emit(render(rows, fmt=fmt, table_columns=columns, summary=report.summary()), output)
    summary = report.summary()
    _note(
        f"\n{summary['compliant']}/{summary['evaluated']} resources compliant "
        f"({summary['compliance_pct']}%). "
        + (
            "Violations by tag: " + _counts_from(summary["violations_by_tag"])
            if summary["violations_by_tag"]
            else ""
        )
    )
    if fail_on_violations and report.compliant < report.total:
        raise typer.Exit(code=1)


def _counts_from(counts: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in counts.items())


# ------------------------------------------------------------------ rightsize


@app.command()
def rightsize(
    ctx: typer.Context,
    subscription: SubscriptionOpt = None,
    lookback_days: Annotated[int, typer.Option(min=1, max=93, help="Days of metrics.")] = 14,
    percentile: Annotated[
        float, typer.Option(min=1, max=100, help="Percentile used for CPU and memory.")
    ] = 95.0,
    cpu_threshold: Annotated[
        float, typer.Option(min=0, max=100, help="Downsize when CPU percentile is below (%).")
    ] = 20.0,
    memory_threshold: Annotated[
        float, typer.Option(min=0, max=100, help="...and memory percentile is below (%).")
    ] = 40.0,
    idle_cpu_threshold: Annotated[
        float, typer.Option(min=0, max=100, help="Flag as idle when CPU percentile is below (%).")
    ] = 5.0,
    show_all: Annotated[
        bool, typer.Option("--all", help="Include VMs to keep and VMs without enough data.")
    ] = False,
    fmt: FormatOpt = OutputFormat.TABLE,
    output: OutputOpt = None,
) -> None:
    """Find VM rightsizing candidates from Azure Monitor CPU and memory percentiles."""
    try:
        settings = RightsizeSettings(
            lookback_days=lookback_days,
            percentile=percentile,
            cpu_threshold=cpu_threshold,
            memory_threshold=memory_threshold,
            idle_cpu_threshold=idle_cpu_threshold,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    providers = _state(ctx).providers
    results = find_rightsizing(
        providers.graph, providers.metrics, providers.compute, settings, subscriptions=subscription
    )
    actionable = {Verdict.IDLE, Verdict.DOWNSIZE}
    shown = results if show_all else [r for r in results if r.verdict in actionable]
    label = f"p{percentile:g}"
    rows = [r.to_row(label) for r in shown]
    columns = [
        "name", "resource_group", "vm_size", f"cpu_{label}", f"mem_{label}",
        "verdict", "suggested_size", "note",
    ]  # fmt: skip
    _emit(render(rows, fmt=fmt, table_columns=columns), output)
    _note(
        f"\n{len(results)} running VM(s) evaluated over {lookback_days} days. "
        f"{_counts([str(r.verdict) for r in results])}"
    )


# ------------------------------------------------------------------ hybrid benefit


@app.command("hybrid-benefit")
def hybrid_benefit(
    ctx: typer.Context,
    subscription: SubscriptionOpt = None,
    workload: Annotated[
        list[Workload] | None,
        typer.Option("--workload", "-w", help="Limit to a workload type (repeatable)."),
    ] = None,
    fmt: FormatOpt = OutputFormat.TABLE,
    output: OutputOpt = None,
) -> None:
    """List Windows VMs and SQL workloads not using Azure Hybrid Benefit."""
    findings = find_hybrid_benefit_gaps(
        _state(ctx).providers.graph, subscriptions=subscription, workloads=workload
    )
    rows = [f.to_row() for f in findings]
    columns = ["workload", "name", "resource_group", "size", "current_license"]
    _emit(render(rows, fmt=fmt, table_columns=columns), output)
    if findings:
        _note(
            f"\n{len(findings)} workload(s) without Hybrid Benefit. "
            "Confirm license entitlement (Software Assurance) before enabling it."
        )


# ------------------------------------------------------------------ schedule

ResourceGroupOpt = Annotated[
    str | None, typer.Option("--resource-group", "-g", help="Only VMs in this resource group.")
]
NameOpt = Annotated[list[str] | None, typer.Option("--name", "-n", help="VM name (repeatable).")]
TagFilterOpt = Annotated[
    list[str] | None,
    typer.Option("--tag", help="Only VMs with this tag, as key=value (repeatable)."),
]
AllVmsOpt = Annotated[
    bool, typer.Option("--all-vms", help="Target every VM in scope (no other selector).")
]

_ACTION_COLUMNS = ["name", "resource_group", "action", "outcome", "detail"]


def _parse_tag_filters(values: Sequence[str] | None) -> dict[str, str]:
    filters: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise typer.BadParameter(f"expected key=value, got {item!r}", param_hint="--tag")
        filters[key.strip()] = value.strip()
    return filters


def _select_targets(
    state: State,
    subscription: Sequence[str] | None,
    resource_group: str | None,
    names: Sequence[str] | None,
    tag_filters: Sequence[str] | None,
    all_vms: bool,
) -> list[dict[str, Any]]:
    filters = _parse_tag_filters(tag_filters)
    if not (resource_group or names or filters or all_vms):
        raise typer.BadParameter(
            "select VMs with --resource-group, --name or --tag (or --all-vms to target all)"
        )
    vms = state.providers.graph.query(load_query("schedule/virtual-machines"), subscription)
    return select_vms(vms, resource_group=resource_group, names=names or (), tag_filters=filters)


def _dry_run_notice(apply: bool) -> None:
    if not apply:
        _note("\nDry run: nothing was changed. Re-run with --apply to make these changes.")


@schedule_app.command("apply")
def schedule_apply(
    ctx: typer.Context,
    at: Annotated[str, typer.Option("--time", help="Local shutdown time, HH:MM (24h).")],
    tz: Annotated[str, typer.Option("--tz", help="IANA time zone, e.g. Europe/Paris.")] = "UTC",
    days: Annotated[str, typer.Option(help="Days, e.g. mon-fri, daily, sat,sun.")] = "mon-fri",
    skip_until: Annotated[
        str | None, typer.Option(help="Pause the schedule until this date (YYYY-MM-DD).")
    ] = None,
    subscription: SubscriptionOpt = None,
    resource_group: ResourceGroupOpt = None,
    name: NameOpt = None,
    tag: TagFilterOpt = None,
    all_vms: AllVmsOpt = False,
    apply: ApplyOpt = False,
    fmt: FormatOpt = OutputFormat.TABLE,
) -> None:
    """Tag VMs with an auto-shutdown schedule."""
    try:
        schedule = ShutdownSchedule(
            at=parse_time(at),
            timezone=tz,
            days=parse_days(days),
            skip_until=date.fromisoformat(skip_until) if skip_until else None,
        )
    except (ScheduleError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    state = _state(ctx)
    targets = _select_targets(state, subscription, resource_group, name, tag, all_vms)
    results = apply_tag_changes(
        plan_apply(targets, schedule), state.providers.compute, dry_run=not apply
    )
    _emit(render([r.to_row() for r in results], fmt=fmt, table_columns=_ACTION_COLUMNS), None)
    _dry_run_notice(apply)


@schedule_app.command("remove")
def schedule_remove(
    ctx: typer.Context,
    subscription: SubscriptionOpt = None,
    resource_group: ResourceGroupOpt = None,
    name: NameOpt = None,
    tag: TagFilterOpt = None,
    all_vms: AllVmsOpt = False,
    apply: ApplyOpt = False,
    fmt: FormatOpt = OutputFormat.TABLE,
) -> None:
    """Remove the auto-shutdown tags from VMs."""
    state = _state(ctx)
    targets = _select_targets(state, subscription, resource_group, name, tag, all_vms)
    results = apply_tag_changes(plan_remove(targets), state.providers.compute, dry_run=not apply)
    _emit(render([r.to_row() for r in results], fmt=fmt, table_columns=_ACTION_COLUMNS), None)
    _dry_run_notice(apply)


@schedule_app.command("run")
def schedule_run(
    ctx: typer.Context,
    subscription: SubscriptionOpt = None,
    at: Annotated[
        str | None,
        typer.Option("--at", help="Evaluate as if it were this time (ISO 8601). Default: now."),
    ] = None,
    window_minutes: Annotated[
        int, typer.Option(min=1, help="Minutes after the shutdown time a VM is still due.")
    ] = 30,
    apply: ApplyOpt = False,
    fmt: FormatOpt = OutputFormat.TABLE,
) -> None:
    """Deallocate VMs whose shutdown time is now. The same logic the timer handler runs."""
    from finops.handler import run

    now = datetime.now(UTC)
    if at:
        try:
            now = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--at") from exc
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    providers = _state(ctx).providers
    results = run(
        providers.graph,
        providers.compute,
        now=now,
        dry_run=not apply,
        window=timedelta(minutes=window_minutes),
        subscriptions=subscription,
    )
    _note(f"Evaluated at {now.astimezone(UTC).isoformat(timespec='minutes')}\n")
    _emit(render([r.to_row() for r in results], fmt=fmt, table_columns=_ACTION_COLUMNS), None)
    _dry_run_notice(apply)


if __name__ == "__main__":
    app()
