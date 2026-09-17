# CIL-IR: AI-native canonical representation for Corpus-Indomicus

Status: design target after the L1 storage experiment. This document supersedes the idea that Markdown, PDF, or a human-layout format should be the canonical working representation.

## Decision

Corpus-Indomicus should treat government PDF files as **ingestion sources**, not as its canonical working format.

The canonical representation should be a compact, machine-native legal intermediate representation called **CIL-IR** (Corpus Indomicus Legal Intermediate Representation).

Human-readable formats such as Markdown, HTML, and PDF become **derived views**. Search indexes, embeddings, summaries, and Arline semantic artifacts are **rebuildable derived indexes**, not canonical document bytes.

The design does **not** require:

- source-PDF byte exactness;
- pixel-exact page reconstruction;
- preservation of scanner noise, paper texture, decorative logos, signatures, or stamps by default;
- human readability of the stored representation.

It **does** require:

- preservation of source text as extracted, with provenance;
- legal hierarchy and ordering;
- tables and typed values when recoverable;
- figures, diagrams, maps, and other visually informative material when semantically relevant;
- source-page mapping;
- explicit distinction between canonical source-derived content and derived semantic interpretation;
- deterministic decoding and validation.

## Why this direction

The measured ten-PDF study contains 728 pages and 46,684,304 source bytes. The existing L1 prototype reduced this to 7,228,195 bytes (84.517% saving) in its aggressive vector/reflow candidate, but about 5.28 MB of that result was still retained visual assets. The textual corpus itself is tiny by comparison: 931,847 extracted characters compress to about 164 KB when stored in one diagnostic frame.

That result indicates that the next important step is **representation design and semantic filtering**, not a more exotic PDF codec.

## Canonical vs derived data

### Canonical

CIL-IR should contain only source-derived information needed to reproduce the corpus meaning and machine-usable structure:

1. `HEADER`
   - format version;
   - instrument/source IDs;
   - provider/source URL identifiers;
   - original SHA-256 and source size;
   - acquisition timestamp/cutoff;
   - converter/parser version.

2. `TEXT`
   - one canonical text pool per document or shard;
   - source text preserved without silent correction;
   - optional normalization stored separately, never replacing raw extracted text.

3. `TREE`
   - legal/document hierarchy such as document, title, preamble, BAB, Bagian, Pasal, ayat, item, list, table, appendix, closing;
   - text ranges into `TEXT` rather than duplicate strings;
   - source-page ranges;
   - unknown nodes are valid and preferred to invented structure.

4. `TABLE`
   - rows, columns, cell text references, rowspan/colspan;
   - typed numeric/date values where confidence is sufficient;
   - raw surface string/reference retained when a typed interpretation is added.

5. `VISUAL`
   - maps;
   - diagrams;
   - informative figures/photos;
   - difficult tables that cannot yet be represented structurally;
   - unknown visually meaningful regions when required by policy.

6. `PROVENANCE`
   - exact source hash;
   - source-page mapping;
   - extraction and parser versions;
   - confidence/status for derived parsing steps.

### Derived and rebuildable

The following should not define the canonical representation:

- BM25/FTS indexes;
- embeddings/vector indexes;
- summaries;
- semantic chunks;
- extracted claims;
- entity links;
- temporal/legal state;
- graph indexes;
- Markdown/HTML/PDF rendering;
- Arline-specific retrieval caches.

These can be rebuilt when algorithms/models change.

## Semantic layer and Arline integration

CIL-IR should expose stable source nodes to an Arline-style semantic layer.

Example:

```text
CIL source node
  ID:UU:2026:12/article/17/paragraph/2
        |
        +-> raw source text range
        +-> references
        +-> source page
        +-> derived claim(s)
        +-> derived entity links
        +-> derived temporal state
```

Derived semantic objects should always point back to source nodes/text ranges.

Example claim:

```text
CLAIM
  subject     = entity:MINISTER_X
  predicate   = MUST_SUBMIT
  object      = entity:REPORT
  deadline    = duration:30d
  source_node = ID:UU:2026:12/article/17/paragraph/2
  extractor   = semantic-model-version
  confidence  = ...
```

A wrong or obsolete semantic extractor must be replaceable without touching canonical source text.

## Physical storage model

CIL-IR does not need to be one file per regulation. At scale, prefer **shards**.

```text
corpus/
  registry.db or registry.parquet
  text/
    shard-000001.cil
  tree/
    shard-000001.cil
  table/
    shard-000001.cil
  visual/
    assets.cas
  semantic/        # rebuildable
    embeddings/
    claims/
    graph/
```

A registry maps `instrument_id -> shard + offset`.

Advantages:

- better compression context across similar legal text;
- fewer filesystem objects;
- sequential I/O and batch processing;
- mmap-friendly indexes;
- easy parallel training/retrieval pipelines.

## Proposed binary channels

Do not encode the canonical object as Markdown or repeated JSON maps.

Suggested channels:

```text
HEADER
TEXT
NODE_TYPE
TREE_TOPOLOGY
TEXT_START
TEXT_LENGTH
NUMBER
SOURCE_PAGE
TABLE_SCHEMA
TABLE_VALUE
VISUAL_REF
REFERENCE
PROVENANCE
```

Possible encoding by channel:

| Channel | Candidate representation |
|---|---|
| TEXT | Zstd, optionally trained legal-text dictionary |
| NODE_TYPE | bit-packed / RLE / entropy coded |
| TREE_TOPOLOGY | succinct parentheses/preorder representation |
| offsets/lengths | delta + unsigned varint |
| signed deltas | ZigZag + varint |
| article/paragraph numbers | delta/implicit sequence + varint |
| repeated IDs | dictionary encoding |
| table values | typed columnar streams + RLE/delta |
| visual refs | integer CAS IDs |
| provenance | compact schema/Protobuf-like binary |

The first implementation should prefer simple deterministic encodings. rANS/FSE/Re-Pair are benchmark candidates only after representation-level costs are understood.

## Visual policy

Default **drop** from canonical CIL-IR unless explicitly needed:

- signature images;
- stamps/seals;
- decorative Garuda/header logos;
- scanner borders/noise;
- paper texture;
- exact fonts and word coordinates.

Default **retain** when informative:

- maps;
- diagrams;
- technical figures;
- photographs with legal meaning;
- QR/barcodes only if their payload/reference matters;
- raster table fallback when structural parsing is not trustworthy.

This policy is intentionally different from archival preservation. Corpus-Indomicus is optimizing for searchable and analyzable legal knowledge.

## Search and AI access

Search should work from `TEXT + TREE`, not PDF parsing.

```text
query
  -> BM25 / semantic router
  -> legal node IDs
  -> graph/reference expansion
  -> exact source text ranges
  -> context compiler
  -> model
```

Embeddings should be stored outside canonical shards because they can exceed the original text size and become obsolete whenever embedding models change.

## Human views

Human readability is an output concern:

```text
CIL-IR
  -> Markdown
  -> HTML
  -> plain text
  -> PDF export
```

No storage decision should be driven by making the canonical bytes pleasant to open in a text editor.

## Next benchmark

The next study must compare at least:

1. current `flow_pack.ildr`;
2. text + legal tree only;
3. text + legal tree + typed tables;
4. CIL-IR with informative visual policy;
5. per-document compression;
6. corpus/shard compression.

Report:

- canonical bytes total;
- text bytes;
- tree bytes;
- table bytes;
- retained visual bytes;
- provenance bytes;
- index/container overhead;
- worst/P50/P90/best per document;
- encode/decode throughput;
- random-access latency;
- legal-tree accuracy on audited samples;
- table accuracy on audited samples;
- retained-visual recall on audited samples.

Do not claim a >90% corpus saving until this is measured on real inputs.

## Acceptance direction

CIL-IR is promising if it materially improves on the current 7.23 MB L1 candidate while keeping:

- raw extracted text lossless relative to the selected source extraction;
- source-node provenance;
- usable legal hierarchy;
- informative tables/figures;
- deterministic decoder;
- derived semantic layer rebuildability.

The next target is not a prettier viewer. The target is a compact AI-native legal IR that Arline-style retrieval and reasoning can consume directly.
