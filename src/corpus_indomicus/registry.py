from __future__ import annotations

from contextlib import contextmanager
import json
import hashlib
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
CREATE INDEX IF NOT EXISTS idx_instrument_issuer_identity
ON instruments(jurisdiction, issuing_body, document_type, year, number);

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

-- Append-only retrieval audit; source_observations remains the compact
-- de-duplicated compatibility table used by older callers.
CREATE TABLE IF NOT EXISTS source_fetch_events (
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
    UNIQUE(provider, source_url, content_sha256, retrieved_at),
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_fetch_event_url ON source_fetch_events(provider, source_url, retrieved_at);

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
    metadata_json TEXT NOT NULL DEFAULT '{}',
    scope_json TEXT NOT NULL DEFAULT '{}',
    scope_fingerprint TEXT,
    frozen_at TEXT
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
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES acquisition_runs(id) ON DELETE CASCADE,
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL,
    UNIQUE(run_id, provider, source_id)
);
CREATE INDEX IF NOT EXISTS idx_manifest_status ON manifest_items(run_id, status);

CREATE TABLE IF NOT EXISTS discovery_partitions (
    run_id INTEGER NOT NULL,
    "group" TEXT NOT NULL DEFAULT '',
    type_id TEXT NOT NULL DEFAULT '',
    year INTEGER NOT NULL DEFAULT 0,
    next_page INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',
    reported_total INTEGER,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    last_signature TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(run_id, "group", type_id, year),
    FOREIGN KEY(run_id) REFERENCES acquisition_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discovery_memberships (
    run_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    source_id TEXT NOT NULL,
    "group" TEXT NOT NULL DEFAULT '',
    type_id TEXT NOT NULL DEFAULT '',
    year INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id, provider, source_id, "group", type_id, year),
    FOREIGN KEY(run_id) REFERENCES acquisition_runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_membership_scope ON discovery_memberships(run_id, "group", type_id, year);

CREATE TABLE IF NOT EXISTS document_files (
    provider TEXT NOT NULL,
    source_id TEXT NOT NULL,
    detail_sha256 TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'expected',
    sha256 TEXT,
    error TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(provider, source_id, detail_sha256, url)
);
"""


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Older registries predate scoped acquisition.  Keep their rows and
            # indexes usable while adding the new nullable/defaulted fields.
            conn.execute("DROP INDEX IF EXISTS idx_instrument_identity")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_instrument_issuer_identity ON instruments(jurisdiction, issuing_body, document_type, year, number)")
            self._ensure_column(conn, "acquisition_runs", "scope_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "acquisition_runs", "scope_fingerprint", "TEXT")
            self._ensure_column(conn, "acquisition_runs", "frozen_at", "TEXT")
            self._ensure_column(conn, "manifest_items", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "manifest_items", "detail_sha256", "TEXT")
            conn.execute("PRAGMA user_version = 2")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
                """INSERT OR IGNORE INTO source_fetch_events
                   (instrument_id, provider, source_url, source_id, retrieved_at,
                    content_sha256, mime_type, raw_path, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    instrument_id, observation.provider, observation.source_url,
                    observation.source_id, observation.retrieved_at,
                    observation.content_sha256, observation.mime_type,
                    observation.raw_path,
                    json.dumps(observation.metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
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

    @staticmethod
    def _scope_fingerprint(scope: dict) -> str:
        # Catalog responses can carry volatile counters/timestamps.  They are
        # retained in scope_json for auditability but must not fork a run.
        def clean(value: object, key: str = "") -> object:
            if isinstance(value, dict):
                return {k: clean(v, k) for k, v in sorted(value.items()) if k not in {"observed_at", "reported_total", "discovered_count"}}
            if isinstance(value, list):
                items = [clean(v) for v in value]
                if key in {"groups", "types", "catalog"}:
                    return sorted(items, key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))
                return items
            return value
        payload = json.dumps(clean(scope), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create_scoped_run(
        self,
        *,
        mode: str,
        provider: str,
        scope: dict,
        snapshot_cutoff: str,
        resume: bool = True,
    ) -> tuple[int, bool]:
        """Create or resume an acquisition identified by its canonical scope."""
        scope_json = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = self._scope_fingerprint(scope)
        all_years = bool(scope.get("all_years"))
        from_year = int(scope.get("from_year", 0) or 0)
        to_year = int(scope.get("to_year", from_year) or from_year)
        if all_years:
            from_year = to_year = 0
        groups = scope.get("groups") or [""]
        top_types = scope.get("types") or []
        partitions: list[tuple[str, str, int]] = []
        years = [0] if all_years else list(range(from_year, to_year + 1))
        for group in groups:
            if isinstance(group, str):
                group_name = group
                types = [typ for typ in top_types if isinstance(typ, dict) and str(typ.get("group", "")) == group_name]
            else:
                group_name, types = str(group.get("group", "")), group.get("types") or [{}]
            if not types:
                raise ValueError(f"scope group {group_name!r} has no selected types")
            for typ in types:
                if isinstance(typ, str):
                    type_id = typ
                else:
                    type_id = str(typ.get("type_id", typ.get("id", "")))
                if not type_id:
                    raise ValueError(f"scope group {group_name!r} has a type without type_id")
                partitions.extend((group_name, type_id, year) for year in years)
        now = utc_now_iso()
        with self.connect() as conn:
            if resume:
                row = conn.execute(
                    """SELECT id FROM acquisition_runs
                       WHERE mode=? AND provider=? AND scope_fingerprint=?
                         AND status IN ('discovering','ready','running','partial')
                       ORDER BY id DESC LIMIT 1""",
                    (mode, provider, fingerprint),
                ).fetchone()
                if row is not None:
                    return int(row[0]), True
            cur = conn.execute(
                """INSERT INTO acquisition_runs
                   (mode, provider, from_year, to_year, snapshot_cutoff, status, created_at, scope_json, scope_fingerprint)
                   VALUES (?, ?, ?, ?, ?, 'discovering', ?, ?, ?)""",
                (mode, provider, from_year, to_year, snapshot_cutoff, now, scope_json, fingerprint),
            )
            run_id = int(cur.lastrowid)
            conn.executemany(
                """INSERT INTO discovery_partitions
                   (run_id, "group", type_id, year, updated_at) VALUES (?, ?, ?, ?, ?)""",
                [(run_id, group, type_id, year, now) for group, type_id, year in partitions],
            )
        return run_id, False

    def partitions(self, run_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM discovery_partitions WHERE run_id=? ORDER BY year, \"group\", type_id", (run_id,)))

    def update_partition(self, run_id: int, group: str, type_id: str, year: int, **values: object) -> None:
        allowed = {"next_page", "status", "reported_total", "discovered_count", "last_signature", "last_error"}
        updates, params = ["updated_at=?"], [utc_now_iso()]
        for key, value in values.items():
            if key in allowed and value is not None:
                updates.append(f"{key}=?"); params.append(value)
        params.extend([run_id, group, type_id, year])
        with self.connect() as conn:
            conn.execute(f"UPDATE discovery_partitions SET {', '.join(updates)} WHERE run_id=? AND \"group\"=? AND type_id=? AND year=?", params)

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
        self, run_id: int, year: int, documents: Iterable[DiscoveredDocument], *, group: str = "", type_id: str = ""
    ) -> int:
        now = utc_now_iso()
        added = 0
        with self.connect() as conn:
            row = conn.execute("SELECT frozen_at FROM acquisition_runs WHERE id=?", (run_id,)).fetchone()
            if row is not None and row[0] is not None:
                raise ValueError(f"run {run_id} is frozen")
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
                metadata_json = json.dumps(doc.metadata, ensure_ascii=False, sort_keys=True)
                conn.execute("UPDATE manifest_items SET metadata_json=CASE WHEN metadata_json='{}' THEN ? ELSE metadata_json END WHERE run_id=? AND provider=? AND source_id=?", (metadata_json, run_id, doc.provider, doc.source_id))
                conn.execute(
                    "INSERT OR IGNORE INTO discovery_memberships(run_id, provider, source_id, \"group\", type_id, year) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, doc.provider, doc.source_id, group or doc.metadata.get("group", ""), type_id or doc.metadata.get("type_id", ""), year),
                )
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

    def freeze_manifest(self, run_id: int) -> None:
        with self.connect() as conn:
            partition_total = int(conn.execute("SELECT COUNT(*) FROM discovery_partitions WHERE run_id=?", (run_id,)).fetchone()[0])
            segment_total = int(conn.execute("SELECT COUNT(*) FROM run_segments WHERE run_id=?", (run_id,)).fetchone()[0])
            incomplete = int(conn.execute("SELECT COUNT(*) FROM discovery_partitions WHERE run_id=? AND status!='complete'", (run_id,)).fetchone()[0])
            incomplete += int(conn.execute("SELECT COUNT(*) FROM run_segments WHERE run_id=? AND status!='complete'", (run_id,)).fetchone()[0])
            if not partition_total and not segment_total:
                raise ValueError(f"run {run_id} has no discovery partitions")
            if incomplete:
                raise ValueError(f"run {run_id} has incomplete discovery partitions")
            conn.execute("UPDATE acquisition_runs SET frozen_at=COALESCE(frozen_at, ?), status='ready' WHERE id=?", (utc_now_iso(), run_id))

    def iter_manifest_items(self, run_id: int, *, retry_failed: bool = False, batch_size: int = 100, limit: int | None = None) -> Iterator[sqlite3.Row]:
        last_id = 0
        yielded = 0
        while limit is None or yielded < limit:
            take = min(batch_size, limit - yielded) if limit is not None else batch_size
            statuses = ["pending", "running"] + (["failed", "partial", "unresolved", "no_file"] if retry_failed else [])
            placeholders = ",".join("?" for _ in statuses)
            with self.connect() as conn:
                rows = conn.execute(f"SELECT * FROM manifest_items WHERE run_id=? AND id>? AND status IN ({placeholders}) ORDER BY id LIMIT ?", (run_id, last_id, *statuses, take)).fetchall()
                if not rows:
                    return
            for row in rows:
                last_id = int(row["id"])
                yielded += 1
                yield row

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
        detail_sha256: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE manifest_items SET status=?,
                    instrument_id=COALESCE(?, instrument_id),
                    last_error=?,
                    detail_sha256=COALESCE(?, detail_sha256),
                    attempts=attempts + ?,
                    updated_at=?
                WHERE id=?
                """,
                (status, instrument_id, error, detail_sha256, 1 if increment_attempt else 0, utc_now_iso(), item_id),
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

    def iter_objects_batched(self, batch_size=100) -> Iterator[sqlite3.Row]:
        previous = ""
        while True:
            with self.connect() as conn:
                rows = conn.execute("SELECT * FROM objects WHERE sha256>? ORDER BY sha256 LIMIT ?",
                                    (previous, batch_size)).fetchall()
            if not rows:
                return
            for row in rows:
                previous = row["sha256"]
                yield row

    def set_expected_files(self, provider: str, source_id: str, detail_sha256: str, urls: Iterable[str]) -> int:
        with self.connect() as conn:
            for url in urls:
                conn.execute("INSERT OR IGNORE INTO document_files(provider, source_id, detail_sha256, url) VALUES (?, ?, ?, ?)", (provider, source_id, detail_sha256, url))
            return int(conn.execute("SELECT COUNT(*) FROM document_files WHERE provider=? AND source_id=? AND detail_sha256=?", (provider, source_id, detail_sha256)).fetchone()[0])

    def record_file(self, provider: str, source_id: str, detail_sha256: str, url: str, *, status: str = "downloaded", sha256: str | None = None, error: str | None = None, metadata: dict | None = None) -> None:
        with self.connect() as conn:
            conn.execute("""INSERT INTO document_files(provider, source_id, detail_sha256, url, status, sha256, error, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(provider, source_id, detail_sha256, url) DO UPDATE SET status=excluded.status, sha256=excluded.sha256, error=excluded.error, metadata_json=excluded.metadata_json""", (provider, source_id, detail_sha256, url, status, sha256, error, json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)))

    def files_complete(self, provider: str, source_id: str, detail_sha256: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total, SUM(status='downloaded') AS done FROM document_files WHERE provider=? AND source_id=? AND detail_sha256=?", (provider, source_id, detail_sha256)).fetchone()
        return int(row[0]) > 0 and int(row[1] or 0) == int(row[0])

    def expected_files(self, provider: str, source_id: str, detail_sha256: str) -> list[sqlite3.Row]:
        """Return the versioned file expectations for a detail snapshot."""
        with self.connect() as conn:
            return list(conn.execute("SELECT * FROM document_files WHERE provider=? AND source_id=? AND detail_sha256=? ORDER BY url", (provider, source_id, detail_sha256)))

    def coverage(self, run_id: int | None = None) -> dict:
        with self.connect() as conn:
            args = () if run_id is None else (run_id,)
            where = "" if run_id is None else "WHERE m.run_id=?"
            manifest = {r[0]: r[1] for r in conn.execute(f"SELECT status, COUNT(*) FROM manifest_items m {where} GROUP BY status", args)}
            sources = conn.execute(f"SELECT COUNT(DISTINCT provider || ':' || source_id) FROM manifest_items m {where}", args).fetchone()[0]
            instruments = conn.execute(f"SELECT COUNT(DISTINCT instrument_id) FROM manifest_items m {where}", args).fetchone()[0]
            memberships = conn.execute(f"SELECT COUNT(*) FROM discovery_memberships m {where}", args).fetchone()[0]
            distribution = [dict(r) for r in conn.execute(f"""SELECT m."group", m.type_id,
                COALESCE(i.year, NULLIF(m.year,0)) AS year, COUNT(*) AS n
                FROM discovery_memberships m LEFT JOIN source_records s ON s.provider=m.provider AND s.source_id=m.source_id
                LEFT JOIN instruments i ON i.id=s.instrument_id {where}
                GROUP BY m."group", m.type_id, COALESCE(i.year, NULLIF(m.year,0))""", args)]
            comparison = [dict(r) for r in conn.execute(f"""SELECT p."group", p.type_id, p.year,
                p.status, p.next_page, p.reported_total, p.last_error,
                (SELECT COUNT(*) FROM discovery_memberships m WHERE m.run_id=p.run_id AND m."group"=p."group" AND m.type_id=p.type_id AND m.year=p.year) AS discovered_count
                FROM discovery_partitions p {'WHERE p.run_id=?' if run_id is not None else ''}""", args)]
            partitions = {r[0]: r[1] for r in conn.execute(f"SELECT status,COUNT(*) FROM discovery_partitions {'WHERE run_id=?' if run_id is not None else ''} GROUP BY status", args)}
            # File metrics include retained revisions; a link is counted once per source/detail snapshot.
            file_filter = "" if run_id is None else "WHERE EXISTS (SELECT 1 FROM manifest_items m WHERE m.run_id=? AND m.provider=f.provider AND m.source_id=f.source_id AND m.detail_sha256=f.detail_sha256)"
            files = {r[0]: r[1] for r in conn.execute(f"SELECT status,COUNT(*) FROM document_files f {file_filter} GROUP BY status", args)}
            file_sql = f"SELECT f.sha256 FROM document_files f {file_filter} {'AND' if file_filter else 'WHERE'} f.status='downloaded' AND f.sha256 IS NOT NULL"
            physical = conn.execute(f"SELECT COUNT(*),COALESCE(SUM(logical_size),0),COALESCE(SUM(stored_size),0) FROM objects WHERE sha256 IN ({file_sql})", args).fetchone()
            logical = conn.execute(f"SELECT COUNT(*),COALESCE(SUM(o.logical_size),0) FROM document_files f JOIN objects o ON o.sha256=f.sha256 {file_filter} {'AND' if file_filter else 'WHERE'} f.status='downloaded'", args).fetchone()
            global_storage = conn.execute("SELECT COUNT(*),COALESCE(SUM(logical_size),0),COALESCE(SUM(stored_size),0) FROM objects").fetchone()
            global_sources = conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
            global_instruments = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
            run = conn.execute("SELECT scope_json,status,frozen_at FROM acquisition_runs WHERE id=?", (run_id,)).fetchone() if run_id is not None else None
        scope = json.loads(run[0]) if run else {}
        return {"run_id": run_id, "memberships": memberships, "sources": sources,
            "unique_documents": sources,
            "global_source_records": global_sources, "distinct_instruments": instruments,
            "global_instruments": global_instruments,
            "types_discovered": len(scope.get("catalog", scope.get("types", []))),
            "types_selected": len(scope.get("types", [])), "groups": distribution,
            "coverage_reference": comparison, "manifest": manifest, "partitions": partitions,
            "files": files, "documents_downloaded": manifest.get("done", 0),
            "no_file": manifest.get("no_file", 0), "partial": manifest.get("partial", 0),
            "failed": manifest.get("failed", 0), "unresolved": manifest.get("unresolved", 0),
            "out_of_scope": manifest.get("out_of_scope", 0),
            "objects": {"pdf_count": physical[0], "pdf_bytes": physical[1],
                "pdf_unique_sha256": physical[0], "duplicate_sha256": logical[0] - physical[0],
                "file_links": logical[0], "logical_bytes": logical[1], "physical_bytes": physical[2]},
            "global_storage": {"objects": global_storage[0], "logical_unique_bytes": global_storage[1],
                               "physical_bytes": global_storage[2]},
            "status": run[1] if run else "aggregate", "frozen_at": run[2] if run else None}
