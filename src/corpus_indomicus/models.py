from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import re
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _identity_token(value: str) -> str:
    value = value.strip().upper()
    value = re.sub(r"[^A-Z0-9]+", "_", value)
    return value.strip("_") or "UNKNOWN"


def make_instrument_id(
    document_type: str,
    year: int | str,
    number: str,
    jurisdiction: str = "ID",
) -> str:
    return ":".join(
        (
            _identity_token(jurisdiction),
            _identity_token(document_type),
            _identity_token(str(year)),
            _identity_token(number),
        )
    )


@dataclass(slots=True)
class SourceObservation:
    provider: str
    source_url: str
    retrieved_at: str = field(default_factory=utc_now_iso)
    source_id: str | None = None
    content_sha256: str | None = None
    mime_type: str | None = None
    raw_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LegalInstrument:
    id: str
    document_type: str
    number: str
    year: int
    title: str
    jurisdiction: str = "ID"
    issuing_body: str | None = None
    status: str | None = None
    dates: dict[str, str | None] = field(default_factory=dict)
    publication: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, list[str]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_minimal(
        cls,
        *,
        document_type: str,
        number: str,
        year: int,
        title: str,
        jurisdiction: str = "ID",
        **kwargs: Any,
    ) -> "LegalInstrument":
        return cls(
            id=make_instrument_id(document_type, year, number, jurisdiction),
            document_type=document_type,
            number=number,
            year=year,
            title=title,
            jurisdiction=jurisdiction,
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
