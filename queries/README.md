# Resource Graph queries

Every finding the toolkit reports starts with one of these [Azure Resource Graph](https://learn.microsoft.com/azure/governance/resource-graph/overview) queries. They are kept as plain `.kql` files so they can be reviewed on their own, pasted into the Resource Graph Explorer in the Azure portal, or reused in workbooks and alerts.

Conventions:

- The `//` header of each file explains what the query finds and why it costs money. Comment lines are stripped before the query is sent.
- Every query projects the same base columns (`id`, `name`, `type`, `resourceGroup`, `subscriptionId`, `location`, `tags`) plus the fields its evaluation needs.
- Queries select candidates broadly. Business rules (exclusion tags, thresholds, license edge cases) live in Python, where they are unit tested.

| File | Used by | Finds |
| --- | --- | --- |
| `idle/unattached-disks.kql` | `finops idle` | Managed disks in state `Unattached` |
| `idle/unassociated-public-ips.kql` | `finops idle` | Public IPs bound to no IP configuration or NAT gateway |
| `idle/orphaned-nics.kql` | `finops idle` | NICs with no VM, private endpoint or Private Link service |
| `idle/empty-app-service-plans.kql` | `finops idle` | Paid App Service plans with zero apps |
| `idle/stopped-vms.kql` | `finops idle` | VMs in `PowerState/stopped` (still billed) |
| `tags/resources-and-groups.kql` | `finops tags` | All resources and resource groups with their tags |
| `rightsize/running-vms.kql` | `finops rightsize` | Running VMs to evaluate against Azure Monitor metrics |
| `schedule/virtual-machines.kql` | `finops schedule` | All VMs with tags and power state |
| `hybrid-benefit/windows-vms.kql` | `finops hybrid-benefit` | Windows VMs and their `licenseType` |
| `hybrid-benefit/sql-vms.kql` | `finops hybrid-benefit` | SQL Server on VMs and their license model |
| `hybrid-benefit/sql-paas.kql` | `finops hybrid-benefit` | vCore SQL Database, elastic pools, Managed Instances |

## Running a query by hand

```bash
az graph query -q "$(grep -v '^//' queries/idle/unattached-disks.kql)" --first 1000 -o table
```

The `az graph` command needs the `resource-graph` CLI extension (`az extension add --name resource-graph`). Resource Graph only returns resources the caller can read, so run it with an identity that has Reader on the scopes you care about.
