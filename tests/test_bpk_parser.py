from corpus_indomicus.sources.jdih_bpk import parse_detail_html, parse_search_page


def test_search_year_rejects_past_reference_links_and_reads_total():
    html = """
    <html><body>
      <div>Menemukan 2 peraturan</div>
      <a href="/Details/350096/uu-no-5-tahun-2026">Perubahan Ketiga atas UU ...</a>
      <a href="/Details/246523/uu-no-6-tahun-2023">UU No. 6 Tahun 2023</a>
      <a href="/Details/350166/uu-no-4-tahun-2026">Perubahan atas UU ...</a>
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
