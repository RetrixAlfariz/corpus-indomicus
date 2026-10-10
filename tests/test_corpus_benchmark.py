import hashlib
import json
import sys
from pathlib import Path

import pytest

# ``tools`` is an intentional repository-level namespace rather than an
# installed package; make the checkout root explicit for src-layout pytest.
sys.path.insert(0, str(Path(__file__).parents[1]))

from tools.corpus_profile.benchmark import (
    METHODS,
    benchmark_unit,
    bounded_files,
    load_inventory,
    make_units,
    run_benchmark,
)


def _inventory(tmp_path: Path, count: int = 3) -> Path:
    records = []
    for index in range(count):
        data = (f"pdf-{index}-".encode() * (index + 3)) + b"\n%%EOF\n"
        path = tmp_path / f"{index}.pdf"
        path.write_bytes(data)
        records.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                        "logical_size": len(data)})
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"unique_pdf_count": count, "files": records}), encoding="utf-8")
    return inventory


def test_inventory_limits_and_shard_boundaries(tmp_path):
    inventory = _inventory(tmp_path, 3)
    files = load_inventory(inventory)
    assert len(bounded_files(files, max_docs=2)) == 2
    assert sum(f.size for f in bounded_files(files, max_input_bytes=files[0].size)) == files[0].size
    units = make_units(files, "shard", shard_bytes=files[0].size + files[1].size)
    assert [len(unit.files) for unit in units] == [2, 1]
    assert bounded_files(files, max_input_bytes=1) == []


@pytest.mark.parametrize("method", METHODS)
def test_each_method_reconstructs_exact_document(tmp_path, method):
    inventory = _inventory(tmp_path, 1)
    unit = make_units(load_inventory(inventory), "perdoc")[0]
    row = benchmark_unit(unit, method, tmp_path)
    assert row["status"] == "ok"
    assert row["exact"] is True
    assert row["output_bytes"] == row["payload_bytes"]
    assert row["independent_access"] is True


def test_shard_manifest_is_counted_and_exact(tmp_path):
    inventory = _inventory(tmp_path, 3)
    unit = make_units(load_inventory(inventory), "shard", shard_bytes=10_000)[0]
    row = benchmark_unit(unit, "gzip9", tmp_path)
    assert row["exact"] is True
    assert row["manifest_bytes"] > 0
    assert row["output_bytes"] == row["payload_bytes"] + row["manifest_bytes"]
    assert row["independent_access"] is False


def test_isolated_shard_large_receipt_does_not_deadlock(tmp_path):
    inventory = _inventory(tmp_path, 120)
    output = tmp_path / "results"
    rows = run_benchmark(inventory, output, methods=("gzip9",), modes=("shard",),
                         temp_dir=tmp_path, progress=False)
    assert rows[0]["exact"] is True
    assert len(rows[0]["reconstructed_sha256"]) == 120


def test_run_is_resumable_and_writes_csv_jsonl(tmp_path):
    inventory = _inventory(tmp_path, 2)
    output = tmp_path / "results"
    first = run_benchmark(inventory, output, methods=("gzip9",), modes=("perdoc",),
                          max_docs=1, temp_dir=tmp_path)
    second = run_benchmark(inventory, output, methods=("gzip9",), modes=("perdoc",),
                           max_docs=1, temp_dir=tmp_path)
    assert len(first) == len(second) == 1
    assert len((output / "benchmark.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert (output / "benchmark.csv").exists()


def test_resume_discards_only_truncated_final_checkpoint_and_checks_identity(tmp_path):
    inventory = _inventory(tmp_path, 1)
    output = tmp_path / "results"
    run_benchmark(inventory, output, methods=("gzip9",), modes=("perdoc",),
                  temp_dir=tmp_path, progress=False)
    checkpoint = output / "benchmark.jsonl"
    checkpoint.write_text(checkpoint.read_text(encoding="utf-8") + '{"partial":', encoding="utf-8")
    rows = run_benchmark(inventory, output, methods=("gzip9",), modes=("perdoc",),
                         temp_dir=tmp_path, progress=False)
    assert len(rows) == 1
    rows_again = run_benchmark(inventory, output, methods=("gzip9",), modes=("perdoc",),
                               temp_dir=tmp_path, progress=False)
    assert len(rows_again) == 1
    with pytest.raises(ValueError, match="different benchmark identity"):
        run_benchmark(inventory, output, methods=("gzip9",), modes=("shard",),
                      temp_dir=tmp_path, progress=False)
