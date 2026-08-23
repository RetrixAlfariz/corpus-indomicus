from corpus_indomicus.integrity import detect_mime, validate_payload


def test_pdf_signature_beats_wrong_header():
    data = b"%PDF-1.7\nhello"
    assert detect_mime(data, "text/html") == "application/pdf"


def test_html_is_rejected_when_pdf_expected():
    result = validate_payload(
        b"<!doctype html><html></html>",
        content_type="application/pdf",
        expected_mime="application/pdf",
    )
    assert not result.valid
    assert result.detected_mime == "text/html"
