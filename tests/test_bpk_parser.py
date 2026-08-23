from corpus_indomicus.sources.jdih_bpk import parse_detail_html, parse_search_html


def test_parse_search_html_deduplicates_details():
    html = """
    <html><body>
      <a href="/Details/234935/uu-no-1-tahun-2023">UU No. 1 Tahun 2023</a>
      <a href="/Details/234935/uu-no-1-tahun-2023">duplicate</a>
    </body></html>
    """
    docs = parse_search_html(html)
    assert len(docs) == 1
    assert docs[0].source_id == "234935"


def test_parse_search_year_ignores_related_old_documents():
    html = """
    <html><body>
      <a href="/Details/350096/uu-no-5-tahun-2026">
        Perubahan Ketiga atas Undang-Undang Nomor 2 Tahun 2002
      </a>
      <a href="/Details/246523/uu-no-6-tahun-2023">UU No. 6 Tahun 2023</a>
      <a href="/Details/149750/uu-no-11-tahun-2020">UU No. 11 Tahun 2020</a>
      <a href="/Details/44418/uu-no-2-tahun-2002">UU No. 2 Tahun 2002</a>
      <a href="/Details/350166/uu-no-4-tahun-2026">UU No. 4 Tahun 2026</a>
    </body></html>
    """
    docs = parse_search_html(html, expected_year=2026)
    assert [doc.source_id for doc in docs] == ["350096", "350166"]


def test_parse_detail_table_pdf_and_neutral_references():
    html = """
    <html><body>
      <h1>Perubahan Kepolisian</h1>
      <table>
        <tr><th>Judul</th><td>Undang-undang (UU) Nomor 5 Tahun 2026</td></tr>
        <tr><th>Bentuk Singkat</th><td>UU</td></tr>
        <tr><th>Nomor</th><td>5</td></tr>
        <tr><th>Tahun</th><td>2026</td></tr>
        <tr><th>Status</th><td>Berlaku</td></tr>
        <tr><th>Tanggal Penetapan</th><td>02 Januari 2026</td></tr>
      </table>

      <section>
        <h3>Status</h3>
        <div>
          <strong>Mengubah</strong>
          <a href="/Details/246523/uu-no-6-tahun-2023">UU No. 6 Tahun 2023</a>
          <a href="/Details/149750/uu-no-11-tahun-2020">UU No. 11 Tahun 2020</a>
        </div>
      </section>

      <a href="/Details/350096/uu-no-5-tahun-2026">self</a>
      <a href="/Download/uu5-2026.pdf">Download</a>
    </body></html>
    """
    detail = parse_detail_html(
        html,
        "https://peraturan.bpk.go.id/Details/350096/uu-no-5-tahun-2026",
    )

    assert detail.instrument is not None
    assert detail.instrument.id == "ID:UU:2026:5"
    assert detail.instrument.status == "Berlaku"
    assert detail.instrument.dates["enacted"] == "2026-01-02"
    assert detail.file_urls == ["https://peraturan.bpk.go.id/Download/uu5-2026.pdf"]

    assert len(detail.references) == 2
    first = detail.references[0]
    assert first.source_id == "350096"
    assert first.target_source_id == "246523"
    assert first.target_label == "UU No. 6 Tahun 2023"
    assert first.context_label == "Mengubah"
    assert "Mengubah" in (first.raw_context or "")
    assert not hasattr(first, "relation_type")


def test_regional_location_is_part_of_identity():
    html = """
    <html><body><table>
      <tr><th>Judul</th><td>Peraturan Walikota Mojokerto Nomor 6 Tahun 2026</td></tr>
      <tr><th>Bentuk Singkat</th><td>Perwali</td></tr>
      <tr><th>Nomor</th><td>6</td></tr>
      <tr><th>Tahun</th><td>2026</td></tr>
      <tr><th>Lokasi</th><td>Kota Mojokerto</td></tr>
    </table></body></html>
    """
    detail = parse_detail_html(
        html,
        "https://peraturan.bpk.go.id/Details/999999/example",
    )
    assert detail.instrument is not None
    assert detail.instrument.id == "ID_KOTA_MOJOKERTO:PERWALI:2026:6"
    assert detail.instrument.jurisdiction == "ID/KOTA_MOJOKERTO"
