# Corpus Indomicus

**Corpus Indomicus** is an acquisition and preservation toolkit for building a source-traceable archive of Indonesian legal documents.

> v1 is deliberately focused on data gathering. Search, OCR, embeddings, RAG, legal reasoning, and UI are out of scope until the archive layer is reliable.

## v1 goals

The acquisition pipeline is designed around five rules:

1. **Preserve the source bytes.** Raw downloads are immutable and content-addressed.
2. **Keep provenance.** Every observation records where it came from and when it was retrieved.
3. **Separate an instrument from its files.** One law may have several official or archival manifestations.
4. **Deduplicate safely.** SHA-256 detects identical bytes without pretending two differently-rendered PDFs are automatically different laws.
5. **Stay restartable.** The local registry is SQLite, so interrupted ingestion can resume without rediscovering everything from scratch.

## Current components

```text
src/corpus_indomicus/
├── acquisition.py       acquisition orchestration
├── cli.py               command-line entry point
├── integrity.py         SHA-256 + lightweight MIME detection
├── models.py            canonical instrument/source models
├── registry.py          SQLite registry
├── storage.py           immutable content-addressed raw archive
└── sources/
    ├── base.py          connector interface + HTTP client
    └── jdih_bpk.py      first source connector
```

The first connector targets the public **Database Peraturan JDIH BPK**. It is intentionally conservative: requests are rate-limited, retried on temporary failures, and source HTML is archived alongside document files.

## Quick start

Requires Python 3.11+.

```bash
uv sync --extra dev
uv run corpus-indomicus init
uv run corpus-indomicus status
```

Discover a small sample from JDIH BPK without downloading documents:

```bash
uv run corpus-indomicus bpk discover --year 2026 --limit 10
```

Ingest a small sample:

```bash
uv run corpus-indomicus bpk ingest --year 2026 --limit 10
```

Use `--query`, `--page`, `--delay`, and `--data-dir` to narrow or relocate a run:

```bash
uv run corpus-indomicus --data-dir ./data bpk ingest \
  --query "pelindungan data" \
  --year 2022 \
  --limit 5 \
  --delay 1.5
```

Start small. A national legal archive is not improved by turning the first run into an accidental denial-of-service benchmark.

## Local data layout

```text
data/
├── raw/
│   └── <provider>/<yyyy>/<mm>/<sha-prefix>/<sha256>.<ext>
└── registry/
    └── corpus.db
```

Raw files are not committed to Git. The repository stores code and schemas; collected source material stays in the local archive or another storage backend chosen by the operator.

## Identity model

A **legal instrument** is not the same object as a PDF.

```text
LegalInstrument
├── canonical metadata
├── status / dates / publication
├── relationships
└── SourceObservations
    ├── detail HTML from source A
    ├── PDF from source A
    └── PDF from source B
```

Canonical IDs are deterministic where enough metadata exists:

```text
ID:UU:2022:27
ID:PERATURAN_BPK:2026:2
ID_KOTA_MOJOKERTO:PERWALI:2026:6
```

Regional identity includes source location/jurisdiction so equal regulation numbers in different regions do not collide. Ambiguous or incomplete records remain source-addressable until they can be resolved. The acquisition layer does not invent missing legal facts.

## Licensing

The MIT license covers the **Corpus Indomicus software**. It does not automatically relicense legal documents, scans, metadata, logos, or other material retrieved from external sources. See [`docs/licensing.md`](docs/licensing.md).

## Development status

**v1 / acquisition foundation**

Implemented:

- immutable raw storage
- SHA-256 integrity hashes
- SQLite provenance registry
- canonical instrument model
- restartable source observations
- conservative HTTP client
- JDIH BPK discovery/detail/file ingestion
- CLI status and sample acquisition commands
- unit tests for the non-network core

Planned next:

- stronger BPK parser fixtures from representative document types
- Ditjen PP (`peraturan.go.id`) connector
- JDIHN discovery connector
- issuing-institution JDIH connectors
- cross-source identity resolution
- normalized metadata schema export
- resumable backfill jobs and checkpoints
