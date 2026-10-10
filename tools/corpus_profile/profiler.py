from __future__ import annotations

import hashlib
import json
import platform
import importlib.metadata
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import PROFILER_VERSION


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest_file(path):
    sha = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temporary.replace(path)


def prepare_manifest(inventory, output, expected_count=1000):
    if expected_count < 1:
        raise ValueError("expected_count must be positive")
    data = json.loads(Path(inventory).read_text(encoding="utf-8"))
    files = sorted(data["files"], key=lambda x: x["sha256"])
    hashes = [f["sha256"] for f in files]
    if len(files) != expected_count or len(set(hashes)) != expected_count:
        raise ValueError("inventory count/unique SHA count does not match expected_count")
    identities = [{"sha256": f["sha256"], "bytes": f["logical_size"], "sources": f["sources"]} for f in files]
    manifest = {"manifest_schema": "corpus-profile-manifest/v2", "profiler_version": PROFILER_VERSION,
                "manifest_sha256": hashlib.sha256(canonical(identities).encode()).hexdigest(),
                "count": len(files), "input_bytes": sum(f["logical_size"] for f in files), "files": files}
    write_json(Path(output) / "dataset_manifest.json", manifest)
    return manifest


def _one(entry, max_file_bytes):
    from .pdf_structure import profile_pdf
    started = time.perf_counter()
    row = {"sha256": entry["sha256"], "source_bytes": entry["logical_size"],
           "sources_json": canonical(entry["sources"])}
    try:
        path = Path(entry["path"])
        row["actual_bytes"] = path.stat().st_size
        row["size_valid"] = row["actual_bytes"] == entry["logical_size"] == entry["stored_size"]
        row["source_sha256"] = digest_file(path)
        row["sha_valid"] = row["source_sha256"] == entry["sha256"]
        if not row["size_valid"] or not row["sha_valid"]:
            raise ValueError("source size/checksum mismatch")
        if row["actual_bytes"] > max_file_bytes:
            raise ValueError("source exceeds configured max_file_bytes")
        result = profile_pdf(path)
        row.update(result["file"])
        row["status"] = "ok"
        pages, objects = result["pages"], result["objects"]
    except Exception as exc:
        row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        pages, objects = [], []
    row["profile_seconds"] = time.perf_counter() - started
    return row, pages, objects


def profile_manifest(manifest, output, *, workers=1, limit=None, max_file_bytes=1024**3):
    if not 1 <= workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output / "profile.sqlite")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS context(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS files(sha TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pages(sha TEXT, number INTEGER, value TEXT NOT NULL, PRIMARY KEY(sha,number));
    CREATE TABLE IF NOT EXISTS objects(sha TEXT, xref INTEGER, value TEXT NOT NULL, PRIMARY KEY(sha,xref));
    """)
    identity = canonical({"manifest": manifest["manifest_sha256"], "version": PROFILER_VERSION})
    old = db.execute("SELECT value FROM context WHERE key='identity'").fetchone()
    if old and old[0] != identity:
        raise ValueError("output belongs to a different manifest or profiler version")
    db.execute("INSERT OR IGNORE INTO context VALUES('identity',?)", (identity,))
    db.commit()
    completed = {r[0] for r in db.execute("SELECT sha FROM files")}
    selected = manifest["files"][:limit] if limit else manifest["files"]
    pending = [f for f in selected if f["sha256"] not in completed]
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        # At most workers files are in flight; no unbounded executor prefetch.
        for offset in range(0, len(pending), workers):
            futures = [pool.submit(_one, f, max_file_bytes) for f in pending[offset:offset + workers]]
            for entry, future in zip(pending[offset:offset + workers], futures):
                row, pages, objects = future.result()
                sha = entry["sha256"]
                with db:
                    db.execute("INSERT INTO files VALUES(?,?)", (sha, canonical(row)))
                    db.executemany("INSERT INTO pages VALUES(?,?,?)", [(sha, i, canonical(dict(p, sha256=sha))) for i, p in enumerate(pages)])
                    db.executemany("INSERT INTO objects VALUES(?,?,?)", [(sha, i, canonical(dict(o, file_sha256=sha))) for i, o in enumerate(objects)])
                completed.add(sha)
                progress = {"profiled": len(completed), "target": len(manifest["files"]),
                            "last_sha": sha, "last_status": row["status"], "session_seconds": time.perf_counter() - started}
                write_json(output / "profile_progress.json", progress)
                if len(completed) % 10 == 0 or row["status"] != "ok":
                    print(canonical(progress), flush=True)
    export_tables(db, output)
    db.close()


def export_tables(db, output):
    import pyarrow as pa
    import pyarrow.parquet as pq
    for table, filename in [("files", "file_profile.parquet"), ("pages", "page_profile.parquet"), ("objects", "object_profile.parquet")]:
        cursor = db.execute(f"SELECT value FROM {table} ORDER BY sha" )
        types = {}
        for record in cursor:
            for key, value in json.loads(record[0]).items():
                if value is None:
                    types.setdefault(key, pa.null())
                else:
                    kind = pa.string() if isinstance(value, (str, dict, list)) else pa.bool_() if isinstance(value, bool) else pa.float64() if isinstance(value, float) else pa.int64()
                    old = types.get(key)
                    types[key] = pa.float64() if old == pa.float64() and kind == pa.int64() or old == pa.int64() and kind == pa.float64() else kind
        schema = pa.schema([(k, types[k]) for k in sorted(types)])
        with pq.ParquetWriter(Path(output) / filename, schema, compression="zstd") as writer:
            cursor = db.execute(f"SELECT value FROM {table} ORDER BY sha")
            while records := cursor.fetchmany(1000):
                normalized = [{k: canonical(v) if isinstance(v, (dict, list)) else v for k, v in json.loads(r[0]).items()} for r in records]
                writer.write_table(pa.Table.from_pylist(normalized, schema=schema))


def final_validation(manifest, output):
    failures = []
    for f in manifest["files"]:
        try:
            if Path(f["path"]).stat().st_size != f["logical_size"] or digest_file(f["path"]) != f["sha256"]:
                failures.append({"sha256": f["sha256"], "error": "source changed"})
        except Exception as exc:
            failures.append({"sha256": f["sha256"], "error": str(exc)})
    report = {"count": len(manifest["files"]), "source_failures": failures,
              "manifest_sha256": manifest["manifest_sha256"], "profiler_version": PROFILER_VERSION,
              "python": platform.python_version(), "platform": platform.platform()}
    report["libraries"] = {}
    for name in ["PyMuPDF", "pyarrow", "zstandard", "matplotlib"]:
        try:
            report["libraries"][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            report["libraries"][name] = "not_available"
    write_json(Path(output) / "validation_report.json", report)
    return report
