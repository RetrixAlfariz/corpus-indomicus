import json

import pytest

from corpus_indomicus.cli import build_parser, main
from corpus_indomicus.registry import Registry


def test_cli_legacy_and_new_commands():
    parser = build_parser()
    for argv in [["setup"], ["plan", "--all-years"], ["backfill", "--from-year", "2025"],
                 ["sync"], ["retry", "--run-id", "1"], ["references"], ["verify"],
                 ["bpk", "discover", "--year", "2025"], ["bpk", "ingest", "--year", "2025"],
                 ["catalog", "types"], ["discover", "--run-id", "1"],
                 ["ingest", "--run-id", "1", "--limit", "100"], ["status", "--run-id", "1", "--json"]]:
        assert callable(parser.parse_args(argv).func)


def test_setup_status_verify_local(tmp_path, capsys):
    common = ["--config", str(tmp_path / "settings.toml"), "--data-dir", str(tmp_path / "data")]
    assert main(common + ["setup"]) == 0
    assert main(common + ["status", "--json", "--output", str(tmp_path / "coverage.json")]) == 0
    report = json.loads((tmp_path / "coverage.json").read_text())
    assert report["sources"] == 0
    assert main(common + ["verify"]) == 0


def test_ingest_requires_frozen_discovery(tmp_path):
    common = ["--data-dir", str(tmp_path)]
    registry = Registry(tmp_path / "registry/corpus.db")
    registry.initialize()
    run = registry.create_run(mode="backfill", provider="jdih_bpk", from_year=2025,
                              to_year=2025, snapshot_cutoff="test")
    assert main(common + ["ingest", "--run-id", str(run), "--limit", "0"]) == 2


def test_discover_changed_scope_with_run_id_rejected(tmp_path):
    registry = Registry(tmp_path / "registry/corpus.db")
    registry.initialize()
    run, _ = registry.create_scoped_run(mode="backfill", provider="jdih_bpk",
        scope={"groups": ["pusat"], "types": [{"group": "pusat", "type_id": "6"}], "all_years": True},
        snapshot_cutoff="test")
    assert main(["--data-dir", str(tmp_path), "discover", "--run-id", str(run), "--all-years"]) == 2
