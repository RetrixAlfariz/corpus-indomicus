import json
import sqlite3

from tools.corpus_profile.profiler import write_json
from tools.corpus_profile.report import aggregate_benchmarks, write_report


def test_byte_weighted_benchmark_aggregation_does_not_hide_failed_units(tmp_path):
    folder = tmp_path / "benchmarks" / "full"
    folder.mkdir(parents=True)
    common = {"mode": "shard", "method": "gzip9", "status": "ok", "exact": True,
              "manifest_bytes": 1, "compress_seconds": 1, "decompress_seconds": .5,
              "peak_rss_bytes": 100}
    rows = [dict(common, unit_id="one", input_bytes=10, output_bytes=9, saving=.1),
            dict(common, unit_id="two", input_bytes=90, output_bytes=45, saving=.5),
            {"unit_id": "three", "mode": "shard", "method": "gzip9", "status": "timeout", "input_bytes": 20, "error": "timeout"}]
    (folder / "benchmark.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    result = aggregate_benchmarks(tmp_path)[0]
    assert result["saving_pct"] == 46
    assert result["successful_units"] == 2
    assert result["units"] == 3
    assert result["input_bytes"] == 100
    assert result["manifest_bytes"] == 2
    assert result["errors"][0]["status"] == "timeout"


def test_report_all_failed_has_no_unsupported_conclusions(tmp_path):
    write_json(tmp_path / "summary.json", {"successful": 0})
    assert write_report(tmp_path) == []
    assert "No PDF successfully profiled" in (tmp_path / "RESEARCH_REPORT.md").read_text()
