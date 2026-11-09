# Station history for dashboards

The station is the only writer of `runtime_data/apl/station.sqlite3`. A dashboard
on the same host can open it read-only (`file:/absolute/path/station.sqlite3?mode=ro`).
Keep queries short and paginated. A remote dashboard should use a station API,
rather than opening SQLite through a network share. WAL remains enabled and the
station retains `synchronous=FULL` durability.

## Upgrade

Stop the station before deploying this version. Back up the database using
SQLite's backup API/tool (a live WAL database must not be backed up by copying
only the `.sqlite3` file). Start the station; schema version 1 migrates existing
rows transactionally on the first start. Later starts skip the migration.
Evidence files are preserved and are not rewritten. Do not run an older station
binary against the upgraded database: older inserts use positional column lists.

`cycles` adds `mcio_sn`, `sn_scanned_at`, `mcio_scanned_at`, and `completed_at`.
Existing MCIO and completion values are recovered from `detail_json`. Scan times
are recovered from accepted scan events; product creation time is the fallback
for the initial product scan. Missing MCIO information remains NULL.

`attempts` adds `attempt_no`, `judged_at`, `judge_pass`, and `capture_trigger`.
Historical attempts are numbered by capture time, with row insertion order as
the tie breaker. Historical judgment time remains NULL: capture time is not
assumed to be the exact judgment time. Existing automatic metadata recovers
`AUTO`, explicit false recovers `MANUAL`, and absent metadata becomes `UNKNOWN`.
The evidence JSON schema remains version 2 with additive fields for new captures.

## Write workflow

1. An accepted product scan inserts a new cycle and scan event. Rejected scans
   and duplicate label notifications do not create extra cycles.
2. An accepted MCIO scan updates that cycle's MCIO fields and inserts an event.
3. A saved capture records its capture timestamp before matching, and its
   judgment timestamp after matching. Preview/probe frames do not create rows.
4. Raw/result JPEGs and JSON are saved first. One short database transaction then
   inserts an attempt, updates the cycle result/state, and inserts its judgment
   event. A failure rolls back those SQL writes and removes the new evidence.
   A process crash between file creation and SQL commit can leave unreferenced
   files, but cannot expose a partially committed attempt/cycle judgment.
5. Retry captures append attempts to the same cycle, starting at 1 for the first
   capture, including automatic capture. Termination preserves all attempts.
   A later inspection of the same SN creates a different cycle.
6. Upload requests/acknowledgments continue to use `outbox` and `events`.
   Upload retries never increase the capture count.

## Dashboard interface

Read the `cycle_history` view for one row per cycle:

```sql
SELECT cycle_id, sn, mcio_sn, sn_scanned_at, mcio_scanned_at,
       visual_result, judge_pass, judged_at, cycle_status,
       source_mode, capture_trigger, attempt_count, updated_at, upload_status
FROM cycle_history
ORDER BY updated_at DESC, cycle_id DESC
LIMIT 100 OFFSET 0;
```

All times are UTC Unix seconds stored as REAL; convert them to the display time
zone in the dashboard. `updated_at` is the latest cycle write, not the judgment
time. `completed_at` records the successful judgment, not an upload acknowledgment.

The latest saved attempt supplies visual result, judgment, source and trigger.
The count includes **all** captures for that cycle, even when filtering history
by the latest visual result. With zero attempts the view returns
`NOT_INSPECTED`, count 0, and NULL judgment/source values.

`judge_pass` is 1 for `OK` or `TIMEOUT_PASS`, and 0 for `NOT_MATCHED`. Keep
`visual_result` visible: a timeout-triggered pass is not a visual match.
`upload_status` is the latest outbox status (`PENDING` or `ACKED`), or `NOT_SENT`
when no upload request exists. In local mode a COMPLETE cycle can be NOT_SENT;
COMPLETE does not by itself prove an SFIS acknowledgment.

Indexes cover cycle SN/time, history ordering, per-cycle capture numbering,
per-cycle scan events and upload lookup. The count is derived, not duplicated in
`cycles`. The existing history API uses this view and retains its `state` field
as an alias of `cycle_status`.
