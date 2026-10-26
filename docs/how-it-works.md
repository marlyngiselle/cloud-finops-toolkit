# How it works

The toolkit is a thin pipeline: **query** the estate, **evaluate** each record with plain Python rules, **render** a report, and, only for `schedule`, **act**. Everything that talks to Azure sits behind three small interfaces, so every rule is unit tested without a network connection.

## Architecture

```mermaid
flowchart LR
    subgraph CLI["finops CLI (typer)"]
        idle[idle]
        tags[tags]
        rightsize[rightsize]
        hb[hybrid-benefit]
        sched[schedule apply / remove / run]
    end

    subgraph Logic["Pure evaluation logic"]
        idleL[idle.py]
        tagsL[tags.py + YAML policy]
        rsL[rightsize.py]
        hbL[hybrid_benefit.py]
        schL[schedule.py]
    end

    subgraph Providers["Provider protocols"]
        G[ResourceGraphProvider]
        M[MetricsProvider]
        C[ComputeProvider]
    end

    subgraph Impl["Implementations"]
        AZ["azure.py<br/>DefaultAzureCredential"]
        DEMO["demo.py<br/>synthetic data"]
        FAKE["tests/conftest.py<br/>fakes"]
    end

    KQL[("queries/*.kql")]

    idle --> idleL
    tags --> tagsL
    rightsize --> rsL
    hb --> hbL
    sched --> schL

    idleL & tagsL & rsL & hbL & schL --> G
    rsL --> M
    rsL & schL --> C
    KQL -.loaded by name.-> G

    G & M & C --- AZ
    G & M & C --- DEMO
    G & M & C --- FAKE

    AZ --> ARG[(Azure Resource Graph)]
    AZ --> MON[(Azure Monitor metrics)]
    AZ --> CMP[(Compute API)]
```

| Interface | Azure implementation | Used by |
| --- | --- | --- |
| `ResourceGraphProvider.query(query, subscriptions)` | `azure-mgmt-resourcegraph`, paginated with `skip_token`, `objectArray` results | every command |
| `MetricsProvider.series(resource_id, metric, lookback, interval)` | `azure-monitor-query` `MetricsQueryClient.query_resource` | `rightsize` |
| `ComputeProvider.vm_memory_mb / set_vm_tags / deallocate_vm` | `azure-mgmt-compute` | `rightsize`, `schedule` |

Authentication is `DefaultAzureCredential`: Azure CLI login on a workstation, workload identity federation in CI, managed identity in a Function. The interactive browser flow is disabled so an unattended run fails fast instead of hanging.

## Why the split between KQL and Python

Resource Graph is excellent at **selecting** candidates across thousands of resources and many subscriptions in one call. It is a poor place for **business rules** that need tests and exceptions. So:

- `.kql` files select broadly (for example "every Windows VM and its `licenseType`").
- Python decides (for example "skip client OS images; skip VMs already on `Windows_Server`; skip anything tagged `finops-ignore=true`").

The KQL stays readable and portable to the portal, and the rules get unit tests.

## Command logic

### `idle`

Each category is one query plus a one-line description and a recommended action. Disk age comes from `LastOwnershipUpdateTime` (when the disk was last detached), falling back to `timeCreated`. `--min-age-days` hides freshly detached disks, which are often part of a running maintenance.

### `tags`

```mermaid
flowchart TD
    R[Resource row] --> S{Type in scope?}
    S -- no --> X[Skipped]
    S -- yes --> I{finops-ignore?}
    I -- yes --> X
    I -- no --> H{inherit_from_resource_group}
    H -- yes --> M[Merge resource group tags<br/>resource tags win]
    H -- no --> E
    M --> E[Evaluate each rule]
    E --> K1[missing]
    E --> K2[empty]
    E --> K3[not-allowed]
    E --> K4[pattern-mismatch]
    E --> K5[key-case]
```

ARM tag names are case-insensitive, so by default `Owner` satisfies a rule for `owner`. Turn on `case_sensitive_keys` when downstream tooling (cost exports, scripts) is case-sensitive and the spelling matters.

### `rightsize`

```mermaid
flowchart TD
    VM[Running VM] --> D{CPU data points<br/>>= 50% of expected?}
    D -- no --> INS[insufficient-data]
    D -- yes --> P[CPU pNN and memory pNN<br/>over the lookback]
    P --> IDLE{CPU < idle threshold<br/>and memory OK?}
    IDLE -- yes --> V1[idle: schedule or decommission]
    IDLE -- no --> DOWN{CPU < CPU threshold<br/>and memory OK?}
    DOWN -- yes --> V2[downsize: half the vCPUs<br/>in the same series]
    DOWN -- no --> V3[keep]
```

- **Percentile, not average.** A nightly batch at 90% CPU is invisible in an average and obvious in p95.
- **Memory** is derived from the platform metric `Available Memory Bytes` and the size's memory from the Compute API. When the size is unknown the decision is CPU-only and the note says so.
- **Suggested size** halves the vCPU count within the same series (`Standard_D8s_v5` to `Standard_D4s_v5`). Constrained-core and irregular sizes get no automatic suggestion. Regional availability and quota are always for a human to confirm.

### `hybrid-benefit`

| Workload | Finding when | Not a finding |
| --- | --- | --- |
| Windows VM | `licenseType` empty | `Windows_Server`, `Windows_Client`, client OS images |
| SQL Server on VM | `sqlServerLicenseType = PAYG` | `AHUB`, `DR`, Developer and Express editions |
| SQL Database, elastic pool, Managed Instance | `licenseType = LicenseIncluded` | `BasePrice`, serverless (`GP_S_*`), DTU model |

### `schedule`

```mermaid
sequenceDiagram
    participant T as Timer (every 15 min)
    participant H as finops.handler
    participant RG as Resource Graph
    participant VM as Compute API

    T->>H: trigger
    H->>RG: schedule/virtual-machines.kql
    RG-->>H: VMs with tags and power state
    loop each VM with finops-shutdown-time
        H->>H: parse tags, convert to UTC, check window and skip-until
        alt due and running or stopped
            alt FINOPS_DRY_RUN != false
                H->>H: log "dry-run"
            else
                H->>VM: begin_deallocate
                VM-->>H: done / error (logged, loop continues)
            end
        else not due, skipped, already deallocated, invalid tag
            H->>H: log decision
        end
    end
    H-->>T: exit code 1 if any deallocation failed
```

Details that matter in production:

- **Time zones** use the IANA database (`zoneinfo`). Comparisons happen in UTC, so the daylight-saving switch does not shift or double a shutdown.
- **Window** (`FINOPS_WINDOW_MINUTES`, default 30) must be at least the timer interval. A VM restarted by its owner after the window closes is left alone until the next day.
- **Midnight**: a 23:50 shutdown with a 30-minute window still fires at 00:10, because the previous day's occurrence is checked too.
- **Stopped is not off.** VMs in `PowerState/stopped` are deallocated as well, since they are still billed for compute.
- **One failure does not stop the run.** Each VM's error is captured in its result; the exit code reports that something failed.

## Testing approach

- Fakes in `tests/conftest.py` implement the three protocols in memory and record calls, so tests assert both the decision and the side effect (or its absence in dry-run).
- Pure functions (`percentile`, `suggest_smaller_size`, `parse_days`, `ShutdownSchedule.is_due`, `TagPolicy.evaluate`) are tested directly with edge cases: DST, midnight wrap, constrained sizes, case-insensitive keys.
- CLI tests run against `--demo` data and against injected fakes via `typer.testing.CliRunner`.
- The Azure adapter (`providers/azure.py`) is a thin mapping onto the SDKs and is excluded from coverage. It is exercised by real runs, not by mocks of the SDK.
