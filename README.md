# NetApp ONTAP Aggregate Capacity & Efficiency Report

A CLI tool that connects to a NetApp ONTAP cluster over SSH, pulls volume,
aggregate, and storage-efficiency data, and produces a combined
capacity/efficiency report as a console table, a CSV file, and a JSON
payload.

## What it does

1. Prompts for a remote host/IP, username, password, and an environment
   tag (e.g. `prod`, `dev`, `stg` — defaults to `production`).
2. Opens an SSH connection (via `paramiko`) and runs three ONTAP CLI
   commands:
   - `volume show -fields vserver,volume,aggregate,size,available`
   - `aggr show -fields availsize,percent-used,size,usedsize`
   - `storage aggregate show-efficiency`
3. Parses and cross-references the three outputs per aggregate:
   - Physical aggregate size/used/available/%used
   - Total storage efficiency ratio and data reduction ratio (w/o snapshots)
   - Per-volume allocated/used/available space, rolled up per aggregate
   - Logical allocation % and logical used % relative to aggregate size
4. Writes out:
   - A formatted grid table to the terminal (via `tabulate`)
   - A CSV report: `aggr-capacity-efficiency-<remote_host>-<YYYYMMDD>.csv`
   - A JSON payload: `aggr-capacity-<environment>-<YYYYMMDD>.json`, containing
     per-aggregate stats plus a full list of parsed volumes

## Requirements

```bash
pip install paramiko tabulate
```

Python 3.7+ (uses f-strings and standard library `csv`/`json`/`getpass`).

## Usage

```bash
python3 get-aggr-capacity.py
```

By default you'll be prompted interactively for credentials. If the
`ONTAP_USER` and `ONTAP_PASSWORD` environment variables are set, the script uses
those instead and skips the corresponding prompt(s):

```bash
export ONTAP_USER="admin"
export ONTAP_PASSWORD="your-password"
python3 get-aggr-capacity.py
```

You'll still be prompted for:

| Prompt | Description |
|---|---|
| Remote host IP or hostname | The ONTAP cluster management LIF |
| Username | SSH login (e.g. `admin` or a domain-prefixed account) |
| Password | Entered securely via `getpass` (not echoed) |
| Environment identifier tag | Free-text label used in the JSON filename; defaults to `production` |

## Output columns

| Column | Meaning |
|---|---|
| Aggregate | Aggregate name |
| Total Vol Allocated (GB) | Sum of volume sizes on this aggregate |
| Total Vol Used (GB) | Sum of (volume size − available) across volumes |
| Total Vol Avail (GB) | Sum of volume-reported available space |
| Aggr Size (GB) | Physical aggregate size |
| Aggr Used (GB) | Physical aggregate used space |
| Aggr Avail (GB) | Physical aggregate available space |
| % Logically Alloc | Total volume allocation as % of aggregate size |
| % Logically Used | Total volume used space as % of aggregate size |
| Aggr % Used | ONTAP-reported physical % used |
| Total Efficiency | Storage efficiency ratio (e.g. `3.50:1`) |
| Data Reduction (w/o Snaps) | Data reduction ratio excluding snapshots |

## Notes / things to check before relying on this

- **SSH host keys are auto-accepted** (`paramiko.AutoAddPolicy()`) — fine
  for quick internal use, but worth tightening (e.g. `load_system_host_keys`)
  if this will run against production regularly.
- **Password is passed as a plaintext argument** to `ssh.connect()` — works,
  but consider key-based auth for anything automated/scheduled.
- **Volume-line parsing assumes a fixed field order** (`vserver, volume,
  aggregate, size, available` — with `aggregate`/`size`/`available` read
  from the *end* of the split line). This should hold for the `-fields`
  query used, but if you change the field list or a vserver/volume name
  contains whitespace, this parsing will break.
- **Aggregate-line parsing uses a regex split** (`\s{2,}|\s(?=\d)`), which
  expects exactly 5 fields in the order `name, availsize, percent-used,
  size, usedsize`. This is brittle if ONTAP output column order/spacing
  changes between versions.
- Any aggregate present in the efficiency or volume output but missing
  from the `aggr show` output is skipped with a warning, since it has no
  size to compute percentages against.
- Exits the whole script (`sys.exit(1)`) on any SSH failure — there's no
  retry or per-command error recovery.
- Stderr from each SSH command (`volume show`, `aggr show`,
  `storage aggregate show-efficiency`) is now logged as a warning if
  present, rather than discarded silently.
- The env var names are `ONTAP_USER`/`ONTAP_PASSWORD` (not `USER`/
  `PASSWORD`) specifically to avoid colliding with the `USER` variable
  most shells already set to your local login name.

## History

`volume-calc-capcity.py`, an earlier trimmed-down version of this script
(no efficiency metrics, vserver, environment tagging, or JSON export), has
been removed — its one useful addition (logging command stderr as a
warning) was folded into this script.