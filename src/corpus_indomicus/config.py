from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import tomllib


@dataclass(slots=True)
class ArchiveConfig:
    data_dir: str = "data"
    pilot_from_year: int = 2025
    sync_overlap_years: int = 1
    min_free_gb: float = 100.0


@dataclass(slots=True)
class SourceConfig:
    enabled: bool = True
    delay: float = 1.0
    max_retries: int = 3
    timeout: float = 30.0


@dataclass(slots=True)
class AppConfig:
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    jdih_bpk: SourceConfig = field(default_factory=SourceConfig)

    @property
    def current_year(self) -> int:
        return date.today().year


def default_config_text() -> str:
    return """# Corpus Indomicus v1\n# Acquisition pilot defaults to Indonesian regulations from 2025 onward.\n\n[archive]\ndata_dir = \"data\"\npilot_from_year = 2025\nsync_overlap_years = 1\nmin_free_gb = 100.0\n\n[sources.jdih_bpk]\nenabled = true\ndelay = 1.0\nmax_retries = 3\ntimeout = 30.0\n"""


def load_config(path: str | Path = "corpus.toml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        return AppConfig()

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    archive_raw = raw.get("archive", {})
    sources = raw.get("sources", {})
    bpk_raw = sources.get("jdih_bpk", {})

    return AppConfig(
        archive=ArchiveConfig(
            data_dir=str(archive_raw.get("data_dir", "data")),
            pilot_from_year=int(archive_raw.get("pilot_from_year", 2025)),
            sync_overlap_years=max(0, int(archive_raw.get("sync_overlap_years", 1))),
            min_free_gb=max(0.0, float(archive_raw.get("min_free_gb", 100.0))),
        ),
        jdih_bpk=SourceConfig(
            enabled=bool(bpk_raw.get("enabled", True)),
            delay=max(0.0, float(bpk_raw.get("delay", 1.0))),
            max_retries=max(0, int(bpk_raw.get("max_retries", 3))),
            timeout=max(1.0, float(bpk_raw.get("timeout", 30.0))),
        ),
    )


def write_default_config(path: str | Path = "corpus.toml", *, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        return path
    path.write_text(default_config_text(), encoding="utf-8")
    return path
