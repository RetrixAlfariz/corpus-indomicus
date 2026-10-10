# Corpus characterization research

The repository-local `tools.corpus_profile` package measures existing PDF bytes.
It does not modify acquisition, run OCR, rewrite PDFs, or implement a compressor.
Install the locked optional dependencies with `uv sync --locked --extra profile --extra dev`.

## Input and reproducibility

Use an inventory with `files`, each containing `sha256`, `path`, `logical_size`,
`stored_size`, and `sources` (source ID, detail/download URLs, regulation metadata).
Paths are only local locators; dataset identity hashes sorted SHA, bytes and
provenance, excluding absolute paths. Manifest/schema/profiler versions and runtime
versions identify the experiment. Generated reports and source data are Git-ignored.

```powershell
uv run --locked --extra profile python -u -m tools.corpus_profile.cli --inventory data/loadtest-1000-20261010/pdf-inventory.json --output reports/characterization-20261010 --workers 2 --limit 5
uv run --locked --extra profile python -u -m tools.corpus_profile.cli --inventory data/loadtest-1000-20261010/pdf-inventory.json --output reports/characterization-20261010 --workers 2
```

The first command validates a small sample. The second resumes the same output.
SQLite commits one file at a time; unfinished work is recomputed. A different
manifest/profiler version cannot silently reuse checkpoints. Failed files are
recorded, not silently excluded. To retry failed profiling after a fix, use a new
output/version. A changed source fails its inventory hash check. Final validation
checks all sources again, including those processed in earlier sessions.

Workers are constrained to 1-4 with at most that many files in flight. Default
maximum source file size is 1 GiB. Pages and streams are inspected one PDF at a
time; stream byte payloads are discarded after hashing. Table export batches 1000
records. SQLite/results keep metadata, not copies of PDFs. Parser-library allocations
can exceed source size; use one worker on constrained machines. No high-resolution
rendering is used during profiling.

Stream/dictionary hash grouping uses SQLite disk-backed temporary tables with a
32 MiB page-cache target. Page statistics retain numeric metadata arrays and
text-pattern hash groups, rather than image/PDF payloads.

## Measurements and their limits

Raw encoded stream bytes are grouped as image/font-program/content/other.
Residual is the signed difference from physical file size. Dictionary syntax,
xref data, obsolete incremental revisions and unresolved bytes are not equivalent
to removable waste. Free/deleted/unresolved xref slots remain explicit.
Font programs are located through `/FontFile`, `/FontFile2`, `/FontFile3` references.
`/Prev` presence is only a revision lower-bound indicator.

Image coverage is a clipped axis-aligned placement union, without rasterizing
images. Inline images/masks/rotation/occlusion may limit interpretation. Thresholds:
born >=20 text characters and <5% image coverage; raster >=70% and <20 characters;
raster with text >=70% and >=20 characters; mixed text and 5%-70% images; otherwise
unknown. Raster with selectable text is possible OCR, not verified OCR provenance.
Classes are heuristics. PDF parsing/repair and password restrictions are reported.

Stream SHA equality is byte-exact **encoded payload** equality; identical streams
may have different decode dictionaries. Deduplication potential is a gross upper
bound before metadata/container overhead. Normalized dictionary fingerprints are
not original-serialization equality. Text-pattern fingerprints replace digits,
normalize whitespace, and truncate lines: neither full text nor semantic identity.
Repeated fonts/images do not automatically justify a custom container.

Statistics use linear interpolated percentiles and population standard deviation.
File-weighted and byte-weighted results are distinct. Year/type counts refer to
PDF attachments. Size/page/class buckets form joint strata with deterministic seed
20261010 and equal allocation, which does not estimate population proportions.
Top 20 source-byte outliers are a separate group.

```powershell
uv run --locked --extra profile python -m tools.corpus_profile.audit --output reports/characterization-20261010 --max-pages 24
```

Review previews at 72 DPI and record findings in `visual_audit.json` referencing
SHA/page/class and source evidence. Keep pending pages distinguishable from reviewed
pages. A small visual sample cannot establish corpus-wide classifier accuracy.

## Byte-exact standard benchmark

```powershell
uv run --locked --extra profile python -u -m tools.corpus_profile.benchmark --inventory data/loadtest-1000-20261010/pdf-inventory.json --output reports/characterization-20261010/benchmarks/full --methods gzip9,zstd3,zstd9,zstd19,lzma2 --modes perdoc,shard --shard-bytes 134217728 --timeout-seconds 300 --max-rss-bytes 1073741824
```

Each unit runs in an isolated child. A 20ms RSS sampler and time/RSS caps limit
execution; resource limits, missing methods and failures are recorded. Original
bytes are streamed into temporary compressed files, then reconstructed and hashed.
Temporary directories are removed even after a killed worker. Successful units
retain reconstructed SHA receipts. Encode/decode timings exclude process startup;
decode includes checksum computation. Single trials and warm caches are limitations.

Gzip uses DEFLATE level 9, zstd levels 3/9/19, and LZMA2 uses XZ preset 6. Original
PDF is the ratio=1 baseline with no encode/decode operation. Shards concatenate
originals and include a portable uncompressed offset/hash sidecar in output bytes.
Sidecar offsets refer to **decoded** bytes; there is no compressed random-access
index. Shard access requires decoding preceding content. Do not compare a partial
method against full-corpus results without matching coverage.

Benchmark checkpoints include sorted membership/settings/runtime codec identity.
Use separate directories for different methods/modes or inputs. Interrupted final
JSONL fragments are discarded; completed rows are retained. `--max-docs` and
`--max-input-bytes` provide explicit bounded cohorts. Subset results must state
the actual sampled bytes and documents, not be extrapolated to the corpus.

```powershell
uv run --locked --extra profile python -m tools.corpus_profile.report --output reports/characterization-20261010
uv run --locked --extra profile python -m tools.corpus_profile.cli --inventory data/loadtest-1000-20261010/pdf-inventory.json --output reports/characterization-20261010 --validate-only
uv run --locked --extra profile --extra dev pytest -q tests
```

The report assembles Parquet profiles, source validation, explicit redundancy
limits, outliers, audit results, raw benchmark CSV/JSON and figures. Share only
non-sensitive statistics; never commit PDF bytes, inventories with absolute local
paths, database files, rendered source pages, or benchmark payloads.
