# PDF storage and compression study

## Completed local snapshot (2026-09-16)

The current measured study is [reports/storage-study-20260916/STUDY_REPORT.md](../reports/storage-study-20260916/STUDY_REPORT.md).
It profiles the 10 already-downloaded local PDFs, without acquisition or re-OCR.
The earlier top-level empty tables and `local-study-20260916` preliminary benchmark
are historical artifacts, not the final results. The preliminary benchmark exposed
an old Lua-bundled 7z on PATH; the final benchmark prefers the installed Program
Files/7-Zip executable and records its version.

```powershell
uv run --locked --extra profile python -u -m corpus_indomicus.pdf_profile --input data --output reports/new-study --benchmark
uv run --locked --extra profile python scripts/summarize_storage_study.py reports/new-study
```

## Running the 2026 acquisition and study

```powershell
$env:PYTHONUTF8 = '1'
uv run --locked --extra profile --with truststore==0.10.4 python -u scripts/run_storage_study.py
```

This runner uses the Windows native certificate store with verification enabled,
discovers the BPK 2026 manifest, downloads it, retries incomplete records once,
verifies source object hashes, then runs the study. It resumes an unfinished
`storage-study` acquisition. Check `reports/2026-study/workflow_status.json` and
`workflow_events.jsonl`; final profiles go to `reports/2026-study/profile/`.
Year filtering means the legal document's year, not the date its PDF was uploaded.
The source is mutable: snapshot cutoff records the acquisition start, not a
historically frozen view guaranteed by the provider. Missing records remain gaps.

The study reads source PDFs without rewriting them. It does not run OCR, acquisition,
or master conversion. Point `--input` at the downloaded corpus directory (including
an existing `data/objects` store); PDF discovery is recursive and case insensitive.

```powershell
uv sync --locked --extra profile --extra dev
uv run --locked --extra profile python -m corpus_indomicus.pdf_profile --input "E:/path/to/dataset" --output reports --benchmark
uv run --locked --extra profile --extra dev pytest tests/test_pdf_profile.py tests/test_pdf_benchmark.py -q
```

Use a fresh output directory for each study. Profiling completes before compression
experiments begin. `--benchmark` is optional. An empty dataset writes empty inventory
tables and explicit `blocked_no_input_pdfs` status, then exits with code 2. It must
never be interpreted as a measured zero raster/duplicate ratio. Input hashes are
checked again after all experiments, including files which failed PDF parsing.

## Artifacts

| File | Contents |
| --- | --- |
| `pdf_profile.csv` | Original SHA-256, size, pages, classification, stream category bytes, parse errors |
| `page_profile.parquet` | Text length, invisible text, image coverage, vector count, flags, allocated bytes |
| `object_profile.parquet` | Active xrefs, stream SHA-256/length/filter, font programs and dictionary diagnostics |
| `image_profile.parquet` | Every rendered image occurrence, dimensions, colorspace, BPC, transformed DPI |
| `storage_summary.json` | Corpus/page distributions, codec bytes/counts/shares, duplicate streams/fonts |
| `input_manifest.json`, `original_integrity.json` | Input inventory and before/after hash verification |
| `run_provenance.json` | Command, platform, dependencies, profiling/benchmark source hashes |
| `benchmark.csv`, `benchmark_summary.json` | Per-PDF compression, verification, timing, failures, quantiles |
| `dedup_benchmark.csv`, `dedup_summary.json` | Exact container experiment, including manifest overhead |
| `benchmark_artifacts/` | Compressed files and explicitly derived structural PDFs |
| `dedup_container/` | Portable JSON manifests and content-addressed zstd blobs |
| `duplicate_groups.json` | Repeated encoded stream hashes and every document/xref reference |
| `validation_summary.json`, `STUDY_REPORT.md` | Reconciliations, integrity recheck, and interpreted results |
| `derived_preview/` | First/middle/last original and lossy derivative page previews |

## Measurement definitions

- **Page bytes:** both file size / page count and a separate allocated estimate.
  Referenced stream bytes are divided among using pages; unassigned streams and
  residual file bytes are distributed uniformly. This is not a standalone page size.
- **Composition:** raw encoded stream lengths from active xrefs are exact parser
  observations. Dictionaries, header, xref, trailer, prior revisions and unassigned
  physical bytes remain `structural_or_unaccounted_bytes`. Do not call this residual
  exact per-category object overhead. Negative residual marks invalid accounting.
  Unresolvable xref slots remain explicit warning rows and are counted in the
  summary; they can include free/deleted slots and are not silently assumed absent.
  All-stream codec byte shares are separate from image-only codec byte shares.
- **Fonts:** embedded program streams include decoded SHA-256 for comparison across
  different stream encodings; subset detection uses the six-letter name prefix.
  Names alone never establish font identity. Font byte share counts stream bytes.
- **Images:** image XObject bytes counted once per file. Each occurrence has its
  own placement/DPI. Coverage is clipped bounding-box union; masks, rotated/skewed
  placements, clipping paths and overpainting can overestimate visible coverage.
  Inline image bytes remain included in content streams. DPI is an estimate from
  pixel dimensions and placement; it is not proof of original scan resolution.
- **Classification:** at least 75% image coverage is raster-backed; with at least
  20 extracted non-whitespace-edge text characters it is a raster+OCR candidate.
  10–75% coverage is mixed; lower coverage is born-digital. Heterogeneous documents
  are mixed. A 90% individual image indicates full-page raster. Invisible text is
  stronger overlay evidence, not confirmed OCR provenance. A blank/vector-only
  page can fall into born-digital; manually audit a sample before interpreting shares.
- **Complexity:** at least 500 vector segments flags vector-heavy pages. Page size
  over 1,000 points, images over 4,000 pixels on either axis, or vector-heavy status
  flag possible large maps/tables. These are candidates, not semantic detections.
- **Dedup:** exact encoded-stream hashes are compared across file paths. Duplicate
  byte ratio excludes dictionaries and other overhead. Normalized dictionary hashes
  are diagnostics, not hashes of the original serialized object. Cross-document
  duplicate bytes count one instance per document, separate from within-file repeats.
  A content-addressed acquisition store may already remove entire duplicate PDFs;
  this study describes the physical files present, not duplicate download URLs.

## Experiments and interpretation

Original, gzip level 9, zstd level 9 and 7z/LZMA2 level 9 are measured per PDF.
Every reversible result requires reconstructed SHA-256 equality. 7z uses a new
temporary archive, with a 300-second timeout per invocation. Optional tools are
recorded with paths/versions. Missing JPEG-recompression/DjVu tools are skipped;
no theoretical saving is fabricated. No lossy conversion runs against masters.

PyMuPDF structural optimization is a **derived PDF**, even if rendering is identical.
All pages undergo 96-DPI render and extracted-text comparison. Passing that check
does not preserve signatures, all interactive behavior, metadata, or exact original
bytes. Decode time here measures opening the derivative; visual verification has
its own timer. No archival-exact claim is made unless original bytes match.

The `layered_200dpi` comparison explicitly scales image XObjects whose least
placement DPI is above 220 to approximately 200 DPI and replaces their payloads
with JPEG quality 75. Existing page content/text layers remain. All pages undergo
text comparison and grayscale rendering at 36 DPI; mean normalized pixel error is
diagnostic, not a quality acceptance threshold. First/middle/last previews per PDF
are saved at 96 DPI. This **lossy, non-archival** experiment may increase size when
converting efficient bilevel Fax streams to grayscale JPEG. It is deliberately
reported even when compression is worse. Inline images are not rewritten by this
adapter. No master bytes are changed.

The stream-addressed experiment partitions source bytes at exact matches to raw
active streams of at least 256 bytes. Unmatched spans stay opaque. Each chunk is
zstd-compressed and shared by SHA-256. Ordered manifests preserve every byte,
including PDF syntax and incremental revisions. Reconstruction uses persisted blobs
and manifests and verifies the original hash. Logical container size includes JSON
and frame overhead but excludes filesystem allocation/filename overhead. Shared
blob bytes are divided equally among using documents for per-file saving quantiles.

Restore a single document into a new path:

```powershell
uv run --locked --extra profile python -c "from pathlib import Path; from corpus_indomicus.pdf_dedup import reconstruct; reconstruct(Path('reports/dedup_container/0000000.json'), Path('restored.pdf'))"
```

Peak memory is currently null (not measured). Timings are one pass, affected by
cache/order, and include some file I/O for external methods; they are not precise
cross-tool CPU microbenchmarks. A single PDF and the accumulated profile tables are
held in memory. Large-dataset scalability must be assessed against the real corpus.

Compare corpus byte-weighted savings as well as per-file mean/median/P10/P90 and
best/worst. The custom-container decision depends on its incremental gain over
plain zstd/7z, manifest and operational costs, restoration latency, and error count.
No recommendation or Indonesian-corpus claim is supported without actual inputs.
