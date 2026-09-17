# L1 structure-first: measured study record

Date: 17 September 2026. Prototype: 0.2.0.
Status: **complete_requires_quality_review**.

This document preserves the durable conclusions of the L1 experiment. Raw PDFs, benchmark payloads, reconstructed packages, previews, Parquet/CSV tables and temporary logs are intentionally **not** kept in the current Git tree. See `reports/README.md`.

## Input and verification

The measured set contains **10 PDFs, 728 pages, 46,684,304 bytes**. The study used exact source bytes whose SHA-256 and sizes are recorded in `reports/storage-study-20260916/input_manifest.json`.

The historical full-corpus CI run was GitHub Actions run `35218274725`. Local and CI runs each passed **43 tests** and produced matching package hashes. Source PDF hashes remained unchanged and conversion errors were zero.

The committed binary `.pdf.gz` benchmark payloads that were used for that historical independent CI reproduction have since been purged from the current tree as repository hygiene. This does not change the measured result; it means a new full-corpus rerun now requires the local corpus bytes (or another authorized source) matching the tracked input manifest. CI on the research branch now runs unit/regression tests only.

## Measured size

All totals include compressed metadata, retained visual assets, asset indexes, manifest and container framing.

| Representation | Bytes | Saving vs source PDF |
|---|---:|---:|
| Source PDF | 46,684,304 | 0% |
| Guarded / JSON entries | 19,040,870 | 59.214% |
| Guarded / channel pack | 17,144,893 | 63.275% |
| Guarded / flow pack | **16,863,350** | **63.878%** |
| Vector candidate / JSON entries | 10,053,631 | 78.465% |
| Vector candidate / channel pack | 7,510,272 | 83.913% |
| Vector candidate / flow pack | **7,228,195** | **84.517%** |

Historical source-exact baseline on the same input: 7z/LZMA2 = **35,369,533 bytes**, saving **24.237%**.

`guarded` preserves detected table envelopes as raster crops. `vector_candidate` replaces detected table rules with vectors and is an aggressive research candidate, not an approved conversion policy.

## Distribution

| Per-document saving | Guarded flow | Vector flow |
|---|---:|---:|
| Worst | 30.125% | 58.749% |
| Mean | 71.095% | 81.513% |
| P10 | 32.772% | 77.625% |
| Median | 81.477% | 83.191% |
| P90 | 87.404% | 87.403% |
| Best | 88.090% | 88.089% |
| Byte-weighted aggregate | 63.878% | 84.517% |

The largest gap between the two policies came from two table-heavy documents. This is evidence that table representation is a major storage lever, **not** evidence that inferred table semantics are correct.

## Composition of flow packages

| Component | Guarded | Vector candidate |
|---|---:|---:|
| Retained visual assets | 15,309,506 | 5,282,841 |
| Text / structure / provenance / index | 1,548,431 | 1,939,940 |
| Container framing | 5,413 | 5,414 |
| Total | 16,863,350 | 7,228,195 |

The corpus contains **931,847 extracted characters**. Text-only diagnostics compressed to 180,237 bytes independently or 164,134 bytes in one shared frame. Those values exclude structure, assets and provenance and must not be presented as complete document sizes.

## Fidelity limits

- Text roundtrip means equality with the existing selectable-text extraction, **not OCR correctness**. Source text already contains errors such as `FRESIDEN` and `REPUIUK` in inspected samples.
- Exact decoded crop pixels do not prove complete evidence-region recall.
- Table cell relationships, rowspan/colspan and legal hierarchy are not ground-truth validated.
- Fixed-layout review can show text/crop overlap; reflow is easier to read but extraction order is not guaranteed to equal natural reading order.
- A regression test prevents one observed class of stamp curve from being vectorized as a table rule, but this does not prove all stamps/figures are safe.
- The ten-document convenience set is not representative of all Indonesian legal PDFs.

Therefore the durable quality state remains:

```json
{
  "status": "requires_review",
  "ocr_accuracy": null,
  "evidence_recall": null,
  "semantic_structure_accuracy": null,
  "approved_for_source_deletion": false
}
```

## Reproduce

Unit and regression tests:

```powershell
uv run --with-requirements tools/l1/requirements.txt pytest tools/l1/test_l1.py -q
```

Full corpus study, using a **new output directory** and local PDFs:

```powershell
uv run --with-requirements tools/l1/requirements.txt python tools/l1/study.py `
  --input data `
  --output reports/l1-study-new `
  --workers 2
```

Verify the input SHA-256 values against `reports/storage-study-20260916/input_manifest.json` before comparing results. Generated output is ignored by Git; persist only reviewed compact summaries when a study becomes a durable record.

## Direction after this study

L1 established that structure-first representation can reduce this raster-heavy sample substantially. The next design target is **CIL-IR**, an AI-native canonical representation where PDF is an ingestion source, human-readable formats are exports, and embeddings/semantic outputs are rebuildable derivatives. See `docs/cil-ir.md` and issue #2.
