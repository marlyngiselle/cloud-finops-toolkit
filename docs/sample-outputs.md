# Sample outputs

Every output below was produced by running the CLI with `--demo`, which swaps the Azure providers for bundled **synthetic** data (`src/finops/providers/demo.py`). Names, subscription IDs (`00000000-0000-4000-8000-...`), and IP addresses (RFC 5737 documentation ranges) are invented. Run `make demo` to reproduce them.

Reports go to stdout and the one-line summary goes to stderr, so `-f csv` and `-f json` stay machine-readable when redirected.

## Idle and orphaned resources

```console
$ finops --demo idle
CATEGORY                NAME                  RESOURCE_GROUP  LOCATION    DETAIL                                                 AGE_DAYS
----------------------  --------------------  --------------  ----------  -----------------------------------------------------  --------
unattached-disk         disk-build-agent-old  rg-ci-dev       westeurope  256 GiB Premium_LRS                                    96
unattached-disk         disk-sqltest-data02   rg-data-test    westeurope  512 GiB StandardSSD_LRS                                41
unattached-disk         disk-temp-restore     rg-shop-dev     westeurope  128 GiB Standard_LRS                                   3
unassociated-public-ip  pip-old-appgw         rg-shop-prod    westeurope  Standard SKU, Static, 203.0.113.10
unassociated-public-ip  pip-vm-jump-01        rg-shop-dev     westeurope  Standard SKU, Static, 198.51.100.24
orphaned-nic            nic-etl-old-7731      rg-data-test    westeurope  not attached, private IP 10.30.2.9
orphaned-nic            nic-vm-jump-01        rg-shop-dev     westeurope  not attached, private IP 10.20.1.4, holds a public IP
empty-app-service-plan  asp-shop-staging      rg-shop-dev     westeurope  P1v3 (PremiumV3), 2 instance(s), 0 apps
stopped-vm              vm-report-batch       rg-data-test    westeurope  Standard_E8s_v5 stopped but still allocated

9 finding(s). empty-app-service-plan: 1, orphaned-nic: 2, stopped-vm: 1, unassociated-public-ip: 2, unattached-disk: 3
```

`disk-legal-hold-2025` exists in the demo data but is tagged `finops-ignore=true`, so it is not reported.

CSV carries every column, including the recommendation and the full resource ID:

```console
$ finops --demo idle --category unattached-disk --format csv
category,name,resource_group,location,detail,age_days,recommendation,subscription_id,resource_id
unattached-disk,disk-build-agent-old,rg-ci-dev,westeurope,256 GiB Premium_LRS,96,"Snapshot if the data matters, then delete the disk.",00000000-0000-4000-8000-000000000002,/subscriptions/00000000-0000-4000-8000-000000000002/resourceGroups/rg-ci-dev/providers/microsoft.compute/disks/disk-build-agent-old
unattached-disk,disk-sqltest-data02,rg-data-test,westeurope,512 GiB StandardSSD_LRS,41,"Snapshot if the data matters, then delete the disk.",00000000-0000-4000-8000-000000000002,/subscriptions/00000000-0000-4000-8000-000000000002/resourceGroups/rg-data-test/providers/microsoft.compute/disks/disk-sqltest-data02
unattached-disk,disk-temp-restore,rg-shop-dev,westeurope,128 GiB Standard_LRS,3,"Snapshot if the data matters, then delete the disk.",00000000-0000-4000-8000-000000000002,/subscriptions/00000000-0000-4000-8000-000000000002/resourceGroups/rg-shop-dev/providers/microsoft.compute/disks/disk-temp-restore

3 finding(s). unattached-disk: 3
```

## Tag compliance

Policy: [`examples/tag-policy.yaml`](../examples/tag-policy.yaml). `vm-shop-web-01` and `kv-shop-dev` are compliant because they inherit `owner`, `cost-center` and `environment` from their resource group.

```console
$ finops --demo tags --policy examples/tag-policy.yaml
NAME               TYPE                                              RESOURCE_GROUP  STATUS         VIOLATIONS
-----------------  ------------------------------------------------  --------------  -------------  ----------------------------------------------------------------
rg-ci-dev          microsoft.resources/subscriptions/resourcegroups  rg-ci-dev       non-compliant  owner: missing; cost-center: missing; environment: missing; a...
vm-build-agent-02  microsoft.compute/virtualmachines                 rg-ci-dev       non-compliant  owner: 'build' does not match ^[^@\s]+@example\.com$; cost-ce...
aks-data-test      microsoft.containerservice/managedclusters        rg-data-test    non-compliant  cost-center: 'CC-77' does not match ^CC-\d{4}$; environment: ...
rg-data-test       microsoft.resources/subscriptions/resourcegroups  rg-data-test    non-compliant  cost-center: missing; environment: 'qa' not in [dev, test, pr...
stdataraw001       microsoft.storage/storageaccounts                 rg-data-test    non-compliant  cost-center: missing; environment: 'qa' not in [dev, test, prod]
rg-shop-dev        microsoft.resources/subscriptions/resourcegroups  rg-shop-dev     non-compliant  application: missing
rg-shop-prod       microsoft.resources/subscriptions/resourcegroups  rg-shop-prod    non-compliant  application: missing
sql-shop-prod      microsoft.sql/servers                             rg-shop-prod    non-compliant  environment: 'production' not in [dev, test, prod]

2/10 resources compliant (20.0%). Violations by tag: application: 6, cost-center: 5, environment: 6, owner: 2
```

The table truncates long cells. JSON includes a summary block for dashboards:

```console
$ finops --demo tags --policy examples/tag-policy.yaml --format json | head -14
{
  "summary": {
    "evaluated": 10,
    "compliant": 2,
    "non_compliant": 8,
    "compliance_pct": 20.0,
    "violations_by_tag": {
      "application": 6,
      "cost-center": 5,
      "environment": 6,
      "owner": 2
    }
  },
  "items": [
```

## Rightsizing

Default: only actionable rows (`idle`, `downsize`).

```console
$ finops --demo rightsize
NAME               RESOURCE_GROUP  VM_SIZE           CPU_P95  MEM_P95  VERDICT   SUGGESTED_SIZE   NOTE
-----------------  --------------  ----------------  -------  -------  --------  ---------------  ----------------------------------------------------------------
vm-build-agent-02  rg-ci-dev       Standard_F16s_v2  2.7      11.9     idle                       near-idle: consider a shutdown schedule or decommissioning
vm-jump-01         rg-shop-dev     Standard_B4ms     8.0               downsize  Standard_B2ms    check regional availability before resizing; memory not measured
vm-shop-web-01     rg-shop-prod    Standard_D8s_v5   12.9     30.8     downsize  Standard_D4s_v5  check regional availability before resizing

6 running VM(s) evaluated over 14 days. downsize: 2, idle: 1, insufficient-data: 1, keep: 2
```

With `--all`, VMs to keep and VMs with too little data are shown too:

```console
$ finops --demo rightsize --all
NAME               RESOURCE_GROUP  VM_SIZE           CPU_P95  MEM_P95  VERDICT            SUGGESTED_SIZE   NOTE
-----------------  --------------  ----------------  -------  -------  -----------------  ---------------  ----------------------------------------------------------------
vm-build-agent-02  rg-ci-dev       Standard_F16s_v2  2.7      11.9     idle                                near-idle: consider a shutdown schedule or decommissioning
vm-jump-01         rg-shop-dev     Standard_B4ms     8.0               downsize           Standard_B2ms    check regional availability before resizing; memory not measured
vm-shop-web-01     rg-shop-prod    Standard_D8s_v5   12.9     30.8     downsize           Standard_D4s_v5  check regional availability before resizing
vm-new-api-01      rg-shop-dev     Standard_D4s_v5                     insufficient-data                   only 67 of 336 CPU data points
vm-data-etl        rg-data-test    Standard_E16s_v5  13.2     81.3     keep
vm-shop-web-02     rg-shop-prod    Standard_D8s_v5   65.4     57.5     keep

6 running VM(s) evaluated over 14 days. downsize: 2, idle: 1, insufficient-data: 1, keep: 2
```

## Azure Hybrid Benefit

```console
$ finops --demo hybrid-benefit
WORKLOAD              NAME                  RESOURCE_GROUP  SIZE                CURRENT_LICENSE
--------------------  --------------------  --------------  ------------------  --------------------
windows-vm            vm-erp-app-01         rg-erp-prod     Standard_D4s_v5     none (pay-as-you-go)
sql-vm                sqlvm-erp-01          rg-erp-prod     Enterprise          PAYG
sql-database          sql-shop-prod/orders  rg-shop-prod    GP_Gen5 (4 vCores)  LicenseIncluded
sql-managed-instance  sqlmi-data-prod       rg-data-prod    BC_Gen5 (8 vCores)  LicenseIncluded

4 workload(s) without Hybrid Benefit. Confirm license entitlement (Software Assurance) before enabling it.
```

## Auto-shutdown

Tag VMs (dry-run by default; `vm-shop-dev-02` keeps its owner's `skip-until`):

```console
$ finops --demo schedule apply --time 19:00 --tz Europe/Paris --days mon-fri --resource-group rg-shop-dev
NAME            RESOURCE_GROUP  ACTION  OUTCOME    DETAIL
--------------  --------------  ------  ---------  -----------------------------
vm-shop-dev-01  rg-shop-dev     tag     unchanged  no change
vm-shop-dev-02  rg-shop-dev     tag     dry-run    +finops-shutdown-days=mon-fri

Dry run: nothing was changed. Re-run with --apply to make these changes.
```

Selecting nothing is refused, so a typo cannot tag every VM in a subscription:

```console
$ finops --demo schedule apply --time 19:00
Invalid value: select VMs with --resource-group, --name or --tag (or
--all-vms to target all)
```

Evaluate the shutdown handler at a given instant (Tuesday 19:10 in Paris):

```console
$ finops --demo schedule run --at 2026-10-27T18:10:00Z
Evaluated at 2026-10-27T18:10+00:00

NAME             RESOURCE_GROUP  ACTION      OUTCOME              DETAIL
---------------  --------------  ----------  -------------------  -----------------------------------
vm-shop-dev-01   rg-shop-dev     deallocate  dry-run              19:00 Europe/Paris mon-fri
vm-ci-agent-03   rg-ci-dev       none        invalid-schedule     time must be HH:MM (24h), got '7pm'
vm-data-test-01  rg-data-test    none        already-deallocated  20:00 America/New_York mon-fri
vm-shop-dev-02   rg-shop-dev     none        skipped              skip-until 2026-10-31

Dry run: nothing was changed. Re-run with --apply to make these changes.
```

