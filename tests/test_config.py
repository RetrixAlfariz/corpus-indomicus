from corpus_indomicus.config import load_config, write_default_config


def test_default_config_is_pilot_2025(tmp_path):
    path = tmp_path / "corpus.toml"
    write_default_config(path)
    cfg = load_config(path)
    assert cfg.archive.pilot_from_year == 2025
    assert cfg.archive.min_free_gb == 100.0
