# Architecture

Corpus Indomicus v1 treats acquisition as an archival pipeline rather than a scraping script.

```text
Source discovery
      |
      v
Source connector
      |
      +--> detail HTML --------+
      |                        |
      +--> document files -----+--> immutable raw archive
                               |
                               +--> SHA-256
                               |
                               +--> source observation
                               |
                               +--> canonical metadata
                                      |
                                      v
                                SQLite registry
```

## Core objects

### LegalInstrument

Represents the legal act as a conceptual object: type, number, year, title, issuing body, status, dates, publication information, and relationships.

It is not a downloaded file.

### SourceObservation

Represents one observation of an instrument or source artifact at a particular URL and retrieval time.

A single instrument may have many observations from several providers.

### StoredObject

Represents immutable retrieved bytes identified by SHA-256.

The raw archive is content-addressed. If two URLs return byte-identical PDFs, only one content object is needed on disk while the registry may retain both provenance records.

## Why SQLite first?

v1 needs restartability, uniqueness constraints, indexes, and provenance more than it needs distributed infrastructure. SQLite provides those properties with essentially zero operator burden.

A future migration to PostgreSQL or another catalog service should be a storage concern, not a reason to redesign the instrument model.

## Failure policy

Acquisition is intentionally conservative:

- raw source bytes are preserved before downstream interpretation matters;
- incomplete metadata does not block raw archival;
- missing metadata remains missing instead of being guessed;
- source failures increment the run error count and do not invalidate prior records;
- seen detail URLs are skipped by default;
- `--force` re-fetches when an operator intentionally wants a new observation.

## Out of scope for v1 foundation

- OCR
- semantic document parsing
- legal citation extraction
- amendment graph resolution
- temporal consolidation
- embeddings and vector search
- legal reasoning
- end-user web UI

Those systems should consume the archive rather than contaminating acquisition with derived interpretations.
