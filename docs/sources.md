# Source strategy

Corpus Indomicus is designed as a multi-source archive.

Initial priority:

1. Database Peraturan JDIH BPK
2. Ditjen Peraturan Perundang-undangan / peraturan.go.id
3. JDIHN as a national discovery layer
4. issuing-ministry and issuing-agency JDIH sites
5. provincial and regency/city JDIH sites

No single source should become the permanent identity namespace of the corpus.

## Connector rules

A connector should:

- identify itself with a stable provider key;
- discover source records without inventing canonical legal facts;
- archive source HTML when useful for auditability;
- retrieve document files conservatively;
- respect request pacing;
- expose enough source metadata for later normalization;
- tolerate fields that are missing or structurally inconsistent.

## JDIH BPK connector

Provider key: `jdih_bpk`

The v1 connector uses the public search and detail pages, then detects downloadable files from the detail record. Parsing is deliberately defensive because public-site HTML can change independently of this repository.

Before large backfills, validate the parser against representative records from:

- central laws
- government regulations
- presidential regulations
- ministerial regulations
- agency regulations
- provincial regulations
- regency regulations
- mayoral regulations

Large historical crawls should use checkpoints and a slower request policy than tiny development samples.
