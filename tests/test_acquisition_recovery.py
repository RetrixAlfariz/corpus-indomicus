import pytest

from corpus_indomicus.acquisition import AcquisitionPipeline
from corpus_indomicus.integrity import sha256_bytes
from corpus_indomicus.sources.base import DiscoveredDocument
from corpus_indomicus.sources.jdih_bpk import BpkSearchPage
from test_acquisition import FakeConnector, FakeResponse


def ready_run(pipeline, count=1):
    run = pipeline.registry.create_run(mode="backfill", provider="jdih_bpk",
        from_year=2025, to_year=2025, snapshot_cutoff="test")
    pipeline.registry.add_manifest_items(run, 2025, [
        DiscoveredDocument("jdih_bpk", str(i), f"https://x/Details/{i}", metadata={"year": 2025})
        for i in range(count)])
    pipeline.registry.update_segment(run, 2025, status="complete")
    pipeline.registry.freeze_manifest(run)
    return run


@pytest.mark.parametrize("mode,expected", [("no_file", "no_file"), ("incomplete", "partial"),
    ("html", "partial"), ("missing_metadata", "unresolved"), ("multiple", "done")])
def test_file_completeness_and_missing_metadata(tmp_path, mode, expected):
    class Connector(FakeConnector):
        def fetch_detail(self, document):
            response, detail = super().fetch_detail(document)
            if mode == "no_file":
                detail.file_urls = []
            elif mode == "missing_metadata":
                detail.instrument = None
            else:
                detail.file_urls.append("https://x/attachment.pdf")
            return response, detail

        def fetch_file(self, url):
            if url.endswith("attachment.pdf"):
                if mode == "incomplete":
                    raise ConnectionError("interrupted download")
                if mode == "html":
                    return FakeResponse(b"<html>error</html>", {"content-type": "application/pdf"})
            return super().fetch_file(url)
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    run = ready_run(pipeline)
    pipeline.process_run(Connector(), run)
    assert pipeline.registry.run_counts(run)[expected] == 1
    assert pipeline.registry.run_counts(run).get("done", 0) == (expected == "done")


def test_limit_and_interrupted_item_resume(tmp_path):
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    run = ready_run(pipeline, 3)
    first = pipeline.registry.manifest_items(run)[0]
    pipeline.registry.mark_manifest_item(first["id"], "running")
    assert pipeline.process_run(FakeConnector(), run, limit=1).processed == 1
    assert pipeline.registry.get_run(run)["status"] == "partial"
    assert pipeline.registry.run_counts(run)["pending"] == 2
    pipeline.process_run(FakeConnector(), run)
    assert pipeline.registry.run_counts(run)["done"] == 3
    with pipeline.registry.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM instruments")]
    assert len(set(ids)) == 3  # same type/year/number never merges source records


def test_receipt_recovers_after_object_written_before_db_commit(tmp_path, monkeypatch):
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    response = FakeResponse(b"%PDF-1.7\noriginal\n%%EOF", {"content-type": "application/pdf"})
    monkeypatch.setattr(pipeline.registry, "record_object", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        pipeline._store_response(provider="jdih_bpk", source_id="1", source_url="https://x/file",
            response=response, kind="document_file", instrument_id=None)
    recovered = AcquisitionPipeline(data_dir=tmp_path)
    assert recovered.registry.hash_seen(sha256_bytes(response.content))
    row = recovered.registry.iter_objects()[0]
    assert row["stored_size"] == len(response.content)
    assert recovered.archive.read_object(row["path"]) == response.content
    assert recovered.registry.source_seen("jdih_bpk", "https://x/file")
    assert not list((tmp_path / "receipts").glob("*.json"))


def scoped_run(pipeline):
    scope = {"groups": ["pusat"], "types": [{"group": "pusat", "type_id": "6"}],
             "all_years": True, "query": None}
    return pipeline.registry.create_scoped_run(mode="backfill", provider="jdih_bpk",
        scope=scope, snapshot_cutoff="test")[0]


def test_scoped_checkpoint_resume_and_page_limit(tmp_path):
    class Connector:
        def search_page(self, *, type_id, year, query, page):
            return BpkSearchPage([DiscoveredDocument("jdih_bpk", str(page), f"https://x/Details/{page}")],
                reported_total=2, has_more=page < 2, next_page=page + 1 if page < 2 else None)
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    run = scoped_run(pipeline)
    pipeline.discover_scoped(Connector(), run, max_pages=1)
    assert not pipeline.registry.get_run(run)["frozen_at"]
    assert pipeline.registry.partitions(run)[0]["next_page"] == 2
    with pytest.raises(ValueError):
        pipeline.process_run(FakeConnector(), run)
    pipeline.discover_scoped(Connector(), run)
    assert pipeline.registry.get_run(run)["frozen_at"]
    assert pipeline.registry.manifest_count(run) == 2


@pytest.mark.parametrize("result", [BpkSearchPage([], parser_ok=False),
    BpkSearchPage([], reported_total=100, has_more=False), BpkSearchPage([], has_more=None)])
def test_scoped_discovery_fails_closed(tmp_path, result):
    class Connector:
        def search_page(self, **kwargs):
            return result
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    run = scoped_run(pipeline)
    with pytest.raises(RuntimeError):
        pipeline.discover_scoped(Connector(), run)
    assert not pipeline.registry.get_run(run)["frozen_at"]
    assert pipeline.registry.get_run(run)["status"] == "partial"


def test_replayed_page_does_not_inflate_membership_count(tmp_path, monkeypatch):
    class Connector:
        def search_page(self, **kwargs):
            return BpkSearchPage([DiscoveredDocument("jdih_bpk", "1", "https://x/Details/1")],
                                 reported_total=1, has_more=False)
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    run = scoped_run(pipeline)
    original = pipeline.registry.update_partition
    interrupted = False
    def update(*args, **kwargs):
        nonlocal interrupted
        if not interrupted and kwargs.get("status") == "complete":
            interrupted = True
            raise RuntimeError("checkpoint crash")
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline.registry, "update_partition", update)
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        pipeline.discover_scoped(Connector(), run)
    pipeline.discover_scoped(Connector(), run)
    assert pipeline.registry.partitions(run)[0]["discovered_count"] == 1
    assert pipeline.registry.manifest_count(run) == 1


def test_archive_writer_lock_releases_after_exception(tmp_path):
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    with pipeline._run_lock(1):
        with pytest.raises(RuntimeError, match="already active"):
            with pipeline._run_lock(2):
                pass
    with pipeline._run_lock(2):
        pass


def test_partial_retries_even_when_detail_unchanged(tmp_path):
    class Connector(FakeConnector):
        broken = True
        def fetch_file(self, url):
            if self.broken:
                return FakeResponse(b"%PDF-1.7\ntruncated", {"content-type": "application/pdf"})
            return super().fetch_file(url)
    pipeline = AcquisitionPipeline(data_dir=tmp_path)
    run = ready_run(pipeline)
    connector = Connector()
    pipeline.process_run(connector, run)
    assert pipeline.registry.run_counts(run)["partial"] == 1
    connector.broken = False
    pipeline.process_run(connector, run, refresh_known=True, retry_failed=True)
    assert pipeline.registry.run_counts(run)["done"] == 1
