import pytest

from tools.corpus_profile.feasibility_report import aggregate_benchmarks, verify_evidence_identity
from tools.corpus_profile.storage_streams import VERSION


def test_aggregate_is_byte_weighted_including_sidecar():
    rows = [dict(mode="shard", method="zstd9", status="ok", exact=True, input_bytes=100,
                 output_bytes=60, manifest_bytes=10, compress_seconds=1, decompress_seconds=2, peak_rss_bytes=30),
            dict(mode="shard", method="zstd9", status="ok", exact=True, input_bytes=900,
                 output_bytes=840, manifest_bytes=40, compress_seconds=2, decompress_seconds=1, peak_rss_bytes=40)]
    row = aggregate_benchmarks(rows, "test")[0]
    assert row["saving_pct"] == pytest.approx(10)
    assert row["manifest_bytes"] == 50 and row["peak_rss_bytes"] == 40


def test_report_rejects_mixed_manifest_or_pending_review():
    manifest = {"manifest_sha256": "source", "count": 1}
    streams = {"manifest_sha256": "source", "profiled": 1, "failed": 0, "profiler_version": VERSION}
    audit = {"manifest_sha256": "different", "pages": [{"review_status": "reviewed"}]}
    with pytest.raises(ValueError, match="manifest"):
        verify_evidence_identity(manifest, streams, audit)
    audit["manifest_sha256"] = "source"
    audit["pages"][0]["review_status"] = "not_visually_reviewed"
    with pytest.raises(ValueError, match="pending"):
        verify_evidence_identity(manifest, streams, audit)
