# Reports policy

Git tracks only compact, audit-relevant study summaries and manifests.

Do **not** commit generated benchmark payloads, reconstructed PDFs, compressed archives, dedup blobs, previews, Parquet/CSV tables, logs, PID files, or transient workflow state. Those belong in local `reports/` output or short-lived GitHub Actions artifacts.

Tracked study records should be sufficient to identify the input hashes, methodology, aggregate measurements, validation status, and important limitations. Raw artifacts must be reproducible from local corpus bytes and the versioned tools.

This keeps the repository a source repository instead of slowly evolving into a landfill with a `.git` directory.
