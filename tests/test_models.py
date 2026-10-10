from corpus_indomicus.models import LegalInstrument, make_instrument_id


def test_deterministic_central_identity():
    assert make_instrument_id("UU", 2022, "27") == "ID:UU:2022:27"


def test_issuer_qualified_candidates_do_not_collide():
    assert make_instrument_id("Permen", 2025, "1", issuing_body="Kementerian A") != make_instrument_id(
        "Permen", 2025, "1", issuing_body="Kementerian B")


def test_regional_identity_includes_jurisdiction():
    instrument = LegalInstrument.from_minimal(
        document_type="Perwali",
        number="6",
        year=2026,
        title="Example",
        jurisdiction="ID/KOTA_MOJOKERTO",
    )
    assert instrument.id == "ID_KOTA_MOJOKERTO:PERWALI:2026:6"
