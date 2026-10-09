from corpus_indomicus.storage import RawArchive


def test_storage_deduplicates_exact_bytes_and_compresses_html(tmp_path):
    archive = RawArchive(tmp_path / "objects")
    html = ("<html><body>same words " * 100 + "</body></html>").encode()
    first = archive.store(
        provider="a", source_url="https://a", data=html,
        retrieved_at="2026-08-24T00:00:00+00:00", content_type="text/html",
    )
    second = archive.store(
        provider="b", source_url="https://b", data=html,
        retrieved_at="2026-08-24T00:01:00+00:00", content_type="text/html",
    )
    assert first.sha256 == second.sha256
    assert first.was_new is True
    assert second.was_new is False
    assert first.compression == "gzip"
    assert first.stored_size < first.logical_size
    assert RawArchive.read_object(first.path, compression="gzip") == html


def test_pdf_bytes_are_stored_exactly(tmp_path):
    archive = RawArchive(tmp_path / "objects")
    pdf = b"%PDF-1.7\nexact"
    stored = archive.store(
        provider="x", source_url="https://x/file.pdf", data=pdf,
        retrieved_at="2026-08-24T00:00:00+00:00", content_type="application/pdf",
    )
    assert stored.compression is None
    assert stored.path.read_bytes() == pdf
