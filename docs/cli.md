# Acquisition CLI

Global `--config PATH` and `--data-dir PATH` precede the command. Setup initializes local storage only. BPK commands require network access; plan fetches catalogs but does not download legal documents.

```bash
uv run corpus-indomicus setup
uv run corpus-indomicus catalog types --groups pusat,lembaga
uv run corpus-indomicus plan --groups pusat,lembaga --all-years
uv run corpus-indomicus discover --groups pusat,lembaga --all-years
uv run corpus-indomicus discover --run-id 1
uv run corpus-indomicus ingest --run-id 1 --limit 100
uv run corpus-indomicus status --run-id 1 --output coverage.json
uv run corpus-indomicus retry --run-id 1
uv run corpus-indomicus verify
```

Read the printed run ID rather than assuming 1. `discover --run-id` loads the saved catalog/scope and performs no catalog refresh; omit scope filters. Changed groups/types/query/year bounds create a separate fingerprint/run. `--fresh` forces a separate run. Catalog counts and timestamps changing alone do not prevent resume.

`--groups` accepts `pusat,lembaga`, `pusat` or `lembaga`. `--types` narrows discovery/plan to comma-separated IDs verified against those catalogs. Unresolved/regional types remain in the catalog snapshot but are not selected. Without year flags, new acquisition covers all available source history via pagination per type. Explicit year ranges use one partition per type/year. `--all` has the same meaning as `--all-years`. Config `pilot_from_year` remains a legacy fallback for omitted range starts and recent sync; it does not constrain all-years discovery.

`discover --max-pages N` bounds pages per partition during that invocation. Reaching the bound keeps discovery partial and prevents ingestion. `ingest --limit N` bounds processed manifest items, including recovered interrupted items; zero performs no item work. `ingest --retry-failed` also selects failed/partial/unresolved/no-file items. The dedicated `retry` command revisits those statuses. `done` requires every expected attachment to pass validation. A verified record with no files is terminal `no_file` and remains visible in coverage.

`backfill` combines scoped discovery and ingestion, accepts the same groups/types/year options and `--limit`. `sync` uses the configured recent overlap unless explicit years/all-years are provided. Both legacy commands remain available. Low-level `bpk discover` and `bpk ingest` retain `--query`, `--year`, `--page`, `--limit` (default 10); optional `--groups` and `--type-id` restrict their verified single-page samples. These samples do not claim full coverage. `references` reports neutral reference-target coverage.

`status --json` or `--run-id` prints registry-derived JSON. `--output` writes the same JSON. Reports separate source identities, membership occurrences, instrument rows, statuses, source-reported counts, successful file links, unique physical file hashes and logical/physical bytes. Per-run file statistics use the detail snapshot acquired by that run; older rows without that hash require re-observation. `global_storage` covers all archived payloads including HTML and invalid responses; parser HTML diagnostics remain separate local evidence. `verify` rehashes all registered objects in batches and checks missing files. It detects corruption; it does not silently repair/delete evidence.

Exit codes: 0 success/bounded work performed, 2 acquisition/parser/integrity errors or incomplete discovery, 3 storage reserve pause. A successful bounded ingestion can return 0 while its run remains partial with pending items. Full historical downloading is a separate operational step after reviewing smoke evidence and available disk space.
