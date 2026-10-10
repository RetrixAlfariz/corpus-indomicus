# Acquisition pilot validation — 2026-10-10

Implementation covers source acquisition and preservation. No OCR, compression redesign, CIL-IR, embeddings or retrieval work was introduced. No full historical backfill was run.

## Local tests

`uv run --locked --extra dev pytest -q`: **53 passed** on Windows/Python 3.13. The GitHub Actions acquisition workflow runs the regression suite on Ubuntu/Python 3.12; its execution is checked after pushing the implementation. `git diff --check` is part of final local validation.

Coverage includes primary-card/reference isolation, both source categories, type-ID resolution, regional exclusion with institutional-name exceptions, unknown DOM diagnostics, pagination without skipped pages, conservative issuer/source identities, populated legacy migration, scope mismatch/volatile snapshot resume, cross-category deduplication, interrupted/replayed discovery, frozen manifest protection, bounded ingestion, archive writer locking, no-file/unresolved/partial acquisition, multiple attachments, unchanged-detail retry, HTML masquerading as PDF, truncated payloads, 429/5xx retries, robots policy, atomic-write interruption, receipt replay, exact PDF bytes and local CLI compatibility.

## Bounded live smoke

Live catalogs yielded **7 pusat types and 179 lembaga types**, with IDs obtained from their official `/Search?jenis=` links. No unresolved or regional catalog rows were selected in this observation.

Commands used a separate ignored directory, `data/smoke-20261010`, with a 1 GiB reserve, one-second pacing, two retries and a 30-second timeout:

```bash
uv run corpus-indomicus --config data/smoke-20261010/smoke.toml --data-dir data/smoke-20261010 discover --groups pusat,lembaga --types 13,27 --from-year 2025 --to-year 2025 --max-pages 3 --fresh --no-progress
uv run corpus-indomicus --config data/smoke-20261010/smoke.toml --data-dir data/smoke-20261010 ingest --run-id 3 --limit 5
uv run corpus-indomicus --config data/smoke-20261010/smoke.toml --data-dir data/smoke-20261010 status --run-id 3 --output data/smoke-20261010/coverage.json
uv run corpus-indomicus --config data/smoke-20261010/smoke.toml --data-dir data/smoke-20261010 verify
```

Run ID 3 is local smoke evidence, not a portable assumption. The final manifest has 19 distinct source records: 15 Inpres (type 13) and 4 Peraturan BPK (type 27), matching this observation's reported counts. Acquisition processed four institutional records and one central record: **5 done, 14 pending**, zero invalid files, failures or unresolved metadata. Its status correctly remains `partial` after the limit.

Three bounded iterations during implementation revisited the same five documents, preserving changing HTML observations and deduplicating PDF bytes. The final run links five valid PDFs totaling **14,365,852 bytes**; all iterations together contain five unique PDF objects and 15 HTML objects. Verification: **20 objects, zero bad, zero missing**. Global physical storage is 14,493,673 bytes. PDFs/SQLite/benchmark outputs remain ignored local evidence and were not committed.

## Discovery strategy sample

A separate bounded read-only benchmark paginated only type 27 across all years: **5 requests, 49 distinct primary records, source reports 49**, 8.360 seconds including pacing. Three year-specific first-page probes (2024/2025/2026) took 4.704 seconds and found 4/4/3 records respectively. Those three years do not cover historical inventory; this is a small strategy sample, not a throughput claim for all types. Per-type all-years discovery avoids issuing a request for every empty year/type combination. The live pagination exposed disabled `Next` links pointing to the final page; regression tests now prevent those links from skipping intermediate pages.

## Limits requiring operational review

- BPK has mutable offset pagination, so observation cutoffs cannot guarantee a server-side frozen historical inventory. Repeated sync, source-count comparisons and anomaly review remain necessary.
- Signature/EOF/MIME checks detect common wrong/truncated files; they do not prove full PDF structural validity or legal authenticity.
- Missing file links and unverifiable metadata remain explicit statuses. Complete historical coverage has not been demonstrated by this bounded smoke.
- Old instrument IDs/links are preserved. Earlier merges cannot be undone without re-observing original evidence. Cross-source instrument reconciliation remains conservative candidate metadata.
- Existing corrupt objects fail explicitly and remain preserved; automatic destructive repair is not implemented.

## Changed files

- `README.md`
- `docs/architecture.md`, `docs/sources.md`, `docs/v1.md`, `docs/migrations.md`, `docs/cli.md`, `docs/validation.md`
- `src/corpus_indomicus/acquisition.py`, `cli.py`, `config.py`, `integrity.py`, `models.py`, `registry.py`, `storage.py`
- `src/corpus_indomicus/sources/base.py`, `jdih_bpk.py`
- `tests/test_acquisition.py`, `test_acquisition_recovery.py`, `test_bpk_parser.py`, `test_cli.py`, `test_http.py`, `test_models.py`, `test_registry.py`, `test_storage.py`
- `tests/fixtures/bpk_category.html`, `bpk_search_references.html`
