from pathlib import Path
from corpus_indomicus.sources.jdih_bpk import parse_catalog_types, parse_detail_html, parse_search_page

FIXTURES = Path(__file__).parent / "fixtures"


def test_bootstrap_metadata_is_not_overwritten_by_footer():
    html = ''.join(f'<div class="row"><div>{k}</div><div>{v}</div></div>' for k, v in
                   [('Judul', 'Peraturan tahun 2026'), ('Bentuk Singkat', 'UU'), ('Nomor', '5'), ('Tahun', '2026')])
    detail = parse_detail_html(html + '<footer>Tahun<span>Perwakilan</span></footer>', 'https://peraturan.bpk.go.id/Details/1/x')
    assert detail.instrument is not None
    assert detail.instrument.id == 'ID:UU:2026:5'


def test_connector_uses_real_pagination_parameter():
    from urllib.parse import parse_qs, urlsplit
    from corpus_indomicus.sources.jdih_bpk import JdihBpkConnector
    class Client:
        def get(self, url):
            params = parse_qs(urlsplit(url).query)
            assert params['p'] == ['2']
            assert params['tahun'] == ['2026']
            assert 'page' not in params
            return type('Response', (), {'text': '<html></html>'})()
    JdihBpkConnector(Client()).search_page(year=2026, page=2)


def test_search_year_rejects_past_reference_links_and_reads_total():
    html = """
    <html><body>
      <div>Menemukan 2 peraturan</div>
      <div class="card"><a class="result-title" href="/Details/350096/uu-no-5-tahun-2026">Perubahan Ketiga atas UU ...</a></div>
      <div class="card"><a class="text-danger" href="/Details/246523/uu-no-6-tahun-2023">UU No. 6 Tahun 2023</a></div>
      <div class="card"><a class="result-title" href="/Details/350166/uu-no-4-tahun-2026">Perubahan atas UU ...</a></div>
    </body></html>
    """
    page = parse_search_page(html, expected_year=2026)
    assert page.reported_total == 2
    assert [d.source_id for d in page.documents] == ["350096", "350166"]


def test_detail_extracts_neutral_reference_without_semantic_type():
    html = """
    <html><body>
      <table>
        <tr><th>Judul</th><td>Undang-undang Nomor 5 Tahun 2026</td></tr>
        <tr><th>Bentuk Singkat</th><td>UU</td></tr>
        <tr><th>Nomor</th><td>5</td></tr>
        <tr><th>Tahun</th><td>2026</td></tr>
      </table>
      <div><strong>Mengubah</strong>
        <a href="/Details/246523/uu-no-6-tahun-2023">UU No. 6 Tahun 2023</a>
      </div>
      <a href="/Download/law.pdf">Download</a>
    </body></html>
    """
    detail = parse_detail_html(html, "https://peraturan.bpk.go.id/Details/350096/x")
    assert detail.instrument is not None
    assert detail.instrument.id == "ID:UU:2026:5"
    assert len(detail.references) == 1
    ref = detail.references[0]
    assert ref.target_source_id == "246523"
    assert ref.context_label == "Mengubah"
    assert not hasattr(ref, "relation_type")
    assert detail.file_urls == ["https://peraturan.bpk.go.id/Download/law.pdf"]


def test_search_cards_exclude_same_year_and_other_year_references():
    page = parse_search_page((FIXTURES / "bpk_search_references.html").read_text(), expected_year=2026)
    assert [item.source_id for item in page.documents] == ["350096"]
    assert page.parser_ok and page.is_last_page is None


def test_search_parser_fails_closed_when_result_dom_is_unknown():
    page = parse_search_page('<a href="/Details/1/unknown">A</a>')
    assert page.documents == []
    assert page.parser_ok is False
    assert page.diagnostics


def test_catalog_uses_query_type_id_and_local_reported_total():
    catalog = parse_catalog_types((FIXTURES / "bpk_category.html").read_text(), "pusat")
    assert [(item.type_id, item.reported_total) for item in catalog] == [("8", 1927), ("36", 174)]
    assert all(item.classification == "pusat" for item in catalog)


def test_catalog_preserves_ministry_names_with_daerah_and_flags_regional():
    html = '''<div><div class="display-6">Peraturan Kementerian / Lembaga</div>
    <div><a href="/Search?extra=1&amp;jenis=201">Peraturan Menteri Desa dan Pembangunan Daerah Tertinggal</a></div>
    <div><a href="/Search?jenis=300">Peraturan Daerah</a></div></div>
    <footer><a href="/Search?jenis=400">Unrelated</a></footer>'''
    types = parse_catalog_types(html, "lembaga")
    assert [(t.type_id, t.classification) for t in types] == [("201", "lembaga"), ("300", "daerah")]
    mismatch = parse_catalog_types(html, "pusat")
    assert mismatch[0].classification == "unresolved"


def test_year_slug_is_only_a_hint_and_detail_proves_year():
    page = parse_search_page('<div class="card"><a class="result-title" href="/Details/123/x-tahun-2020">Tahun 2025</a></div>', expected_year=2025)
    assert page.documents[0].metadata["url_year_hint"] == 2020
    assert page.documents[0].metadata["year_scope_unverified"]


def test_detail_separates_pdf_viewer_and_attachments():
    detail = parse_detail_html('<a href="/Read/1/x.pdf">Read</a><a href="/Download/1/x.pdf">Download</a><a href="/Download/2/lampiran.pdf">Lampiran</a>', 'https://peraturan.bpk.go.id/Details/1/x')
    assert detail.file_urls == ['https://peraturan.bpk.go.id/Download/1/x.pdf', 'https://peraturan.bpk.go.id/Download/2/lampiran.pdf']


def test_search_pagination_exposes_next_page_and_not_end():
    html = '''<div class="card"><a class="result-title" href="/Details/1/u-tahun-2026">A</a></div>
    <a href="/Search?p=2">Berikutnya</a>'''
    page = parse_search_page(html, expected_year=2026)
    assert page.next_page == 2
    assert page.has_more is True
    assert page.is_last_page is False


def test_search_retains_primary_when_url_year_is_unknown_and_rejects_foreign_url():
    html = '''<div class="card"><a class="result-title" href="/Details/44/undang-undang">Nomor 4 Tahun 2026</a></div>
    <div class="card"><a class="result-title" href="https://evil.example/Details/45/u-tahun-2026">Foreign</a></div>'''
    page = parse_search_page(html, expected_year=2026)
    assert [item.source_id for item in page.documents] == ["44"]
    assert page.documents[0].metadata["year_scope_unverified"] is True


def test_pager_ignores_disabled_next_and_advances_one_page():
    def html(current):
        links = "".join(f'<li class="{"active" if i == current else ""}"><a href="/Search?p={i}">{i}</a></li>' for i in range(1, 6))
        return f'<div class="card"><a class="result-title" href="/Details/{current}/u">A</a></div><ul class="pagination">{links}<li class="page-item disabled"><a href="/Search?p=5">Next</a></li></ul>'
    first = parse_search_page(html(1), page=1)
    middle = parse_search_page(html(3), page=3)
    last = parse_search_page(html(5), page=5)
    assert first.next_page == 2 and first.has_more is True
    assert middle.next_page == 4 and middle.has_more is True
    assert last.next_page is None and last.has_more is False and last.is_last_page is True
    assert parse_search_page(html(1), page=5).parser_ok is False
