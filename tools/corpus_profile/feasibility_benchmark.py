"""Reproducible bounded Phase 2.1 storage-feasibility orchestration.

This module prepares the stratified 45-document sample from the existing
characterization manifests, verifies source identity, runs the standard exact
compression benchmark, and records lightweight references to compatible full
corpus results. Source PDFs are never copied or modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from .benchmark import METHODS, load_inventory, make_units, run_benchmark
from .profiler import digest_file, write_json

DEFAULT_METHODS = METHODS
DEFAULT_SHARD_BYTES = 128 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_RSS_BYTES = 1024 * 1024 * 1024


def _read(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _inventory_identity(files: Sequence[dict[str, Any]]) -> str:
    value = [{"sha256": str(f["sha256"]).lower(),
              "size": int(f.get("logical_size", f.get("size", 0)))}
             for f in sorted(files, key=lambda f: str(f["sha256"]))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare_sample_inventory(source_inventory: str | Path, sample_manifest: str | Path,
                             output: str | Path) -> dict[str, Any]:
    """Create a deterministic sample inventory and verify every selected source."""
    source = _read(source_inventory)
    source_files = source["files"]
    sample = _read(sample_manifest)
    selected_sha = [str(item["sha256"]).lower() for item in sample["representative"]]
    if len(selected_sha) != len(set(selected_sha)):
        raise ValueError("sample manifest contains duplicate SHA-256 identities")
    by_sha = {str(item["sha256"]).lower(): item for item in source_files}
    if not selected_sha or len(by_sha) != len(source_files):
        raise ValueError("empty sample or duplicate source inventory identities")
    missing = sorted(set(selected_sha) - set(by_sha))
    if missing:
        raise ValueError(f"sample SHA identities absent from source inventory: {missing[:3]}")

    records = [by_sha[sha] for sha in selected_sha]
    failures: list[dict[str, str]] = []
    for record in records:
        path = Path(record["path"])
        expected_size = int(record.get("logical_size", record.get("size", -1)))
        try:
            actual_size = path.stat().st_size
            actual_sha = digest_file(path)
            if actual_size != expected_size or actual_sha != str(record["sha256"]).lower():
                failures.append({"sha256": str(record["sha256"]), "error": "source size/checksum mismatch"})
        except Exception as exc:
            failures.append({"sha256": str(record["sha256"]), "error": f"{type(exc).__name__}: {exc}"})
    if failures:
        raise ValueError(f"sample source identity validation failed for {len(failures)} file(s)")

    inventory = {"count": len(records), "files": [
        {"path": str(item["path"]), "sha256": str(item["sha256"]).lower(),
         "logical_size": int(item.get("logical_size", item.get("size", 0)))}
        for item in records
    ]}
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "sample_inventory.json", inventory)
    receipt = {
        "schema": "phase2.1-sample-receipt/v1",
        "source_inventory": str(Path(source_inventory).resolve()),
        "source_inventory_identity": _inventory_identity(source_files),
        "sample_manifest": str(Path(sample_manifest).resolve()),
        "sample_manifest_sha256": hashlib.sha256(Path(sample_manifest).read_bytes()).hexdigest(),
        "selected_count": len(records), "verified_count": len(records),
        "full_coverage": len(records) == len(selected_sha),
        "files": [{"sha256": item["sha256"], "size": item["logical_size"]} for item in inventory["files"]],
    }
    write_json(out / "sample_receipt.json", receipt)
    return receipt


def validate_sample_receipt(inventory: str | Path, receipt: str | Path) -> dict[str, Any]:
    """Recheck receipt coverage and exact source identity after a run."""
    data = _read(inventory)
    saved = _read(receipt)
    failures = []
    for item in data["files"]:
        path = Path(item["path"])
        try:
            if path.stat().st_size != int(item["logical_size"]) or digest_file(path) != item["sha256"]:
                failures.append(item["sha256"])
        except OSError:
            failures.append(item["sha256"])
    expected = {item["sha256"] for item in data["files"]}
    received = {item["sha256"] for item in saved["files"]}
    return {"count": len(expected), "verified": len(expected) - len(failures),
            "source_failures": failures, "missing_receipt": sorted(expected - received),
            "full_coverage": expected == received and not failures}


def validate_benchmark_results(inventory: str | Path, benchmark_dir: str | Path,
                               methods: Sequence[str], shard_bytes: int,
                               write_report: bool = True,
                               modes: Sequence[str] = ("perdoc", "shard")) -> dict[str, Any]:
    """Validate row identity, exact receipts, and shard membership coverage."""
    with (Path(benchmark_dir) / "benchmark.jsonl").open(encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    files = load_inventory(inventory)
    expected = {(unit.unit_id, method): unit for mode in modes
                for unit in make_units(files, mode, shard_bytes) for method in methods}
    keys = [(row.get("unit_id"), row.get("method")) for row in records]
    duplicates = sorted(f"{key[0]} {key[1]}" for key, count in Counter(keys).items() if count > 1)
    seen = set(keys)
    missing = sorted(f"{key[0]} {key[1]}" for key in expected.keys() - seen)
    failures = []
    for row in records:
        key = (row.get("unit_id"), row.get("method"))
        unit = expected.get(key)
        if unit is None:
            failures.append({"unit_id": row.get("unit_id"), "method": row.get("method"), "error": "unexpected unit"})
            continue
        expected_sha = [item.sha256 for item in unit.files]
        if (row.get("mode") != unit.mode or row.get("status") != "ok" or row.get("exact") is not True
                or int(row.get("input_bytes", -1)) != unit.input_bytes
                or int(row.get("output_bytes", -1)) != int(row.get("payload_bytes", -2)) + int(row.get("manifest_bytes", -1))
                or row.get("independent_access") != (unit.mode == "perdoc")
                or row.get("reconstructed_sha256") != expected_sha):
            failures.append({"unit_id": unit.unit_id, "method": row.get("method"), "error": "receipt mismatch"})
    result = {"expected_rows": len(expected), "observed_rows": len(records),
              "missing_rows": missing, "duplicate_rows": duplicates, "failures": failures,
              "full_coverage": not missing and not duplicates and not failures and len(records) == len(expected)}
    if write_report:
        write_json(Path(benchmark_dir) / "benchmark_validation.json", result)
    return result


def reference_full_benchmark(source_inventory: str | Path, full_dir: str | Path,
                             output: str | Path) -> dict[str, Any]:
    """Record compatible full-run metadata and aggregate references only."""
    source = _read(source_inventory)
    source_identity = _inventory_identity(source["files"])
    full = Path(full_dir)
    refs = []
    source_failures = []
    for item in source["files"]:
        try:
            if Path(item["path"]).stat().st_size != int(item.get("logical_size", item.get("size", -1))) or digest_file(Path(item["path"])) != str(item["sha256"]).lower():
                source_failures.append(item["sha256"])
        except OSError:
            source_failures.append(item["sha256"])
    for mode in ("full-perdoc", "full-shard"):
        meta_path = full / mode / "benchmark_meta.json"
        if not meta_path.exists():
            continue
        meta = _read(meta_path)
        identity = meta.get("identity", {})
        full_files = identity.get("files", [])
        compatible = len(full_files) == len(source["files"]) and {
            (str(x["sha256"]).lower(), int(x["size"])) for x in full_files
        } == {(str(x["sha256"]).lower(), int(x.get("logical_size", x.get("size", 0)))) for x in source["files"]}
        summary = Counter()
        csv_path = full / mode / "benchmark.csv"
        if csv_path.exists():
            import csv
            with csv_path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    summary[row.get("status", "unknown")] += 1
        row_validation = validate_benchmark_results(source_inventory, full / mode,
                                                     identity.get("methods", []), int(identity.get("shard_bytes", DEFAULT_SHARD_BYTES)),
                                                     write_report=False, modes=identity.get("modes", ())) if compatible else {"full_coverage": False}
        compatible = compatible and not source_failures and row_validation.get("full_coverage", False)
        refs.append({"mode": mode, "path": str(meta_path.resolve()),
                     "identity_key": meta.get("identity_key"), "compatible": compatible,
                     "source_inventory_identity": source_identity,
                     "rows_by_status": dict(summary), "row_validation": row_validation})
    result = {"schema": "phase2.1-full-reference/v1", "references": refs,
              "source_failures": source_failures,
              "reused_without_rerun": len(refs) == 2 and not source_failures and all(r["compatible"] for r in refs)}
    write_json(Path(output) / "full_benchmark_reference.json", result)
    return result


def run_feasibility(source_inventory: str | Path, sample_manifest: str | Path,
                    output: str | Path, full_benchmark_dir: str | Path | None = None,
                    methods: Sequence[str] = DEFAULT_METHODS,
                    shard_bytes: int = DEFAULT_SHARD_BYTES,
                    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                    max_rss_bytes: int = DEFAULT_MAX_RSS_BYTES) -> dict[str, Any]:
    out = Path(output)
    receipt = prepare_sample_inventory(source_inventory, sample_manifest, out)
    inventory_path = out / "sample_inventory.json"
    rows = run_benchmark(inventory_path, out / "benchmarks" / "sample", tuple(methods),
                         ("perdoc", "shard"), shard_bytes=shard_bytes,
                         timeout_seconds=timeout_seconds, max_rss_bytes=max_rss_bytes)
    validation = validate_sample_receipt(inventory_path, out / "sample_receipt.json")
    benchmark_validation = validate_benchmark_results(inventory_path, out / "benchmarks" / "sample",
                                                      methods, shard_bytes)
    full_reference = reference_full_benchmark(source_inventory, full_benchmark_dir, out) if full_benchmark_dir else None
    summary = {"schema": "phase2.1-feasibility-summary/v1", "python": platform.python_version(),
               "sample": receipt, "validation": validation,
               "benchmark_validation": benchmark_validation,
               "benchmark": {"rows": len(rows), "status_counts": dict(Counter(r.get("status", "unknown") for r in rows)),
                              "methods": list(methods), "modes": ["perdoc", "shard"], "shard_bytes": shard_bytes,
                              "timeout_seconds": timeout_seconds, "max_rss_bytes": max_rss_bytes},
               "full_benchmark_reference": full_reference}
    write_json(out / "feasibility_summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-inventory", required=True)
    parser.add_argument("--sample-manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--full-benchmark-dir")
    parser.add_argument("--methods", default=",".join(DEFAULT_METHODS))
    parser.add_argument("--shard-bytes", type=int, default=DEFAULT_SHARD_BYTES)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-rss-bytes", type=int, default=DEFAULT_MAX_RSS_BYTES)
    args = parser.parse_args(argv)
    summary = run_feasibility(args.source_inventory, args.sample_manifest, args.output,
                              args.full_benchmark_dir, tuple(x for x in args.methods.split(",") if x),
                              args.shard_bytes, args.timeout_seconds, args.max_rss_bytes)
    print(json.dumps(summary["benchmark"], sort_keys=True))
    valid = summary["validation"]["full_coverage"] and summary["benchmark_validation"]["full_coverage"]
    if args.full_benchmark_dir:
        valid = valid and summary["full_benchmark_reference"]["reused_without_rerun"]
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
