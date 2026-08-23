# Corpus Indomicus v1 architecture

v1 is a bounded acquisition pilot, recommended for 2025 through the current snapshot cutoff.

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

A backfill first discovers source IDs and persists them into `manifest_items`. The manifest is then frozen. Documents published after discovery are deliberately excluded from that run and are picked up by a later `sync`.

Discovery checkpoints are stored per year in `run_segments`, so interrupted discovery can continue from its last page instead of restarting the historical range.

## Three identities

- source record: `(provider, source_id)`
- canonical instrument candidate: jurisdiction + type + year + number
- physical source object: SHA-256 of exact response bytes

None is substituted for another.

## Storage

The object store is global by SHA-256. Compressible responses are losslessly gzip-compressed after hashing. PDF bytes are retained exactly in v1 to provide ground truth for v2 layout/OCR development.

Metadata, HTTP provenance, references, acquisition runs, segments and manifests are SQLite rows rather than per-object sidecar files.

## Reference semantics

A document reference means only that the source page linked document A to document B. Nearby labels such as `Mengubah` are retained as raw evidence, not converted into a canonical legal relation in v1.

## v2 handoff

v2 may create a semantic document representation (AST/Markdown renderer, OCR fallback, layout preservation). Its representation must record the source PDF hash and pass explicit fidelity gates before a source PDF can become policy-evictable.
