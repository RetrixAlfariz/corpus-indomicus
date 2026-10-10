# Corpus Indomicus

**Corpus Indomicus** is an acquisition-first toolkit for building a **source-traceable Indonesian legal-document corpus**.

> **Current scope: data gathering and preservation.** The `main` branch deliberately contains the tested acquisition foundation, not the previous L1/PDF-transformation experiments or the proposed CIL-IR storage engine. Any future storage architecture will be researched and validated separately before adoption.

## What this repository does

- Discover Indonesian legal records from **JDIH BPK** (the first supported connector).
- Freeze bounded discovery manifests for reproducible year-range backfills.
- Resume interrupted discovery/downloads through SQLite checkpoints.
- Maintain separate identities for **source record**, **legal instrument**, and **content object**.
- Store original PDF bytes and preserve source provenance and SHA-256 checksums.
- Deduplicate objects with identical bytes; losslessly gzip HTML/JSON/text.
- Verify document signatures, stored-object integrity, and acquisition status.
- Incrementally synchronize records and retain observations of revisions.
- Enforce a configurable free-disk safety reserve.

The project **does not yet** provide OCR, legal semantic interpretation, embeddings, RAG, production-ready legal document reconstruction, or a new compression engine.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --extra dev
uv run corpus-indomicus setup
uv run corpus-indomicus catalog types --groups pusat,lembaga
uv run corpus-indomicus plan --groups pusat,lembaga --all-years
uv run corpus-indomicus discover --groups pusat,lembaga --all-years
uv run corpus-indomicus ingest --run-id <ID> --limit 100
uv run corpus-indomicus status
uv run corpus-indomicus verify
```

New plans and discoveries default to all available years for the verified central and ministry/institution catalogs. `--all` aliases `--all-years` without a historical cutoff. Use `--from-year`/`--to-year` for a bounded range. The manifest freezes before ingestion; resume discovery with `discover --run-id <ID>` using its saved scope. `--limit` bounds ingestion while leaving remaining work pending. Setup and tests never start a full backfill.

The legacy `backfill` and `sync` commands remain available and use the same category-aware catalog. To collect changes after a backfill:

```bash
uv run corpus-indomicus sync
uv run corpus-indomicus references
uv run corpus-indomicus retry
```

For a fresh independent snapshot, use `backfill --fresh`. See `uv run corpus-indomicus --help` for complete options.

## Local data layout

```text
data/
  registry/
    corpus.db
  objects/
    00/
    01/
    ...
```

Raw PDFs are **retained as received** in this phase. The SHA-256 object store is the *baseline acquisition store*, not a claim that the long-term storage design is solved. HTML/JSON/text are losslessly gzip-compressed. Data and `corpus.toml` are intentionally not committed to Git.

`setup` creates `corpus.toml`, where `archive.min_free_gb` defaults to **100 GiB**. Review this safety reserve before starting collection on smaller disks.

## Project boundaries and next steps

1. **Now:** reliable, auditable legal-document collection, metadata, verification, and source coverage.
2. **Next:** acquisition quality, reproducible datasets, and source/provenance audits.
3. **Later:** a separately evaluated, better storage and compression design with explicit byte-exact archival tests and clearly labeled derived semantic representations.

Until a replacement has been independently validated, original source objects must **not** be deleted as a result of a derived representation or a compression benchmark.

For implementation details, see [CLI](docs/cli.md), [architecture](docs/architecture.md), [scope](docs/v1.md), [sources](docs/sources.md), [migration](docs/migrations.md), [validation](docs/validation.md), and [licensing](docs/licensing.md).

## Licensing

The MIT license applies to **this software only**. It does not relicense acquired government documents, third-party scans, logos, or metadata. Preserve attribution and respect each source's applicable terms.
