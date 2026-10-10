# Sources

## JDIH BPK

JDIH BPK is the first v1 connector because it provides broad coverage of Indonesian regulations and stable detail pages suitable for a bounded pilot.

The connector records:

- search discovery source IDs and URLs
- source-reported result totals when parseable
- detail-page metadata
- downloadable document links
- neutral links to other BPK detail pages

Catalog discovery reads `/Jenis/1` (central) and `/Jenis/2` (ministry/institution) and resolves each authoritative `/Search?jenis=<id>` link dynamically. Regional types are excluded from this pilot. Search parsing accepts only recognized primary result cards, validates same-host numeric `/Details/<id>` URLs, and excludes status/reference links. Unknown DOM shapes fail closed and can be retained under the connector diagnostics directory.

The acquisition engine checks BPK robots.txt, honors crawl-delay and HTTP Retry-After, rate-limits requests and retries 429/5xx/transport failures. Access-denied responses are not repeatedly retried. Pagination follows enabled numeric pages and ignores disabled “next” links. An active final pager page or a verified single-page source count establishes completion; unknown structure, repeated pages and page limits leave discovery incomplete. URL/title years are hints only; structured detail metadata verifies year during acquisition.

The source's search is mutable and offset-based, so a scope cutoff is an observation timestamp rather than a historical snapshot guarantee. Later synchronization and coverage audits are required when source inventory changes during discovery. TLS uses the operating system's trusted certificate store.

Source grouping is recorded separately from normative legal classification. In particular, “Gubernur Lembaga Ketahanan Nasional” is an institutional title, and “Daerah” in a ministry's name does not establish regional jurisdiction. Definitive regional forms/locations are excluded; unverifiable category/type metadata is retained as unresolved. Every catalog type retains its ID, name, group, URL, reported count and observation timestamp.

More sources should be added after the v1 pilot measures real overlap, storage distribution and failure modes. Multi-source deduplication is already supported at the content-object layer.
