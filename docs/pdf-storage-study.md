# PDF storage and compression study

The measured 2026-09-16 snapshot is recorded in:

- `reports/storage-study-20260916/STUDY_REPORT.md`
- `reports/storage-study-20260916/storage_summary.json`
- `reports/storage-study-20260916/benchmark_summary.json`
- `reports/storage-study-20260916/dedup_summary.json`
- `reports/storage-study-20260916/input_manifest.json`
- `reports/storage-study-20260916/validation_summary.json`

The tracked report contains **10 PDFs, 728 pages and 46,684,304 source bytes**. It is a local study set, not a representative census of Indonesian legal PDFs.

## Repository policy

Raw study output is deliberately not tracked in Git. This includes benchmark payloads, reconstructed PDFs, `.gz/.zst/.7z` artifacts, dedup blobs/manifests, previews, Parquet/CSV profiles, logs, PID files and workflow state.

Use local output directories or short-lived CI artifacts for those files. Commit only reviewed compact summaries/manifests. See `reports/README.md` and `.gitignore`.

The preliminary `local-study-20260916`, acquisition logs and transient `reports/2026-study` state existed historically but were purged from the current tree after their useful conclusions were incorporated into the final report.

## Reproduce the profile

The study reads source PDFs without rewriting them and does not run OCR.

```powershell
uv sync --locked --extra profile --extra dev
uv run --locked --extra profile python -u -m corpus_indomicus.pdf_profile `
  --input data `
  --output reports/reproduction `
  --benchmark
uv run --locked --extra profile python scripts/summarize_storage_study.py reports/reproduction
```

Use a fresh output directory. Before comparing measurements, verify that the source SHA-256 values and sizes match `reports/storage-study-20260916/input_manifest.json`.

Tests:

```powershell
uv run --locked --extra profile --extra dev pytest `
  tests/test_pdf_profile.py tests/test_pdf_benchmark.py -q
```

## Measurement model

The profiler records:

- source SHA-256, size and page count;
- extracted text and image coverage;
- image dimensions, colorspace, BPC and estimated placement DPI;
- active PDF stream category/filter and encoded bytes;
- font inventory;
- exact encoded-stream duplicate hashes;
- heuristic page class (`born-digital`, `mixed`, `raster-backed`, `raster+OCR`).

Important limitations:

- image coverage uses clipped bounding boxes and is not semantic segmentation;
- estimated DPI is derived from placement, not proof of original scan resolution;
- active xref inspection is not a physical byte-offset parser, so dictionaries, xref/trailer data, prior revisions and other residual bytes remain separately accounted;
- raster+OCR is a heuristic, not proof of OCR provenance;
- map/table candidate flags are not semantic detection.

## Benchmark methods

The historical measured run compared:

- original bytes;
- gzip level 9;
- zstd level 9;
- 7z/LZMA2 level 9;
- a PyMuPDF structural derivative;
- an intentionally lossy 200-DPI/JPEG derivative;
- exact stream-addressed zstd container/dedup experiment.

Generic archival claims require reconstruction SHA-256 equality with the original source. Derived PDFs are never labeled archival-exact merely because they render similarly.

The final measured source-exact savings were approximately:

| Method | Aggregate saving |
|---|---:|
| gzip9 | 16.779% |
| zstd9 | 20.670% |
| 7z/LZMA2 | **24.237%** |
| stream-CAS + zstd | 17.195% |

The lossy 200-DPI/JPEG derivative increased corpus size because the source pages were predominantly efficient 1-bit CCITT raster. See the tracked final report for full distributions and caveats.

## Relationship to L1 / CIL-IR

This storage study established why generic PDF compression alone is not the main long-term path for the project. The subsequent L1 experiment measured structure-first representations, and the current design target is the AI-native CIL-IR described in `docs/cil-ir.md`.

The PDF study remains a baseline and source-characterization record rather than the canonical storage architecture.
