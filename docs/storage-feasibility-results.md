# Storage feasibility findings: frozen 1,000-PDF pilot

Dataset: 716 PMK and 284 PP PDFs, 4,830,705,402 source bytes and 60,014 pages. Measurements apply to this pilot rather than all Indonesian regulations. Original PDFs remain immutable and locally stored. Detailed inventories, receipts, rendered audit pages, charts and the reproducible research report are ignored under `reports/storage-feasibility-20261010/`.

## Main findings

- Encoded image streams account for 81.42% of source bytes. JBIG2 and JPEG-family streams dominate image storage; filter chains, dictionary dependencies and image usage must remain explicit.
- Encoded font payloads account for 7.59%. Their repeated byte-identical payload gross bound is 337,404,575 bytes (6.98% of source bytes), whereas image repetition is 11,740,429 bytes (0.243%). Font names alone do not establish equality.
- Two byte-distinct embedded payloads labeled Arial account for 332,601,068 bytes, or 90.72% of encoded font storage. Their physical payload occurrences are 538 and 43, across 533 and 43 PDFs respectively; equality within each group is established by encoded-payload SHA-256.
- Across all stream categories the repeated-payload gross bound is 420,177,620 bytes: 347,338,184 across PDFs and 72,839,436 within PDFs. These bounds omit index and reconstruction costs and cannot be added to generic-compression savings.
- The 45-PDF deterministic stratified sample provides 135 first/middle/last pages. An 18-page qualitative visual audit found wrong characters, numeric legal references, broken paragraph order, missing table-body text and missing chart topology. No corpus accuracy percentage is estimated. Selectable text is suitable as a derived search aid, not as a replacement for raster or original PDF bytes.
- Source-record year is not necessarily the regulation year printed in an attached PDF. One audited browser-print attachment shows a 2007 regulation inside the sample's 2022 source-record stratum. Provenance and content labels need separate treatment.

## Complete standard-codec baseline

These are the revalidated complete Phase 2 results on the same frozen sources, rather than a claim that the entire experiment was rerun during Phase 2.1. Shards target 128 MiB and include portable sidecar index bytes. All reconstructed PDFs passed exact SHA-256 checks.

| Method | Per-PDF saving | Shard saving | Per-PDF encode seconds | Per-PDF decode seconds |
|---|---:|---:|---:|---:|
| gzip 9 | 8.29% | 8.29% | 134.58 | 11.82 |
| zstd 3 | 9.38% | 11.05% | 14.81 | 4.63 |
| zstd 9 | 10.09% | 13.22% | 34.04 | 4.85 |
| zstd 19 | 10.96% | 15.23% | 708.51 | 6.15 |
| LZMA2, XZ preset 6 | 10.79% | 15.10% | 1,026.99 | 98.71 |

Ratios are weighted by input bytes. Decode timing includes checksum work. Single warm-cache local runs and sampled process RSS limit performance generalization. Per-PDF frames allow independent access; shard offsets refer to decoded bytes and require prefix decoding.

## Decision gate

1. Keep per-PDF zstd as the measured operational reference and test retrieval behavior before adopting it.
2. Evaluate cold-storage shard retrieval latency against its measured compression gain.
3. Prioritize exact repeated font/resource representation research, including dictionary, index, syntax and revision preservation costs. Raw stream equality does not demonstrate complete original-PDF reconstruction.
4. Test reversible image wrappers only on codec-specific candidates and retain encoded payloads. Pixel equality is not original-byte equality.
5. Retain text as a derived searchable layer alongside sources; do not remove source raster based on text extraction.

No new engine, CIL-IR, mass OCR, permanent PDF transformation or historical acquisition backfill is part of this phase. A new storage engine requires review of the research evidence first. Commands and output definitions are in [the feasibility method](corpus-storage-feasibility.md).
