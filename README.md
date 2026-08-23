# Corpus Indomicus

**Corpus Indomicus** is a source-traceable acquisition and preservation toolkit for building an Indonesian legal corpus.

## v1 boundary

v1 is the **acquisition pilot**. Its recommended scope is **2025 to the current run cutoff**. It deliberately does not do OCR, layout reconstruction, semantic legal interpretation, embeddings, RAG, or legal reasoning. Those belong to v2+ after this pilot gives us real storage and document-complexity measurements.

### What v1 guarantees

- finite snapshot manifests: an `--all` or year-range run freezes what it discovered, then downloads that fixed manifest
- restartable discovery and ingestion through SQLite checkpoints
- a real progress bar backed by persisted manifest items
- exact source SHA-256 hashes
- global content-addressed deduplication across sources/URLs
- lossless gzip compression for HTML/JSON/text; PDFs remain exact source bytes
- MIME/signature validation so an HTML error page is not silently archived as a PDF
- source records distinct from canonical legal instruments and exact byte objects
- neutral document references: Corpus records that A links to B without assuming why
- retryable failed/partial items
- storage safety reserve and storage/PDF-size measurements
- integrity verification by decompressing/re-hashing stored objects

## Quick start

```powershell
uv sync --extra dev
uv run corpus-indomicus setup
uv run corpus-indomicus plan
uv run corpus-indomicus backfill
```

With the default config, `backfill` means **2025 through the current year** and freezes a finite manifest at the start of the run.

After that, normal maintenance is:

```powershell
uv run corpus-indomicus sync
```

`sync` uses an overlap window (default: current and previous year within the configured pilot boundary), rechecks known source records, and records new detail revisions when source bytes change.

## Acquisition scopes

```powershell
# Recommended v1 pilot: 2025 -> current
uv run corpus-indomicus backfill

# Explicit range
uv run corpus-indomicus backfill --from-year 2025 --to-year 2026

# Full historical source sweep, still finite at the run cutoff
uv run corpus-indomicus backfill --all

# Start a separate snapshot instead of resuming an incomplete matching run
uv run corpus-indomicus backfill --fresh
```

A full run does **not** chase records published after its manifest is frozen. They belong to the next `sync`.

## Operational commands

```powershell
uv run corpus-indomicus status
uv run corpus-indomicus references
uv run corpus-indomicus retry
uv run corpus-indomicus verify
```

Low-level BPK commands remain available for development:

```powershell
uv run corpus-indomicus bpk discover --year 2026 --limit 5
uv run corpus-indomicus bpk ingest --year 2026 --limit 5
```

## Storage model

```text
data/
├── registry/
│   └── corpus.db
└── objects/
    ├── 00/
    ├── 01/
    └── ...
```

The object store is global, not provider-specific. Identical bytes from BPK and another source can therefore point to the same physical object.

- PDFs: exact bytes, content-addressed by SHA-256
- HTML/JSON/text: SHA-256 is calculated on the exact source bytes, then those bytes are stored losslessly as `.gz`
- metadata/provenance/jobs/references: SQLite, not per-object JSON sidecars

This gives v2 a clean lineage model: a future semantic/layout representation can record `derived_from_sha256=<original PDF hash>`, be validated against the v1 pilot corpus, and only then make source blobs eligible for policy-based eviction.

## Configuration

`setup` creates `corpus.toml`:

```toml
[archive]
data_dir = "data"
pilot_from_year = 2025
sync_overlap_years = 1
min_free_gb = 100.0

[sources.jdih_bpk]
enabled = true
delay = 1.0
max_retries = 3
timeout = 30.0
```

## Identity model

Three identities are intentionally separate:

1. **Source record**: `(provider, source_id)` answers “have we seen this source entry?”
2. **Legal instrument**: e.g. `ID:UU:2022:27` answers “do we know this law?”
3. **Content object**: SHA-256 answers “do we already possess these exact bytes?”

The same source record may acquire a new content hash later. v1 keeps the observation rather than overwriting history.

## Licensing

The MIT license covers the **Corpus Indomicus software**. It does not automatically relicense legal documents, scans, metadata, logos, or other material retrieved from external sources. See `docs/licensing.md`.
