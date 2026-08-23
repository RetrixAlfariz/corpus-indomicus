from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import DocumentReference, LegalInstrument, SourceObservation, utc_now_iso


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,
    document_type TEXT NOT NULL,
    number TEXT NOT NULL,
    year INTEGER NOT NULL,
    title TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    issuing_body TEXT,
    status TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_instrument_identity
ON instruments(jurisdiction, document_type, year, number);

CREATE TABLE IF NOT EXISTS source_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT,
    provider TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_id TEXT,
    retrieved_at TEXT NOT NULL,
    content_sha256 TEXT,
    mime_type TEXT,
    raw_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL,
    UNIQUE(provider, source_url, content_sha256)
);

CREATE INDEX IF NOT EXISTS idx_source_url
ON source_observations(provider, source_url);

CREATE INDEX IF NOT EXISTS idx_source_hash
ON source_observations(content_sha256);

CREATE TABLE IF NOT EXISTS document_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT,
    provider TEXT NOT NULL,
    source_id TEXT,
    source_url TEXT NOT NULL,
    target_source_id TEXT,
    target_url TEXT NOT NULL,
    target_label TEXT,
    context_label TEXT,
    raw_context TEXT,
    observed_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL,
    UNIQUE(provider, source_url, target_url, context_label, raw_context)
);

CREATE INDEX IF NOT EXISTS idx_reference_source
ON document_references(provider, source_url);

CREATE INDEX IF NOT EXISTS idx_reference_target
ON document_references(provider, target_url);
"""


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_instrument(self, instrument: LegalInstrument) -> None:
        now = utc_now_iso()
        metadata = {
            "dates": instrument.dates,
            "publication": instrument.publication,
            **instrument.metadata,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO instruments (
                    id, document_type, number, year, title, jurisdiction,
                    issuing_body, status, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    document_type=excluded.document_type,
                    number=excluded.number,
                    year=excluded.year,
                    title=excluded.title,
                    jurisdiction=excluded.jurisdiction,
                    issuing_body=COALESCE(excluded.issuing_body, instruments.issuing_body),
                    status=COALESCE(excluded.status, instruments.status),
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    instrument.id,
                    instrument.document_type,
                    instrument.number,
                    instrument.year,
                    instrument.title,
                    instrument.jurisdiction,
                    instrument.issuing_body,
                    instrument.status,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def record_source(
        self,
        observation: SourceObservation,
        instrument_id: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_observations (
                    instrument_id, provider, source_url, source_id,
                    retrieved_at, content_sha256, mime_type, raw_path,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument_id,
                    observation.provider,
                    observation.source_url,
                    observation.source_id,
                    observation.retrieved_at,
                    observation.content_sha256,
                    observation.mime_type,
                    observation.raw_path,
                    json.dumps(observation.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )

    def record_reference(
        self,
        reference: DocumentReference,
        *,
        instrument_id: str | None = None,
        observed_at: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO document_references (
                    instrument_id, provider, source_id, source_url,
                    target_source_id, target_url, target_label,
                    context_label, raw_context, observed_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instrument_id,
                    reference.provider,
                    reference.source_id,
                    reference.source_url,
                    reference.target_source_id,
                    reference.target_url,
                    reference.target_label,
                    reference.context_label or "",
                    reference.raw_context or "",
                    observed_at or utc_now_iso(),
                    json.dumps(reference.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )

    def source_seen(self, provider: str, source_url: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM source_observations
                WHERE provider = ? AND source_url = ?
                LIMIT 1
                """,
                (provider, source_url),
            ).fetchone()
        return row is not None

    def hash_seen(self, content_sha256: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM source_observations
                WHERE content_sha256 = ?
                LIMIT 1
                """,
                (content_sha256,),
            ).fetchone()
        return row is not None

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            instruments = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
            observations = conn.execute(
                "SELECT COUNT(*) FROM source_observations"
            ).fetchone()[0]
            references = conn.execute(
                "SELECT COUNT(*) FROM document_references"
            ).fetchone()[0]
            unique_hashes = conn.execute(
                """
                SELECT COUNT(DISTINCT content_sha256)
                FROM source_observations
                WHERE content_sha256 IS NOT NULL
                """
            ).fetchone()[0]
        return {
            "instruments": int(instruments),
            "source_observations": int(observations),
            "document_references": int(references),
            "unique_content_hashes": int(unique_hashes),
        }
