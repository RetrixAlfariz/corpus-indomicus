from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator

from .models import DocumentReference, LegalInstrument, SourceObservation, utc_now_iso
from .sources.base import DiscoveredDocument
from .storage import StoredObject


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
CREATE INDEX IF NOT EXISTS idx_source_url ON source_observations(provider, source_url);
CREATE INDEX IF NOT EXISTS idx_source_hash ON source_observations(content_sha256);

CREATE TABLE IF NOT EXISTS source_records (
    provider TEXT NOT NULL,
    source_id TEXT NOT NULL,
    detail_url TEXT NOT NULL,
    title_hint TEXT,
    instrument_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    latest_detail_sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(provider, source_id),
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_source_record_url ON source_records(provider, detail_url);

CREATE TABLE IF NOT EXISTS document_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT,
    provider TEXT NOT NULL,
    source_id TEXT,
    source_url TEXT NOT NULL,
    target_source_id TEXT,
    target_url TEXT NOT NULL,
    target_label TEXT,
    context_label TEXT NOT NULL DEFAULT '',
    raw_context TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL,
    UNIQUE(provider, source_url, target_url, context_label, raw_context)
);
CREATE INDEX IF NOT EXISTS idx_reference_source ON document_references(provider, source_url);
CREATE INDEX IF NOT EXISTS idx_reference_target ON document_references(provider, target_url);

CREATE TABLE IF NOT EXISTS objects (
    sha256 TEXT PRIMARY KEY,
    mime_type TEXT NOT NULL,
    logical_size INTEGER NOT NULL,
    stored_size INTEGER NOT NULL,
    compression TEXT,
    path TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_objects_mime ON objects(mime_type);

CREATE TABLE IF NOT EXISTS acquisition_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    provider TEXT NOT NULL,
    from_year INTEGER NOT NULL,
    to_year INTEGER NOT NULL,
    snapshot_cutoff TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovering',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON acquisition_runs(status, mode, provider);

CREATE TABLE IF NOT EXISTS run_segments (
    run_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    next_page INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    reported_total INTEGER,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, year),
    FOREIGN KEY(run_id) REFERENCES acquisition_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS manifest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    source_id TEXT NOT NULL,
    detail_url TEXT NOT NULL,
    title_hint TEXT,
    year INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    instrument_id TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES acquisition_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL,
    UNIQUE(run_id, provider, source_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_status ON manifest_items(run_id, status);
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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def record_object(self, obj: StoredObject, *, first_seen_at: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO objects
                (sha256, mime_type, logical_size, stored_size, compression, path, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    obj.sha256,
                    obj.mime_type,
                    obj.logical_size,
                    obj.stored_size,
                    obj.compression,
                    str(obj.path),
                    first_seen_at or utc_now_iso(),
                ),
            )

    def record_source(self, observation: SourceObservation, instrument_id: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_observations (
                    instrument_id, provider, source_url, source_id,
                    retrieved_at, content_sha256, mime_type, raw_path, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def upsert_source_record(
        self,
        document: DiscoveredDocument,
        *,
        instrument_id: str | None = None,
        checked_at: str | None = None,
        detail_sha256: str | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_records (
                    provider, source_id, detail_url, title_hint, instrument_id,
                    first_seen_at, last_seen_at, last_checked_at,
                    latest_detail_sha256, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, source_id) DO UPDATE SET
                    detail_url=excluded.detail_url,
                    title_hint=COALESCE(excluded.title_hint, source_records.title_hint),
                    instrument_id=COALESCE(excluded.instrument_id, source_records.instrument_id),
                    last_seen_at=excluded.last_seen_at,
                    last_checked_at=COALESCE(excluded.last_checked_at, source_records.last_checked_at),
                    latest_detail_sha256=COALESCE(excluded.latest_detail_sha256, source_records.latest_detail_sha256),
                    metadata_json=excluded.metadata_json
                """,
                (
                    document.provider,
                    document.source_id,
                    document.detail_url,
                    document.title_hint,
                    instrument_id,
                    now,
                    now,
                    checked_at,
                    detail_sha256,
                    json.dumps(document.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )

    def source_record(self, provider: str, source_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM source_records WHERE provider=? AND source_id=?",
                (provider, source_id),
            ).fetchone()

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
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                "SELECT 1 FROM source_observations WHERE provider=? AND source_url=? LIMIT 1",
                (provider, source_url),
            ).fetchone()
        return row is not None

    def hash_seen(self, content_sha256: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM objects WHERE sha256=? LIMIT 1", (content_sha256,)
            ).fetchone()
        return row is not None

    def create_run(
        self,
        *,
        mode: str,
        provider: str,
        from_year: int,
        to_year: int,
        snapshot_cutoff: str,
    ) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO acquisition_runs
                (mode, provider, from_year, to_year, snapshot_cutoff, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'discovering', ?)
                """,
                (mode, provider, from_year, to_year, snapshot_cutoff, now),
            )
            run_id = int(cur.lastrowid)
            conn.executemany(
                """
                INSERT INTO run_segments(run_id, year, next_page, status, updated_at)
                VALUES (?, ?, 1, 'pending', ?)
                """,
                [(run_id, year, now) for year in range(from_year, to_year + 1)],
            )
        return run_id

    def find_resumable_run(
        self, *, mode: str, provider: str, from_year: int, to_year: int
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM acquisition_runs
                WHERE mode=? AND provider=? AND from_year=? AND to_year=?
                  AND status IN ('discovering','ready','running','partial')
                ORDER BY id DESC LIMIT 1
                """,
                (mode, provider, from_year, to_year),
            ).fetchone()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM acquisition_runs WHERE id=?", (run_id,)).fetchone()

    def set_run_status(self, run_id: int, status: str) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            if status == "running":
                conn.execute(
                    "UPDATE acquisition_runs SET status=?, started_at=COALESCE(started_at, ?) WHERE id=?",
                    (status, now, run_id),
                )
            elif status in {"complete", "partial", "failed"}:
                conn.execute(
                    "UPDATE acquisition_runs SET status=?, finished_at=? WHERE id=?",
                    (status, now, run_id),
                )
            else:
                conn.execute("UPDATE acquisition_runs SET status=? WHERE id=?", (status, run_id))

    def segments(self, run_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM run_segments WHERE run_id=? ORDER BY year", (run_id,)))

    def update_segment(
        self,
        run_id: int,
        year: int,
        *,
        next_page: int | None = None,
        status: str | None = None,
        reported_total: int | None = None,
        discovered_count: int | None = None,
    ) -> None:
        updates: list[str] = ["updated_at=?"]
        values: list[object] = [utc_now_iso()]
        for name, value in (
            ("next_page", next_page),
            ("status", status),
            ("reported_total", reported_total),
            ("discovered_count", discovered_count),
        ):
            if value is not None:
                updates.append(f"{name}=?")
                values.append(value)
        values.extend([run_id, year])
        with self.connect() as conn:
            conn.execute(
                f"UPDATE run_segments SET {', '.join(updates)} WHERE run_id=? AND year=?",
                values,
            )

    def add_manifest_items(
        self, run_id: int, year: int, documents: Iterable[DiscoveredDocument]
    ) -> int:
        now = utc_now_iso()
        added = 0
        with self.connect() as conn:
            for doc in documents:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO manifest_items
                    (run_id, provider, source_id, detail_url, title_hint, year, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (run_id, doc.provider, doc.source_id, doc.detail_url, doc.title_hint, year, now),
                )
                added += int(cur.rowcount > 0)
                conn.execute(
                    """
                    INSERT INTO source_records
                    (provider, source_id, detail_url, title_hint, first_seen_at, last_seen_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, source_id) DO UPDATE SET
                        detail_url=excluded.detail_url,
                        title_hint=COALESCE(excluded.title_hint, source_records.title_hint),
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        doc.provider,
                        doc.source_id,
                        doc.detail_url,
                        doc.title_hint,
                        now,
                        now,
                        json.dumps(doc.metadata, ensure_ascii=False, sort_keys=True),
                    ),
                )
        return added

    def manifest_count(self, run_id: int) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM manifest_items WHERE run_id=?", (run_id,)).fetchone()[0])

    def manifest_items(
        self, run_id: int, *, include_failed: bool = False
    ) -> list[sqlite3.Row]:
        statuses = ("pending", "failed") if include_failed else ("pending",)
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"SELECT * FROM manifest_items WHERE run_id=? AND status IN ({placeholders}) ORDER BY year, id",
                    (run_id, *statuses),
                )
            )

    def mark_manifest_item(
        self,
        item_id: int,
        status: str,
        *,
        instrument_id: str | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE manifest_items SET status=?,
                    instrument_id=COALESCE(?, instrument_id),
                    last_error=?,
                    attempts=attempts + ?,
                    updated_at=?
                WHERE id=?
                """,
                (status, instrument_id, error, 1 if increment_attempt else 0, utc_now_iso(), item_id),
            )

    def run_counts(self, run_id: int) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM manifest_items WHERE run_id=? GROUP BY status",
                (run_id,),
            ).fetchall()
        result = {str(row["status"]): int(row["n"]) for row in rows}
        result["total"] = sum(result.values())
        return result

    def latest_run(self) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM acquisition_runs ORDER BY id DESC LIMIT 1").fetchone()

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            instruments = int(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0])
            observations = int(conn.execute("SELECT COUNT(*) FROM source_observations").fetchone()[0])
            references = int(conn.execute("SELECT COUNT(*) FROM document_references").fetchone()[0])
            source_records = int(conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0])
            unique_hashes = int(conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0])
            logical_bytes, stored_bytes = conn.execute(
                "SELECT COALESCE(SUM(logical_size),0), COALESCE(SUM(stored_size),0) FROM objects"
            ).fetchone()
        return {
            "instruments": instruments,
            "source_records": source_records,
            "source_observations": observations,
            "document_references": references,
            "unique_content_hashes": unique_hashes,
            "logical_bytes": int(logical_bytes),
            "stored_bytes": int(stored_bytes),
        }

    def pdf_stats(self) -> dict[str, float | int]:
        with self.connect() as conn:
            sizes = [
                int(row[0])
                for row in conn.execute(
                    "SELECT logical_size FROM objects WHERE mime_type='application/pdf' ORDER BY logical_size"
                )
            ]
        if not sizes:
            return {"count": 0, "mean_bytes": 0.0, "median_bytes": 0, "p95_bytes": 0, "max_bytes": 0}
        n = len(sizes)
        median = sizes[n // 2] if n % 2 else (sizes[n // 2 - 1] + sizes[n // 2]) // 2
        p95 = sizes[min(n - 1, max(0, int((n - 1) * 0.95)))]
        return {
            "count": n,
            "mean_bytes": sum(sizes) / n,
            "median_bytes": median,
            "p95_bytes": p95,
            "max_bytes": sizes[-1],
        }

    def reference_coverage(self) -> dict[str, int]:
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM document_references").fetchone()[0])
            resolved = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM document_references r
                    WHERE r.target_source_id IS NOT NULL
                      AND EXISTS (
                        SELECT 1 FROM source_records s
                        WHERE s.provider=r.provider AND s.source_id=r.target_source_id
                      )
                    """
                ).fetchone()[0]
            )
        return {"total": total, "known_targets": resolved, "missing_targets": max(0, total - resolved)}

    def iter_objects(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM objects ORDER BY sha256"))
