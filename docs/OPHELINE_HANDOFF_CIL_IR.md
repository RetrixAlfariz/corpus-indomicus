# Opheline handoff: CIL-IR next phase

This is the concrete continuation point after the L1 experiment. Read `docs/l1-study-results.md` first for measured results, then `docs/cil-ir.md` for the target architecture.

## Current state

- Branch: `research/l1-structure-first`
- Current L1 experiment: `tools/l1/`
- Measured input: 10 PDFs, 728 pages, 46,684,304 bytes
- Current smallest review-only candidate: 7,228,195 bytes (84.517% saving)
- Text pool: 931,847 extracted characters; diagnostic shared-frame compression ~164 KB
- Main remaining L1 cost: visual assets, not text
- Source deletion remains disabled because OCR accuracy, table semantics and visual/evidence recall are not yet audited

## New decision

Do **not** optimize for human-readable storage. Markdown/HTML/PDF are exports only.

Implement a machine-native canonical representation: **CIL-IR**.

Do not preserve by default:

- signatures;
- stamps/seals;
- decorative logos;
- scanner noise/background;
- exact page layout;
- fonts;
- word-level bounding boxes.

Preserve:

- raw extracted text;
- legal hierarchy/order;
- source page mapping;
- tables/typed values;
- maps/diagrams/informative figures;
- difficult table raster fallback;
- provenance.

Derived Arline-style claims/entities/relations/temporal state and embeddings must be separate and rebuildable.

## Phase 1 — measured CIL-IR prototype

Create `tools/cil_ir/` without modifying acquisition code.

### 1. Text pool

Input existing L1 extraction or PDFs through the current extractor.

Produce:

```text
TEXT = concatenated raw extracted text
```

Requirements:

- no silent normalization;
- byte/hash validation;
- every structural node references `start,length`;
- no duplicated source strings in structural records.

### 2. Legal tree

Implement deterministic rule-based candidate parsing first.

Node types minimum:

```text
DOCUMENT
TITLE
PREAMBLE
CHAPTER
SECTION
ARTICLE
PARAGRAPH
ITEM
LIST
TABLE
APPENDIX
CLOSING
UNKNOWN
```

Store:

```text
node_type
text_start
text_length
number/label if available
source_page_start
source_page_end
parent/tree topology
parser status/confidence class
```

Do not use an LLM to make decoding possible. Unknown is valid.

### 3. Compact binary baseline

Build at least two physical encodings:

A. schema-based baseline using MessagePack/CBOR/Protobuf-like records + zstd;
B. channelized representation:

```text
NODE_TYPE
TREE_TOPOLOGY
TEXT_START
TEXT_LENGTH
NUMBER
SOURCE_PAGE
```

Use simple deterministic techniques where useful:

- delta integers;
- unsigned varints;
- ZigZag for signed deltas;
- RLE/dictionary encoding;
- bit packing for node type;
- zstd per homogeneous channel.

Do not implement rANS/FSE/Re-Pair until A/B sizes are known.

### 4. Tables

Implement a separate table channel.

At minimum:

```text
table_id
rows
columns
cell_text_ref
rowspan
colspan
surface_text_ref
typed_value optional
```

Typed values are derived annotations and must not replace source surface text.

For tables that cannot be reliably parsed, use an explicit `RASTER_TABLE_FALLBACK` visual object.

### 5. Visual filter

The current L1 region retention is too broad for the new use case.

Classify or conservatively route visual regions into:

```text
DROP_DECORATIVE
MAP
DIAGRAM
PHOTO
INFORMATIVE_FIGURE
QR_OR_BARCODE
RASTER_TABLE_FALLBACK
UNKNOWN_VISUAL
```

Signatures, stamps and decorative logos should default to `DROP_DECORATIVE` for this AI-corpus representation unless a future policy says otherwise.

Do not claim semantic classification accuracy without an audited sample. Keep unknown visual regions until policy/audit resolves them.

### 6. Provenance

Every CIL-IR document/shard must retain:

```text
instrument/source ID if available
original SHA-256
original source bytes
source page mapping
parser/converter version
acquisition/provider identifiers when available
```

Do not make source URL the only identity.

## Phase 2 — AI/Arline bridge

Create a separate rebuildable directory/schema, not part of canonical storage:

```text
semantic/
  chunks
  entities
  claims
  references
  temporal
  embeddings
```

Every derived record must point to CIL source node IDs/text ranges.

Example:

```text
claim.source_node = ID:.../article/17/paragraph/2
claim.extractor_version = ...
```

Never overwrite canonical source text based on a semantic model.

## Phase 3 — shard experiment

Compare:

1. one CIL file per document;
2. 10-document shared text/tree shard;
3. trained-zstd dictionary if sample count is sufficient.

Registry maps `document_id -> shard + offsets`.

Do not mix embeddings into canonical shard size.

## Required benchmark output

Create `reports/cil-ir-study-YYYYMMDD/` containing:

```text
summary.json
per_document.csv
channel_breakdown.csv
legal_tree_audit.json
Table_audit.json
visual_policy_audit.json
provenance.json
CIL_IR_STUDY_REPORT.md
```

Report separately:

- source PDF bytes;
- current L1 flow bytes;
- CIL text bytes;
- tree bytes;
- table bytes;
- retained visual bytes;
- provenance bytes;
- container/index bytes;
- total canonical bytes;
- per-document P10/P50/P90/worst/best;
- encode/decode time;
- random-access decode time for one article/table;
- parser failures/unknown nodes;
- table fidelity sample;
- visual-retention audited sample.

## Quality gates

Required before calling CIL-IR canonical:

1. Raw extracted text round-trip = 100% against chosen extraction baseline.
2. All nodes resolve to valid text/source ranges.
3. Decoder deterministic and versioned.
4. Unknown parsing never deletes source text.
5. Table typed value never replaces source surface form.
6. Informative visual policy manually audited on a sample.
7. Source PDF SHA and source-page provenance retained.
8. No embedding/model output required for canonical decoding.

Do not equate these with OCR ground-truth accuracy. OCR/source-text correctness is a separate audit.

## Target, not claim

The next engineering target is roughly **2–4 MB** for the current 46.68 MB ten-document corpus, corresponding to ~91–96% reduction, but this is only a target until measured.

If CIL-IR does not improve materially over the existing 7.23 MB candidate after tables and visuals are correctly retained, prefer the simpler representation.

## Recommended first commit

Start with:

```text
tools/cil_ir/schema.py
tools/cil_ir/encode.py
tools/cil_ir/decode.py
tools/cil_ir/benchmark.py
tools/cil_ir/test_cil_ir.py
```

Implement **TEXT + TREE only first**, benchmark it on all 10 real PDFs, and commit the measured size before adding tables or visual channels. That isolates where every added feature costs storage.
