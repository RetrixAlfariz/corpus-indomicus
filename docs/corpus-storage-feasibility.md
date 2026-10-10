# Corpus storage feasibility benchmark

Phase 2.1 uses the existing 45-entry stratified sample in
`reports/characterization-20261010/sample_manifest.json`. The orchestration
tool resolves each sample SHA against the 1,000-document source inventory,
checks the original path size and SHA-256 with `tools.corpus_profile.profiler.digest_file`,
and writes a receipt before compression begins. A changed or missing source
fails closed.

Run the bounded experiment from the repository root with:

```powershell
uv run --locked --extra profile python -m tools.corpus_profile.feasibility_benchmark `
  --source-inventory data/loadtest-1000-20261010/pdf-inventory.json `
  --sample-manifest reports/characterization-20261010/sample_manifest.json `
  --full-benchmark-dir reports/characterization-20261010/benchmarks `
  --output reports/storage-feasibility-20261010
```

The default run benchmarks `perdoc` and `shard` units using `gzip9`, `zstd3`,
`zstd9`, `zstd19`, and `lzma2`. Shards are bounded at 128 MiB; each unit has a
300-second timeout and a 1 GiB RSS limit. Results are resumable through the
existing benchmark checkpoint files and are written under
`reports/storage-feasibility-20261010/benchmarks/sample`.

The output also contains `sample_inventory.json`, `sample_receipt.json`, and
`feasibility_summary.json`. `full_benchmark_reference.json` records the
identity key, compatibility check, and status counts for the completed full
per-document and full-shard benchmarks. It references those existing results
without copying their JSONL/CSV payloads or rerunning compatible work.

The experiment validates exact reconstruction of every benchmark unit and
rechecks every original sample after the run. Compression ratios and timings
are machine- and codec-version-specific; the summary records the runtime
versions and bounded settings used for interpretation. The benchmark is
read-only with respect to the source PDFs and does not download or install an
engine.

For the 2026-10-10 run, the sample receipt verified all 45/45 original PDFs.
The benchmark produced 240 rows (45 per-document plus 3 shards, across five
codecs), with 240/240 exact receipts and full unit coverage. The sample contains
309,544,652 unique source bytes; each method processes those same bytes once
in each mode. Do not add the modes and describe that sum as dataset size.
Per-mode results are in the generated compression comparison table.
Existing full-per-document (5,000 rows) and full-shard
(195 rows) results were reused only after all 1,000 source identities and row
unit memberships validated against the compatible metadata; no full rerun was
performed.

The machine-readable validation schemas are `phase2.1-sample-receipt/v1`,
`phase2.1-feasibility-summary/v1`, and the benchmark validator output
`benchmark_validation.json`. The latter checks duplicate rows, expected
`doc:<sha256>` IDs, shard membership order, mode, input bytes, manifest and
payload accounting, independent-access flags, status, exactness, and complete
coverage.

## Image/font/object investigation

Reuse the verified Phase 2 object hashes and independently read every encoded
stream, rejecting a mismatch. The stream inventory resolves embedded font
payloads through FontDescriptor and DescendantFonts. Font resource aliases are
counted separately from physical payload xrefs, preventing inflated deduplication
capacity. SQL grouping is disk-backed with a 32 MiB SQLite cache target.

```powershell
uv run --locked --extra profile python -u -m tools.corpus_profile.storage_streams `
  --manifest reports/characterization-20261010/dataset_manifest.json `
  --characterization reports/characterization-20261010 `
  --output reports/storage-feasibility-20261010
```

One PDF is read at a time with a 1 GiB input-file cap; source size/SHA is checked
before/after. Checkpoints include the dataset fingerprint and stream-profiler
version. A small pilot can use `--limit 5`; compatible complete file checkpoints
are reused, and changed sources are rejected. Derived metadata from a different
version must use another output directory. Per-file failures remain explicit.

Page placements are matched to resource xrefs by uniquely identifying
width/height/bit-depth, avoiding decoded image hashing. Same-dimension aliases
remain ambiguous. Rotation-corrected individual image bboxes covering >=70%
of page area are flagged as possible full-page scans; this ignores masks,
transparency, clipping and unions of multiple placements. Resource listings are
not automatically counted as uses. Inline placements without an identifiable
xref are outside the per-object usage counts.

The output includes `storage-feasibility.sqlite` (images, fonts, objects,
checkpoints), `storage-feasibility-summary.json`, and
`duplicate_stream_references.jsonl`. Duplicate accounting groups each physical
stream xref once. The actual UTF-8 bytes of proposed portable references are
measured separately from the gross payload capacity. This does not specify
byte offsets, original syntax/revision maps or a full container and therefore
does not establish net PDF storage savings or a working reversible transform.

Image wrapping trials use zstd9 on the first three distinct encoded payloads
per terminal filter in SHA/xref order, each <=16 MiB, restoring the raw payload
and checking SHA plus byte equality. Large excluded streams remain in the
inventory. Font similarity compares at most ten same-normalized-name pairs
ranked by resource-reference bytes (a candidate-selection rank, not storage),
each encoded payload <=1 MiB, by size ratio and aligned 1 KiB chunk-hash Jaccard.
Those comparisons describe binary encoding only, not glyph/semantic identity.
Decoded image/font hashes are not computed; names alone imply no byte equality.

## Text fidelity and final report

```powershell
uv run --locked --extra profile python -m tools.corpus_profile.fidelity `
  --profile reports/characterization-20261010 `
  --output reports/storage-feasibility-20261010 --max-pages 18
```

This writes `text_sample.jsonl` with the 135 first/middle/last-page text records
from the 45-PDF stratified sample and `fidelity_plan.json` with deliberate audit
candidates/previews. Automated candidate tags are not verified table/diagram
labels. The plan is **pending** until the selected pages are visually compared
with the saved selectable text; preserve observations and source/page identities
in `text_fidelity_audit.json` with `review_status: reviewed`, reviewer,
ground-truth status and sampling limits. Never mark a plan reviewed merely by
running extraction. Rendering is capped at 120 DPI and 1600 px; no OCR runs.
The audit transcribes selected tokens only and gives no accuracy percentage.

After human/model visual review and all experiments, validate sources again
and generate the integrated report:

```powershell
uv run --locked --extra profile python -m tools.corpus_profile.cli `
  --inventory data/loadtest-1000-20261010/pdf-inventory.json `
  --output reports/storage-feasibility-20261010 --validate-only
uv run --locked --extra profile python -m tools.corpus_profile.feasibility_report `
  --profile reports/characterization-20261010 `
  --output reports/storage-feasibility-20261010
```

The report generator requires matching complete stream coverage, reviewed
fidelity evidence, complete sample/full-run benchmark validation and successful
final source verification. Outputs: `RESEARCH_REPORT.md`,
`compression_comparison.csv`, `research_evidence.json`, and standalone PNG/SVG
figures. Source-record year is not assumed to be attachment-content year.
All generated data and local locators remain ignored in `reports/`; only the
tooling, tests, methods and non-sensitive findings are committed.
