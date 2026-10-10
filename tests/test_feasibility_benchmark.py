import hashlib
import json
from pathlib import Path

import pytest

from tools.corpus_profile.feasibility_benchmark import (
    prepare_sample_inventory,
    run_feasibility,
    reference_full_benchmark,
    validate_benchmark_results,
    validate_sample_receipt,
)


def _fixture(tmp_path: Path):
    files = []
    representatives = []
    for index in range(2):
        data = (f"fixture-{index}".encode() * (index + 2)) + b"\n%%EOF\n"
        path = tmp_path / f"{index}.pdf"
        path.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        files.append({"path": str(path), "sha256": sha, "logical_size": len(data), "sources": []})
        representatives.append({"sha256": sha, "stratum": ["fixture", str(index)]})
    source = tmp_path / "dataset.json"
    source.write_text(json.dumps({"count": 2, "files": files}), encoding="utf-8")
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"representative": representatives}), encoding="utf-8")
    return source, sample


def test_sample_inventory_verifies_each_original_and_coverage(tmp_path):
    source, sample = _fixture(tmp_path)
    receipt = prepare_sample_inventory(source, sample, tmp_path / "out")
    assert receipt["selected_count"] == 2
    result = validate_sample_receipt(tmp_path / "out" / "sample_inventory.json",
                                    tmp_path / "out" / "sample_receipt.json")
    assert result["full_coverage"] is True
    assert result["source_failures"] == []


def test_sample_rejects_changed_source(tmp_path):
    source, sample = _fixture(tmp_path)
    record = json.loads(source.read_text(encoding="utf-8"))["files"][0]
    Path(record["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="source identity"):
        prepare_sample_inventory(source, sample, tmp_path / "out")


def test_bounded_orchestration_writes_exact_benchmark_summary(tmp_path):
    source, sample = _fixture(tmp_path)
    summary = run_feasibility(source, sample, tmp_path / "out", methods=("gzip9",),
                              shard_bytes=1024, timeout_seconds=30, max_rss_bytes=1024**3)
    assert summary["validation"]["full_coverage"] is True
    assert summary["benchmark"]["status_counts"] == {"ok": 3}
    assert (tmp_path / "out" / "benchmarks" / "sample" / "benchmark.csv").exists()


def test_benchmark_validation_detects_duplicate_and_tampered_receipts(tmp_path):
    source, sample = _fixture(tmp_path)
    run_feasibility(source, sample, tmp_path / "out", methods=("gzip9",),
                    shard_bytes=1024, timeout_seconds=30, max_rss_bytes=1024**3)
    result_path = tmp_path / "out" / "benchmarks" / "sample" / "benchmark.jsonl"
    lines = result_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["input_bytes"] += 1
    result_path.write_text("\n".join(lines + [json.dumps(tampered)]) + "\n", encoding="utf-8")
    result = validate_benchmark_results(source, tmp_path / "out" / "benchmarks" / "sample",
                                        ("gzip9",), 1024)
    assert result["duplicate_rows"]
    assert result["failures"]
    assert result["full_coverage"] is False


def test_full_reference_requires_both_modes_and_current_sources(tmp_path):
    source, _ = _fixture(tmp_path)
    (tmp_path / "out").mkdir()
    result = reference_full_benchmark(source, tmp_path / "absent", tmp_path / "out")
    assert result["reused_without_rerun"] is False
    entry = json.loads(source.read_text())["files"][0]
    Path(entry["path"]).write_bytes(b"mutated")
    result = reference_full_benchmark(source, tmp_path / "absent", tmp_path / "out")
    assert result["source_failures"] == [entry["sha256"]]
