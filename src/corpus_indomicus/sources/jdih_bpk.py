from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from ..models import DocumentReference, LegalInstrument
from .base import DiscoveredDocument, HttpSourceClient, SourceConnector


PROVIDER = "jdih_bpk"
BASE_URL = "https://peraturan.bpk.go.id"


@dataclass(slots=True)
class BpkDetail:
    instrument: LegalInstrument | None
    metadata: dict[str, str]
    file_urls: list[str]
    references: list[DocumentReference]


@dataclass(slots=True)
class BpkSearchPage:
    documents: list[DiscoveredDocument]
    reported_total: int | None = None
    next_page: int | None = None
    has_more: bool | None = None
    is_last_page: bool | None = None
    end_reason: str | None = None
    parser_ok: bool = True
    diagnostics: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class BpkCatalogType:
    """A type discovered from a BPK category page."""

    group: str
    type_id: str
    name: str
    url: str
    reported_total: int | None = None
    observed_at: datetime | None = None
    classification: str = "unresolved"


def _clean(value: str) -> str:
    return " ".join(value.split()).strip()


def _details_id(url: str) -> str:
    match = re.search(r"/Details/(\d+)", url, flags=re.IGNORECASE)
    return match.group(1) if match else url


def _with_params(url: str, params: dict[str, Any]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in params.items() if v is not None and v != ""})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _parse_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\b(18|19|20)\d{2}\b", value)
    return int(match.group(0)) if match else None


def _reported_total(soup: BeautifulSoup) -> int | None:
    text = _clean(soup.get_text(" ", strip=True))
    patterns = (
        r"Menemukan\s+([\d.,]+)\s+peraturan",
        r"([\d.,]+)\s+peraturan\s+ditemukan",
        r"([\d.,]+)\s+peraturan\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            return int(digits) if digits else None
    return None


def _classification(group: str | None) -> str:
    value = _clean(group or "").lower()
    if value in {"pusat", "central", "peraturan perundang-undangan pusat", "1"}:
        return "pusat"
    if value in {"lembaga", "kementerian/lembaga", "kementerian", "2"}:
        return "lembaga"
    if "daerah" in value or value in {"regional", "provinsi", "kabupaten"}:
        return "daerah"
    return "unresolved"


def parse_catalog_types(
    html: str,
    group: str,
    base_url: str = BASE_URL,
    *,
    observed_at: datetime | None = None,
) -> list[BpkCatalogType]:
    """Extract type links from a ``/Jenis/<category>`` page.

    The category number in the page URL is deliberately not used as a type
    id; only the ``jenis`` query parameter is authoritative.
    """
    soup = BeautifulSoup(html, "html.parser")
    classification = _classification(group)
    labels = {"peraturan perundang-undangan pusat": "pusat",
              "peraturan kementerian / lembaga": "lembaga", "peraturan daerah": "daerah"}
    heading = next((node for node in soup.select('.display-6, h2, h3')
                    if _clean(node.get_text(" ", strip=True)).lower() in labels), None)
    verified = labels.get(_clean(heading.get_text(" ", strip=True)).lower()) if heading else None
    if verified != classification:
        classification = "unresolved"
    catalog_root = heading.parent if heading else soup
    if heading:
        for _ in range(3):
            if catalog_root.select_one('a[href*="jenis="]'):
                break
            if not isinstance(catalog_root.parent, Tag):
                break
            catalog_root = catalog_root.parent
    observed_at = observed_at or datetime.now(timezone.utc)
    result: list[BpkCatalogType] = []
    seen: set[str] = set()
    for anchor in catalog_root.select('a[href]'):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.netloc.lower() != urlsplit(base_url).netloc.lower() or parts.path.lower() != "/search":
            continue
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        type_id = params.get("jenis", "").strip()
        if not type_id or not type_id.isdigit() or type_id in seen:
            continue
        name = _clean(anchor.get_text(" ", strip=True))
        if re.fullmatch(r"[\d.,]+", name):
            continue
        # The live page has a name link followed by a count link.  Restrict
        # count extraction to the local item so footer/navigation numbers do
        # not become catalog totals.
        total: int | None = None
        item = anchor.find_parent(["li", "div", "tr"])
        ancestor = item
        for _ in range(5):
            if not ancestor:
                break
            nearby = ancestor.find_all("a", href=True)
            for candidate in nearby:
                text = _clean(candidate.get_text(" ", strip=True))
                candidate_href = candidate.get("href")
                if candidate is not anchor and re.fullmatch(r"[\d.,]+", text) and isinstance(candidate_href, str):
                    candidate_params = dict(parse_qsl(urlsplit(urljoin(base_url, candidate_href)).query))
                    if candidate_params.get("jenis") == type_id:
                        total = int(re.sub(r"\D", "", text))
                        break
            if total is not None:
                break
            ancestor = ancestor.parent if isinstance(ancestor.parent, Tag) else None
        if not name and item:
            name = _clean(item.get_text(" ", strip=True))
        if not name and anchor.get("title"):
            name = _clean(str(anchor["title"]))
        if not name:
            continue
        # Regional types must never silently enter the central pilot.
        item_classification = classification
        if re.match(r"^(?:peraturan|keputusan|instruksi)\s+(?:daerah|bupati|wali\s*kota|walikota)\b", name, re.IGNORECASE):
            item_classification = "daerah"
        seen.add(type_id)
        result.append(BpkCatalogType(
            group=group,
            type_id=type_id,
            name=name,
            url=absolute,
            reported_total=total,
            observed_at=observed_at,
            classification=item_classification,
        ))
    return result


def parse_search_page(
    html: str,
    base_url: str = BASE_URL,
    *,
    expected_year: int | None = None,
    page: int | None = None,
) -> BpkSearchPage:
    """Parse a BPK search page conservatively.

    Only recognized primary identity anchors are accepted. Slug years are hints;
    detail-page metadata verifies the actual year before acquisition completes.
    """
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, DiscoveredDocument] = {}

    # Results are cards in the current source. A card's first non-reference
    # Details link is its identity; links under status/reference sections are
    # intentionally ignored.
    containers: list[Tag] = []
    for selector in ("article", ".search-result", ".search-result-item", "div.card"):
        for node in soup.select(selector):
            if isinstance(node, Tag) and node not in containers and node.select_one('a[href*="/Details/"]'):
                containers.append(node)
    anchors: list[Tag] = []
    if containers:
        for container in containers:
            candidates = container.select('a[href*="/Details/"]')
            primary = next((a for a in candidates if any(c in (a.get("class") or []) for c in ("text-gray-600", "result-title", "card-title"))), None)
            if primary is not None:
                anchors.append(primary)
    else:
        all_details = soup.select('a[href*="/Details/"]')
        if all_details:
            return BpkSearchPage([], _reported_total(soup), parser_ok=False,
                                 diagnostics=("result containers not recognized",))

    for anchor in anchors:
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        detail_url = urljoin(base_url, href)
        detail_parts = urlsplit(detail_url)
        if detail_parts.netloc and detail_parts.netloc.lower() != urlsplit(base_url).netloc.lower():
            continue
        if not re.search(r"/Details/\d+(?:/|$)", detail_parts.path, re.IGNORECASE):
            continue
        title_hint = _clean(anchor.get_text(" ", strip=True)) or None

        candidate_year = _parse_year(detail_parts.path)

        source_id = _details_id(detail_url)
        if source_id in found:
            continue

        if not title_hint:
            parent = anchor.find_parent(["article", "div", "li"])
            if parent:
                title_hint = _clean(parent.get_text(" ", strip=True))[:500] or None

        found[source_id] = DiscoveredDocument(
            provider=PROVIDER,
            source_id=source_id,
            detail_url=detail_url,
            title_hint=title_hint,
            metadata={"url_year_hint": candidate_year,
                      **({"requested_year": expected_year, "year_scope_unverified": True} if expected_year else {})},
        )

    reported = _reported_total(soup)
    numeric_pages: set[int] = set()
    active_page: int | None = page
    active_link = soup.select_one('.pagination .active a[href], .pagination a[aria-current="page"]')
    observed_page = None
    if active_link:
        active_params = dict(parse_qsl(urlsplit(str(active_link.get("href", ""))).query))
        if active_params.get("p", "").isdigit():
            observed_page = int(active_params["p"])
    if observed_page is not None:
        if page is not None and page != observed_page:
            return BpkSearchPage([], reported, parser_ok=False, diagnostics=("source returned a different page",))
        active_page = observed_page
    for link in soup.select('a[href]'):
        href = link.get("href")
        if not isinstance(href, str):
            continue
        match = re.search(r"(?:^|[?&])p=(\d+)", href)
        if not match:
            continue
        number = int(match.group(1))
        disabled = "disabled" in (link.get("class") or []) or link.get("aria-disabled") == "true" or (
            isinstance(link.parent, Tag) and "disabled" in (link.parent.get("class") or []))
        if not disabled:
            numeric_pages.add(number)
        if active_page is None and ("active" in (link.parent.get("class") or []) if isinstance(link.parent, Tag) else False):
            active_page = number
        if active_page is None and _clean(link.get_text(" ", strip=True)).lower() in {"berikutnya", "next", "selanjutnya", ">", "›"}:
            active_page = max(1, number - 1)
    next_page = min((p for p in numeric_pages if active_page is not None and p > active_page), default=None)
    pager_present = bool(numeric_pages) or bool(soup.select(".pagination"))
    at_last_pager_page = observed_page is not None and bool(numeric_pages) and observed_page >= max(numeric_pages)
    has_more = True if next_page is not None else (False if (reported is not None and reported <= len(found)) or at_last_pager_page else (None if not pager_present else None))
    is_last = False if has_more is True else (True if has_more is False else None)
    reason = "next_page" if has_more else ("reported_total_reached" if is_last else None)
    return BpkSearchPage(list(found.values()), reported, next_page=next_page,
                         has_more=has_more, is_last_page=is_last, end_reason=reason,
                         parser_ok=True)


def parse_search_html(
    html: str,
    base_url: str = BASE_URL,
    *,
    expected_year: int | None = None,
) -> list[DiscoveredDocument]:
    return parse_search_page(html, base_url, expected_year=expected_year).documents


def _metadata_from_tables(soup: BeautifulSoup) -> dict[str, str]:
    metadata: dict[str, str] = {}
    # Current BPK metadata uses Bootstrap label/value rows, not tables.
    # Bind values to their row so navigation/footer labels cannot overwrite them.
    for row in soup.select('div.row'):
        cells = row.find_all('div', recursive=False)
        if len(cells) == 2:
            key = _clean(cells[0].get_text(' ', strip=True))
            value = _clean(cells[1].get_text(' ', strip=True))
            if key in KNOWN_LABELS and value:
                metadata.setdefault(key, value)
                if key == "Bentuk":
                    for anchor in cells[1].select("a[href]"):
                        params = dict(parse_qsl(urlsplit(str(anchor["href"])).query))
                        if params.get("jenis", "").isdigit():
                            metadata["Jenis ID"] = params["jenis"]
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) >= 2:
            key = _clean(cells[0].get_text(" ", strip=True))
            value = _clean(cells[1].get_text(" ", strip=True))
            if key and value:
                metadata.setdefault(key, value)
    for term in soup.find_all("dt"):
        desc = term.find_next_sibling("dd")
        if desc:
            key = _clean(term.get_text(" ", strip=True))
            value = _clean(desc.get_text(" ", strip=True))
            if key and value:
                metadata.setdefault(key, value)
    return metadata


KNOWN_LABELS = (
    "Tipe Dokumen",
    "Judul",
    "T.E.U.",
    "Nomor",
    "Bentuk",
    "Bentuk Singkat",
    "Tahun",
    "Tempat Penetapan",
    "Tanggal Penetapan",
    "Tanggal Pengundangan",
    "Tanggal Berlaku",
    "Sumber",
    "Subjek",
    "Status",
    "Bahasa",
    "Lokasi",
    "Bidang",
)


def _metadata_from_text(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    strings = [_clean(s) for s in soup.stripped_strings]
    positions = {
        label: i
        for i, text in enumerate(strings)
        for label in KNOWN_LABELS
        if text == label
    }
    for label, index in positions.items():
        for candidate in strings[index + 1 : index + 5]:
            if candidate in KNOWN_LABELS:
                break
            if candidate:
                result.setdefault(label, candidate)
                break
    return result


def _jurisdiction_from_location(location: str | None) -> str:
    if not location:
        return "ID"
    cleaned = _clean(location)
    if cleaned.lower() in {"pemerintah pusat", "indonesia", "republik indonesia"}:
        return "ID"
    token = re.sub(r"[^A-Z0-9]+", "_", cleaned.upper()).strip("_")
    return f"ID/{token}" if token else "ID"


def _parse_date(value: str | None) -> str | None:
    if not value:
        return None
    value = _clean(value)
    months = {
        "januari": "January",
        "februari": "February",
        "maret": "March",
        "april": "April",
        "mei": "May",
        "juni": "June",
        "juli": "July",
        "agustus": "August",
        "september": "September",
        "oktober": "October",
        "november": "November",
        "desember": "December",
    }
    translated = value.lower()
    for indo, english in months.items():
        translated = re.sub(rf"\b{indo}\b", english, translated, flags=re.IGNORECASE)
    try:
        return datetime.strptime(translated.title(), "%d %B %Y").date().isoformat()
    except ValueError:
        return value


def _find_files(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        text = _clean(anchor.get_text(" ", strip=True)).lower()
        href_lower = href.lower()
        # /Read is a viewer route, not an expected physical attachment.
        if "/read/" in href_lower:
            continue
        looks_like_file = (
            href_lower.endswith(".pdf")
            or ".pdf?" in href_lower
            or "/download" in href_lower
            or "download" in text
        )
        if not looks_like_file:
            continue
        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _reference_context(anchor: Tag) -> tuple[str | None, str | None]:
    parent = anchor.find_parent(["tr", "li", "p", "article", "section", "div"])
    if not isinstance(parent, Tag):
        return None, None
    raw_context = _clean(parent.get_text(" ", strip=True))[:1000] or None
    target_text = _clean(anchor.get_text(" ", strip=True))
    context_label: str | None = None
    for tag_name in ("th", "dt", "strong", "b"):
        candidate = parent.find(tag_name)
        if isinstance(candidate, Tag):
            text = _clean(candidate.get_text(" ", strip=True))
            if text and text != target_text and len(text) <= 160:
                context_label = text
                break
    if context_label is None:
        heading = parent.find_previous(["h2", "h3", "h4", "h5"])
        if isinstance(heading, Tag):
            text = _clean(heading.get_text(" ", strip=True))
            if text and len(text) <= 160:
                context_label = text
    return context_label, raw_context


def _find_references(soup: BeautifulSoup, detail_url: str) -> list[DocumentReference]:
    source_id = _details_id(detail_url)
    references: list[DocumentReference] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for anchor in soup.select('a[href*="/Details/"]'):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        target_url = urljoin(detail_url, href)
        target_source_id = _details_id(target_url)
        if target_source_id == source_id:
            continue
        target_label = _clean(anchor.get_text(" ", strip=True)) or None
        context_label, raw_context = _reference_context(anchor)
        key = (target_url, context_label, raw_context)
        if key in seen:
            continue
        seen.add(key)
        references.append(
            DocumentReference(
                provider=PROVIDER,
                source_id=source_id,
                source_url=detail_url,
                target_source_id=target_source_id,
                target_url=target_url,
                target_label=target_label,
                context_label=context_label,
                raw_context=raw_context,
            )
        )
    return references


def parse_detail_html(html: str, detail_url: str) -> BpkDetail:
    soup = BeautifulSoup(html, "html.parser")
    metadata = _metadata_from_tables(soup)
    for key, value in _metadata_from_text(soup).items():
        metadata.setdefault(key, value)

    document_type = metadata.get("Bentuk Singkat") or metadata.get("Bentuk")
    number = metadata.get("Nomor")
    year = _parse_year(metadata.get("Tahun"))
    title = metadata.get("Judul")
    if not title:
        heading = soup.find("h1")
        title = _clean(heading.get_text(" ", strip=True)) if heading else None

    instrument: LegalInstrument | None = None
    if document_type and number and year and title:
        instrument = LegalInstrument.from_minimal(
            document_type=document_type,
            number=number,
            year=year,
            title=title,
            jurisdiction=_jurisdiction_from_location(metadata.get("Lokasi")),
            issuing_body=metadata.get("T.E.U."),
            status=metadata.get("Status"),
            dates={
                "enacted": _parse_date(metadata.get("Tanggal Penetapan")),
                "promulgated": _parse_date(metadata.get("Tanggal Pengundangan")),
                "effective": _parse_date(metadata.get("Tanggal Berlaku")),
            },
            publication={"source": metadata.get("Sumber")},
            metadata={
                "source_metadata": metadata,
                "bpk_detail_id": _details_id(detail_url),
            },
        )

    return BpkDetail(
        instrument=instrument,
        metadata=metadata,
        file_urls=_find_files(soup, detail_url),
        references=_find_references(soup, detail_url),
    )


class JdihBpkConnector(SourceConnector):
    provider = PROVIDER

    def __init__(self, client: HttpSourceClient, diagnostics_dir: str | Path | None = None):
        self.client = client
        self.diagnostics_dir = Path(diagnostics_dir) if diagnostics_dir else None

    def catalog_types(self, groups: Iterable[str] = ("pusat", "lembaga")) -> list[BpkCatalogType]:
        """Fetch and parse the authoritative type catalog for each group."""
        aliases = {
            "pusat": 1, "central": 1, "1": 1,
            "lembaga": 2, "kementerian/lembaga": 2, "2": 2,
        }
        if isinstance(groups, str):
            groups = (groups,)
        result: list[BpkCatalogType] = []
        seen: set[tuple[str, str]] = set()
        for group in groups:
            key = _clean(str(group)).lower()
            category_id = aliases.get(key)
            if category_id is None:
                # Unknown groups are retained as unresolved metadata only when
                # the caller supplied a concrete category path elsewhere.
                continue
            response = self.client.get(f"{BASE_URL}/Jenis/{category_id}")
            for item in parse_catalog_types(response.text, key, BASE_URL):
                identity = (item.group, item.type_id)
                if identity not in seen:
                    seen.add(identity)
                    result.append(item)
        return result

    def search_page(
        self,
        *,
        query: str | None = None,
        year: int | None = None,
        page: int = 1,
        type_id: str | int | None = None,
    ) -> BpkSearchPage:
        url = _with_params(
            f"{BASE_URL}/Search",
            {"tentang": query, "tahun": year, "jenis": type_id, "p": page},
        )
        response = self.client.get(url)
        result = parse_search_page(response.text, BASE_URL, expected_year=year, page=page)
        soup = BeautifulSoup(response.text, "html.parser")
        selected_types = {str(o.get("value")) for o in soup.select('select[name="jenis"] option[selected]')}
        selected_years = {str(o.get("value")) for o in soup.select('select[name="tahun"] option[selected]')}
        if type_id is not None and selected_types and str(type_id) not in selected_types:
            result.parser_ok = False
            result.diagnostics += ("source returned a different type filter",)
        if year is not None and selected_years and str(year) not in selected_years:
            result.parser_ok = False
            result.diagnostics += ("source returned a different year filter",)
        if (not result.parser_ok or result.has_more is None) and self.diagnostics_dir is not None:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            (self.diagnostics_dir / f"bpk-search-{stamp}.html").write_text(response.text, encoding="utf-8")
        if not result.parser_ok:
            raise RuntimeError("BPK search parser failed: " + "; ".join(result.diagnostics))
        for document in result.documents:
            if type_id is not None:
                document.metadata["type_id"] = str(type_id)
        return result

    def discover(
        self,
        *,
        query: str | None = None,
        year: int | None = None,
        page: int = 1,
        type_id: str | int | None = None,
    ) -> Iterable[DiscoveredDocument]:
        return self.search_page(query=query, year=year, page=page, type_id=type_id).documents

    def fetch_detail(self, document: DiscoveredDocument):
        response = self.client.get(document.detail_url)
        return response, parse_detail_html(response.text, document.detail_url)

    def fetch_file(self, url: str):
        return self.client.get(url)
