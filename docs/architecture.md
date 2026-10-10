# Corpus Indomicus v1 architecture

v1 is an acquisition system for all historically available BPK records in the central and ministry/institution categories, with optional year bounds.

```text
scope
  -> finite discovery manifest
  -> persisted job items
  -> detail fetch
  -> metadata + neutral references
  -> file fetch + validation
  -> global content-addressed object store
  -> SQLite provenance / coverage / job state
```

## Finite snapshots

A backfill first discovers source IDs and persists them into `manifest_items`. The manifest becomes immutable after every partition reaches a verified page boundary. Its cutoff records observation time, not a server-side historical snapshot: BPK's mutable offset pagination can change during a crawl. Later sync and coverage comparisons remain necessary.

Discovery checkpoints are stored in `discovery_partitions`, keyed by run/category/type/year (zero means no year filter). Provider belongs to the run. `next_page`, the previous page signature, reported count and errors persist across interruption. Legacy `run_segments` remain readable. A fingerprint includes selected categories/types, year bounds and query, excluding volatile catalog counts/timestamps. Incomplete discovery cannot start ingestion.

## Three identities

- source record: `(provider, source_id)`
- legal instrument row: source-scoped ID; issuer-qualified cross-source candidate metadata is recorded when an issuer exists
- physical source object: SHA-256 of exact response bytes

None is substituted for another. The stored source-scoped instrument identity is `provider:source:<source_id>`; records from different providers are never merged solely on number and year.

## Storage

The object store is global by SHA-256. Compressible responses are losslessly gzip-compressed after hashing. PDF bytes are retained exactly in v1 to provide ground truth for v2 layout/OCR development.

Metadata, HTTP provenance, references, acquisition runs, segments and manifests are SQLite rows rather than per-object sidecar files.

Storage writes use an fsynced temporary file, durable receipt intent and atomic rename on the same filesystem. An archive-wide OS lock permits one acquisition writer and releases on process death. Receipt replay verifies bytes and restores object/provenance rows after a crash between rename and SQLite commit. Missing receipt objects remain retryable. Corrupt existing objects fail explicitly instead of being silently accepted.

Expected files are versioned by provider/source/detail hash. Each attachment must pass signature/EOF validation before `done`; missing metadata is `unresolved`, no links is `no_file`, and incomplete attachments are `partial`. The `/Read` viewer is retained in original HTML but excluded from expected downloads. Manifest and object verification use bounded keyset batches and short SQLite transactions.

`source_observations` retains the original deduplicated observation interface; `source_fetch_events` adds timestamped fetch history. Stored HTTP provenance includes request/final URLs, status, MIME, length, ETag and Last-Modified where available.

## Reference semantics

A document reference means only that the source page linked document A to document B. Nearby labels such as `Mengubah` are retained as raw evidence, not converted into a canonical legal relation in v1.

## v2 handoff

v2 may create a semantic document representation (AST/Markdown renderer, OCR fallback, layout preservation). Its representation must record the source PDF hash and pass explicit fidelity gates before a source PDF can become policy-evictable.
