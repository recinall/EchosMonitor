---
name: miniseed-sds
description: MiniSEED/SDS archive conventions — session-rooted layout, SDS path grammar, midnight split, STEIM2/FLOAT32 encoding rules, crash recovery, DB-after-fsync ordering. Consult before touching storage/ (sds.py, mseed_writer.py, archive_reader.py, dao.py, db.py), the session model, or the Archive tab's data access; code comments reference this skill by name.
---

# MiniSEED / SDS conventions (EchosMonitor)

## Layout (rule 14 — session-rooted)

```
<archive_root>/<sanitized_project_name>/<sanitized_device_name>/
    {year}/{net}/{sta}/{cha}.D/{net}.{sta}.{loc}.{cha}.D.{year}.{doy}
```

- Project + device segments use the same sanitiser
  (`[A-Za-z0-9._-]`, underscores collapsed, `.`/`_` stripped at ends,
  sha1-fallback for empty); the sanitiser is NOT injective → injectivity is
  enforced at config/session-creation time (reject colliding names loudly).
- Empty location code renders as `..` in the filename (SDS canonical).
- `events/` (if ever reintroduced) and `archive.db` sit beside the SDS tree,
  never inside `YEAR/…`. One `archive.db` per session root.

## Day boundary
Half-open day `[00:00, next 00:00)`. A trace straddling UTC midnight is
split; a trace ENDING exactly at midnight stays whole (strict `>` test).
Split before encoding, never after.

## Encoding
- STEIM2 preferred, requires int32 (range-checked cast; out-of-range int64 →
  encoding error, not silent clamp). Float dtype under a STEIM config →
  downgrade to FLOAT32 with ONE info log per stream; report the encoding
  actually written. Record length 512 default, big-endian, `flush=True`.

## Write discipline (rule 8)
- Append-only fds, LRU-capped open-file set (evict ⇒ fsync+close).
- First session-touch of an existing file: truncate any tail not aligned to
  `record_length` (torn-write recovery), once per path per session.
- Periodic fsync timer (`fsync_interval_s`); `flushedFile` fires AFTER fsync
  with BOTH `bytes_added` (delta → `streams.total_bytes`) AND `os.fstat`
  size (cumulative → `files.bytes` UPSERT) — conflating them broke
  cross-session truth once already.
- DB rows only after fsync; `files.path` UNIQUE with update-in-place;
  ISO-8601 UTC strings everywhere (lexicographic = chronological).
- Slow-IO guard: >1 s write/fsync warns; 3 consecutive → pause path 30 s;
  ENOSPC → pause for the session.

## Reading
- `ArchiveReader` is strictly read-only; index-backed file discovery
  (`files_in_range`) UNION a day-by-day canonical-path scan (≤400 days cap).
- `merge(method=0)` — gaps stay explicit (masked); callers decide. Windows
  for science (HVSR, deconvolution) REJECT masked gaps rather than filling.
- A re-indexer rebuilds the DB from paths via `parse_sds_path` (the
  per-device and per-project segments sit ABOVE the parsed five components —
  read them from the path).
