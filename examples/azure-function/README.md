# Scheduled shutdown as an Azure Function

A minimal timer-triggered Function that runs `finops.handler.main` every 15 minutes.

| Setting | Value |
| --- | --- |
| Identity | System-assigned managed identity |
| Roles | `Reader` (Resource Graph) and `Virtual Machine Contributor` on the scopes it manages |
| `FINOPS_DRY_RUN` | `true` at first. Set to `false` once the logs look right |
| `FINOPS_WINDOW_MINUTES` | `30` (must be at least the timer interval) |
| `FINOPS_SUBSCRIPTIONS` | Optional, comma-separated subscription IDs |

Rollout that avoids surprises:

1. Deploy with `FINOPS_DRY_RUN=true` and tag two or three dev VMs.
2. Read a few days of logs (one JSON line per VM and decision).
3. Announce the convention to VM owners, including the `finops-shutdown-skip-until` opt-out.
4. Flip `FINOPS_DRY_RUN` to `false`.

The same handler runs anywhere Python runs: `python -m finops.handler` from cron, a container job or a pipeline schedule.
