from corpus_indomicus.models import LegalInstrument, make_instrument_id


def test_deterministic_identity():
    assert make_instrument_id("UU", 2022, "27") == "ID:UU:2022:27"


def test_minimal_instrument():
    instrument = LegalInstrument.from_minimal(
        document_type="Peraturan BPK",
        number="2",
        year=2026,
        title="Kode Etik Badan Pemeriksa Keuangan",
    )
    assert instrument.id == "ID:PERATURAN_BPK:2026:2"
