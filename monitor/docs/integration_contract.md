# ATCS integration contract (schema version 1.0)

ATCS Monitor is a standalone program. **V1 has no network connection to the ATCS
dashboard or backend.** This document defines the JSON documents a future
integration can exchange, so the two projects can be connected later without
changing the monitor's internals.

Implementation: `app/integration/schemas.py` (builders + validators, no I/O).
Tests: `tests/test_storage_export.py` (`test_snapshot_*`, `test_session_summary_*`).

## Conventions

| Item | Rule |
|---|---|
| Encoding | UTF-8 JSON |
| `schema_version` | `"1.0"`. Consumers must reject unknown major versions. |
| Timestamps | ISO 8601 **with a time-zone offset**, e.g. `2026-10-23T12:00:00.000+00:00` (the monitor writes UTC). |
| Class keys | Always all six, in this order: `person`, `car`, `motorcycle`, `bicycle`, `bus`, `truck`. Values are non-negative integers. |
| `source` | `"camera"` or `"video_file"` |
| Directions | `"a_to_b"` / `"b_to_a"`. Side **A** is the right-hand side when walking from the line's P1 to P2 on screen. |

## 1. Live snapshot (`kind` omitted)

A point-in-time view. It can be produced from `MonitoringEngine.latest_snapshot()`
with `schemas.build_snapshot(...)`.

```json
{
  "schema_version": "1.0",
  "session_id": "3f1c0d6e9a2b4c51a7e8f00112233445",
  "timestamp": "2026-10-23T12:00:00.000+00:00",
  "source": "camera",
  "visible":   {"person": 2, "car": 3, "motorcycle": 0, "bicycle": 1, "bus": 0, "truck": 0},
  "crossings": {"person": 4, "car": 9, "motorcycle": 1, "bicycle": 2, "bus": 0, "truck": 0}
}
```

*This example illustrates the format only; the values are not measured data.*

* `visible`: objects in the current frame.
* `crossings`: line crossings since the session started, summed over all lines and both directions.

## 2. Session summary (`kind: "session_summary"`)

This is the same document the app writes with **History → Export JSON** and
`scripts/run_video_pipeline.py`. It is built by `schemas.session_summary(session, crossing_events)`.

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | `"1.0"` |
| `kind` | string | `"session_summary"` |
| `session_id` | string | 32-hex id, unique per session |
| `source`, `source_id` | string | `camera` + camera name, or `video_file` + file name |
| `started_at`, `ended_at` | ISO 8601 / null | session bounds |
| `status` | string | `completed`, `interrupted` (app was killed), `error`, or `running` |
| `model` | object | `name`, `version` (file + sha256 prefix + ultralytics version), `device` |
| `duration_s`, `frames_processed`, `avg_inference_fps` | number | processing statistics |
| `unique` | counts | confirmed tracker IDs per class (each ID once) |
| `crossings` | counts | crossing totals per class (all lines, both directions) |
| `line_totals` | object | `{line_id: {"a_to_b": counts, "b_to_a": counts}}` |
| `recordings` | list | `{file, ok, duration_s, codec}`; file name only, no local paths |
| `crossing_events` | list | `{timestamp, line_id, track_id, class, direction}` |

Validation guarantees:

* every class is present and every count is a non-negative integer
* timestamps carry a time zone
* for finished sessions, `crossings` equals the per-class tally of `crossing_events`; both come from the same SQLite transaction

## 3. Python interface

```python
from app.integration import schemas

doc = schemas.build_snapshot(session_id, timestamp_iso, "camera", visible, crossings)
text = schemas.to_json(doc)
again = schemas.snapshot_from_json(text)       # raises schemas.ContractError if invalid
schemas.validate_session_summary(exported_doc)  # raises ContractError
```

## 4. Suggested future integration (not implemented)

1. Periodically (e.g. every 5 s), the monitor writes a snapshot. When a session ends it writes the
   session summary. Both could go to a drop folder or a local HTTP endpoint owned by the ATCS backend.
2. ATCS validates `schema_version`, then stores the data keyed by `session_id`. It upserts snapshots
   and inserts each summary once.
3. Crossing events are idempotent on `(session_id, line_id, track_id, direction)`. The monitor counts
   each track at most once per line per direction, so this key is unique.

Any change that breaks these rules requires `schema_version` `"2.0"`.
