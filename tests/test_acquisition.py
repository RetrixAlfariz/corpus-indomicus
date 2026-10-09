from dataclasses import dataclass

from corpus_indomicus.acquisition import AcquisitionPipeline
from corpus_indomicus.sources.base import DiscoveredDocument
from corpus_indomicus.sources.jdih_bpk import BpkSearchPage, parse_detail_html


@dataclass
class FakeResponse:
    content: bytes
    headers: dict

    @property
    def text(self):
        return self.content.decode()


class FakeConnector:
    provider = "jdih_bpk"

    def search_page(self, *, query=None, year=None, page=1):
        if page == 1:
            return BpkSearchPage([
                DiscoveredDocument("jdih_bpk", "100", "https://x/Details/100/law", "Law", {"year": year})
            ], reported_total=1)
        return BpkSearchPage([], reported_total=1)

    def fetch_detail(self, document):
        html = b"""<html><body><table>
        <tr><th>Judul</th><td>Undang-undang Nomor 1 Tahun 2025</td></tr>
        <tr><th>Bentuk Singkat</th><td>UU</td></tr>
        <tr><th>Nomor</th><td>1</td></tr>
        <tr><th>Tahun</th><td>2025</td></tr>
        </table><a href='https://x/file.pdf'>Download</a></body></html>"""
        response = FakeResponse(html, {"content-type": "text/html"})
        return response, parse_detail_html(response.text, document.detail_url)

    def fetch_file(self, url):
        return FakeResponse(b"%PDF-1.7\ncontent", {"content-type": "application/pdf"})


def test_snapshot_manifest_then_processing(tmp_path):
    pipeline = AcquisitionPipeline(data_dir=tmp_path, min_free_bytes=0)
    run_id, resumed = pipeline.create_or_resume_run(
        mode="backfill", provider="jdih_bpk", from_year=2025, to_year=2025,
        snapshot_cutoff="2026-08-24T04:00:00+07:00",
    )
    assert not resumed
    connector = FakeConnector()
    pipeline.discover_snapshot(connector, run_id)
    assert pipeline.registry.manifest_count(run_id) == 1
    assert pipeline.registry.get_run(run_id)["status"] == "ready"

    summary = pipeline.process_run(connector, run_id)
    assert summary.ingested_instruments == 1
    assert summary.invalid_files == 0
    assert pipeline.registry.get_run(run_id)["status"] == "complete"
    stats = pipeline.registry.stats()
    assert stats["instruments"] == 1
    assert stats["unique_content_hashes"] == 2
