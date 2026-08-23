from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .integrity import detect_mime, extension_for_mime, sha256_bytes


@dataclass(slots=True, frozen=True)
class StoredObject:
    sha256: str
    mime_type: str
    path: Path
    size: int


def _provider_token(provider: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9._-]+", "_", provider.strip()).strip("_")
    return token.lower() or "unknown"


class RawArchive:
    """Immutable content-addressed storage for retrieved source bytes."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def store(
        self,
        *,
        provider: str,
        source_url: str,
        data: bytes,
        retrieved_at: str,
        content_type: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> StoredObject:
        digest = sha256_bytes(data)
        mime = detect_mime(data, content_type)
        ext = extension_for_mime(mime)

        try:
            stamp = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
        except ValueError:
            stamp = datetime.now(timezone.utc)

        folder = (
            self.root
            / _provider_token(provider)
            / f"{stamp.year:04d}"
            / f"{stamp.month:02d}"
            / digest[:2]
        )
        folder.mkdir(parents=True, exist_ok=True)

        path = folder / f"{digest}{ext}"
        if not path.exists():
            path.write_bytes(data)

        sidecar = folder / f"{digest}.metadata.json"
        if not sidecar.exists():
            payload = {
                "sha256": digest,
                "size": len(data),
                "mime_type": mime,
                "provider": provider,
                "first_seen_at": retrieved_at,
                "first_source_url": source_url,
                "extra": extra_metadata or {},
            }
            sidecar.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        return StoredObject(
            sha256=digest,
            mime_type=mime,
            path=path,
            size=len(data),
        )
