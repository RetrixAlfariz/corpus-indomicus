from __future__ import annotations

from dataclasses import dataclass
import gzip
from pathlib import Path
from typing import Literal

from .integrity import detect_mime, extension_for_mime, sha256_bytes


@dataclass(slots=True, frozen=True)
class StoredObject:
    sha256: str
    mime_type: str
    path: Path
    logical_size: int
    stored_size: int
    compression: str | None
    was_new: bool


class RawArchive:
    """Global content-addressed object store.

    Hashes are always calculated from the exact bytes received from the source.
    Compressible web payloads are stored losslessly with gzip; PDFs remain exact
    standalone bytes so they can be independently verified or evicted later.
    """

    COMPRESSIBLE = {"text/html", "application/json", "text/plain"}

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path_for(self, digest: str, mime: str, compression: str | None) -> Path:
        ext = extension_for_mime(mime)
        suffix = f"{ext}.gz" if compression == "gzip" else ext
        return self.root / digest[:2] / f"{digest}{suffix}"

    def store(
        self,
        *,
        provider: str,
        source_url: str,
        data: bytes,
        retrieved_at: str,
        content_type: str | None = None,
        extra_metadata: dict | None = None,
    ) -> StoredObject:
        del provider, source_url, retrieved_at, extra_metadata  # provenance lives in SQLite
        digest = sha256_bytes(data)
        mime = detect_mime(data, content_type)
        compression: Literal["gzip"] | None = "gzip" if mime in self.COMPRESSIBLE else None
        path = self._path_for(digest, mime, compression)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            return StoredObject(
                sha256=digest,
                mime_type=mime,
                path=path,
                logical_size=len(data),
                stored_size=path.stat().st_size,
                compression=compression,
                was_new=False,
            )

        payload = gzip.compress(data, compresslevel=9, mtime=0) if compression == "gzip" else data
        path.write_bytes(payload)

        return StoredObject(
            sha256=digest,
            mime_type=mime,
            path=path,
            logical_size=len(data),
            stored_size=len(payload),
            compression=compression,
            was_new=True,
        )

    @staticmethod
    def read_object(path: str | Path, *, compression: str | None = None) -> bytes:
        payload = Path(path).read_bytes()
        if compression == "gzip":
            return gzip.decompress(payload)
        return payload
