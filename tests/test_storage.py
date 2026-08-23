from corpus_indomicus.storage import RawArchive


def test_raw_archive_is_content_addressed(tmp_path):
    archive = RawArchive(tmp_path)
    data = b"%PDF-1.7\nhello"

    first = archive.store(
        provider="test",
        source_url="https://example.test/a.pdf",
        data=data,
        retrieved_at="2026-08-23T00:00:00+00:00",
        content_type="application/pdf",
    )
    second = archive.store(
        provider="test",
        source_url="https://example.test/b.pdf",
        data=data,
        retrieved_at="2026-08-23T00:00:00+00:00",
        content_type="application/pdf",
    )

    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert first.path.read_bytes() == data
