# Sources

## JDIH BPK

JDIH BPK is the first v1 connector because it provides broad coverage of Indonesian regulations and stable detail pages suitable for a bounded pilot.

The connector records:

- search discovery source IDs and URLs
- source-reported result totals when parseable
- detail-page metadata
- downloadable document links
- neutral links to other BPK detail pages

The acquisition engine rate-limits requests and persists checkpoints. Search result pages may contain links to related historical instruments; year-scoped discovery filters obvious out-of-year links, and detail processing enforces the requested year again before accepting the record into that year slice.

More sources should be added after the v1 pilot measures real overlap, storage distribution and failure modes. Multi-source deduplication is already supported at the content-object layer.
