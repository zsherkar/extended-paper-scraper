"""Approved arXiv source integration with hard policy guardrails.

This module intentionally exposes only a reviewed set of arXiv-backed sources.
Unknown ``arxiv`` targets are blocked before any network request is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
import json
import logging
import os
from pathlib import Path
from time import monotonic, sleep, time
from typing import Callable, Iterable
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ppr.models import Paper

logger = logging.getLogger(__name__)

ARXIV_EXPORT_HOST = "export.arxiv.org"
ARXIV_QUERY_PATH = "/api/query"
ARXIV_API_URL = f"https://{ARXIV_EXPORT_HOST}{ARXIV_QUERY_PATH}"
ARXIV_ABS_URL_TEMPLATE = "https://arxiv.org/abs/{arxiv_id}"
ARXIV_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"

ARXIV_API_TOS_URL = "https://info.arxiv.org/help/api/tou.html"
ARXIV_API_USER_MANUAL_URL = "https://info.arxiv.org/help/api/user-manual.html"
ARXIV_BULK_DATA_URL = "https://info.arxiv.org/help/bulk_data.html"

ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_MAX_RESULTS_PER_SLICE = 2000
ARXIV_MAX_RESULTS_TOTAL = 30000
ARXIV_RECOMMENDED_QUERY_RESULT_LIMIT = 1000
ARXIV_DEFAULT_MAX_ITEMS = 75
ARXIV_REQUEST_TIMEOUT = (10, 60)
ARXIV_CACHE_DIR = Path(os.environ.get("PPR_ARXIV_CACHE_DIR", "data/.arxiv_cache"))
ARXIV_CACHE_TTL_SECONDS = int(os.environ.get("PPR_ARXIV_CACHE_TTL_SECONDS", "21600"))

ARXIV_ALLOWED_SORT_BY = ("relevance", "lastUpdatedDate", "submittedDate")
ARXIV_ALLOWED_SORT_ORDER = ("ascending", "descending")
ARXIV_ALLOWED_CONTENT_MODES = ("metadata",)
ARXIV_RESERVED_TARGET_PREFIXES = ("arxiv",)
ARXIV_ACCESS_DISABLED_REASON = (
    "Unreviewed arXiv targets are blocked in this repo to protect shared API "
    "access. The command was stopped before any arXiv request was sent."
)


@dataclass(frozen=True)
class ArxivSource:
    source_id: str
    name: str
    search_query: str
    source_type: str
    source_category: str
    home_url: str
    max_items: int = ARXIV_DEFAULT_MAX_ITEMS


ARXIV_SOURCES = {
    "arxiv_cs_ai_recent": ArxivSource(
        source_id="arxiv_cs_ai_recent",
        name="arXiv cs.AI Recent",
        search_query="cat:cs.AI",
        source_type="preprint_archive",
        source_category="arxiv_recent_ai",
        home_url="https://arxiv.org/list/cs.AI/recent",
    ),
    "arxiv_cs_lg_recent": ArxivSource(
        source_id="arxiv_cs_lg_recent",
        name="arXiv cs.LG Recent",
        search_query="cat:cs.LG",
        source_type="preprint_archive",
        source_category="arxiv_recent_ml",
        home_url="https://arxiv.org/list/cs.LG/recent",
    ),
    "arxiv_cs_cl_recent": ArxivSource(
        source_id="arxiv_cs_cl_recent",
        name="arXiv cs.CL Recent",
        search_query="cat:cs.CL",
        source_type="preprint_archive",
        source_category="arxiv_recent_nlp",
        home_url="https://arxiv.org/list/cs.CL/recent",
    ),
    "arxiv_cs_cv_recent": ArxivSource(
        source_id="arxiv_cs_cv_recent",
        name="arXiv cs.CV Recent",
        search_query="cat:cs.CV",
        source_type="preprint_archive",
        source_category="arxiv_recent_cv",
        home_url="https://arxiv.org/list/cs.CV/recent",
    ),
    "arxiv_cs_ro_recent": ArxivSource(
        source_id="arxiv_cs_ro_recent",
        name="arXiv cs.RO Recent",
        search_query="cat:cs.RO",
        source_type="preprint_archive",
        source_category="arxiv_recent_robotics",
        home_url="https://arxiv.org/list/cs.RO/recent",
    ),
    "arxiv_cs_ne_recent": ArxivSource(
        source_id="arxiv_cs_ne_recent",
        name="arXiv cs.NE Recent",
        search_query="cat:cs.NE",
        source_type="preprint_archive",
        source_category="arxiv_recent_neural",
        home_url="https://arxiv.org/list/cs.NE/recent",
    ),
    "arxiv_stat_ml_recent": ArxivSource(
        source_id="arxiv_stat_ml_recent",
        name="arXiv stat.ML Recent",
        search_query="cat:stat.ML",
        source_type="preprint_archive",
        source_category="arxiv_recent_statistics",
        home_url="https://arxiv.org/list/stat.ML/recent",
    ),
}

ARXIV_SOURCE_GROUPS = {
    "arxiv_recent": tuple(ARXIV_SOURCES),
    "arxiv_ai": tuple(ARXIV_SOURCES),
    "preprint_sources": tuple(ARXIV_SOURCES),
}
DEFAULT_ARXIV_SOURCE_IDS = ARXIV_SOURCE_GROUPS["arxiv_recent"]
ARXIV_APPROVED_SOURCE_IDS = frozenset(ARXIV_SOURCES)


class ArxivPolicyError(ValueError):
    """Raised when a planned arXiv request violates repo-enforced policy."""


def is_arxiv_target(target: str) -> bool:
    normalized = (target or "").strip().lower().replace("-", "_")
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}_")
        for prefix in ARXIV_RESERVED_TARGET_PREFIXES
    )


def ensure_arxiv_access_allowed(targets: Iterable[str]) -> None:
    requested = [target for target in targets if is_arxiv_target(target)]
    if not requested:
        return

    blocked = [target for target in requested if target not in ARXIV_APPROVED_SOURCE_IDS]
    if not blocked:
        return

    blocked_targets = ", ".join(blocked)
    raise ArxivPolicyError(
        f"{ARXIV_ACCESS_DISABLED_REASON} Blocked target(s): {blocked_targets}. "
        "Use the reviewed built-in arXiv source IDs only."
    )


def build_arxiv_abs_link(arxiv_id: str) -> str:
    return ARXIV_ABS_URL_TEMPLATE.format(arxiv_id=arxiv_id.strip())


def build_arxiv_pdf_link(arxiv_id: str) -> str:
    return ARXIV_PDF_URL_TEMPLATE.format(arxiv_id=arxiv_id.strip())


def build_arxiv_headers(
    *,
    tool_name: str = "extended-paper-retriever",
    contact_email: str = "",
) -> dict[str, str]:
    user_agent = f"{tool_name} (metadata-only arXiv client)"
    if contact_email:
        user_agent = f"{tool_name} ({contact_email}; metadata-only arXiv client)"
    return {
        "User-Agent": user_agent,
        "Accept": "application/atom+xml",
    }


def _normalize_id_list(id_list: str | Iterable[str] | None) -> str:
    if id_list is None:
        return ""
    if isinstance(id_list, str):
        return id_list.strip()

    ids = [value.strip() for value in id_list if value and value.strip()]
    return ",".join(ids)


@dataclass(frozen=True)
class ArxivApiPolicy:
    api_url: str = ARXIV_API_URL
    allowed_hosts: tuple[str, ...] = (ARXIV_EXPORT_HOST,)
    min_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS
    max_results_per_slice: int = ARXIV_MAX_RESULTS_PER_SLICE
    max_results_total: int = ARXIV_MAX_RESULTS_TOTAL
    recommended_query_result_limit: int = ARXIV_RECOMMENDED_QUERY_RESULT_LIMIT
    single_connection_only: bool = True
    allowed_content_modes: tuple[str, ...] = ARXIV_ALLOWED_CONTENT_MODES
    prefer_abs_links: bool = True
    allow_serving_eprints: bool = False
    allow_pdf_mirroring: bool = False
    allow_source_mirroring: bool = False

    def validate_api_url(self, url: str | None = None) -> str:
        candidate = url or self.api_url
        parsed = urlparse(candidate)
        if parsed.hostname not in self.allowed_hosts:
            raise ArxivPolicyError(
                f"arXiv API traffic must go through {ARXIV_EXPORT_HOST}, got {candidate!r}"
            )
        if parsed.path.rstrip("/") != ARXIV_QUERY_PATH:
            raise ArxivPolicyError(
                f"arXiv API path must be {ARXIV_QUERY_PATH}, got {parsed.path!r}"
            )
        return candidate

    def validate_request(
        self,
        *,
        start: int = 0,
        max_results: int = 100,
        sort_by: str | None = None,
        sort_order: str | None = None,
        connections: int = 1,
        content_mode: str = "metadata",
    ) -> None:
        if start < 0:
            raise ArxivPolicyError("arXiv start must be >= 0")
        if max_results <= 0:
            raise ArxivPolicyError("arXiv max_results must be > 0")
        if max_results > self.max_results_per_slice:
            raise ArxivPolicyError(
                f"arXiv max_results must be <= {self.max_results_per_slice} per request"
            )
        if start + max_results > self.max_results_total:
            raise ArxivPolicyError(
                "arXiv request window must stay within the first "
                f"{self.max_results_total} results"
            )
        if self.single_connection_only and connections != 1:
            raise ArxivPolicyError("arXiv requests must use a single connection")
        if content_mode not in self.allowed_content_modes:
            allowed = ", ".join(self.allowed_content_modes)
            raise ArxivPolicyError(
                f"arXiv content mode must be one of [{allowed}], got {content_mode!r}"
            )
        if sort_by is not None and sort_by not in ARXIV_ALLOWED_SORT_BY:
            allowed = ", ".join(ARXIV_ALLOWED_SORT_BY)
            raise ArxivPolicyError(f"sortBy must be one of [{allowed}]")
        if sort_order is not None and sort_order not in ARXIV_ALLOWED_SORT_ORDER:
            allowed = ", ".join(ARXIV_ALLOWED_SORT_ORDER)
            raise ArxivPolicyError(f"sortOrder must be one of [{allowed}]")

    def build_query_params(
        self,
        *,
        search_query: str = "",
        id_list: str | Iterable[str] | None = None,
        start: int = 0,
        max_results: int = 100,
        sort_by: str | None = None,
        sort_order: str | None = None,
        connections: int = 1,
        content_mode: str = "metadata",
    ) -> dict[str, str | int]:
        self.validate_request(
            start=start,
            max_results=max_results,
            sort_by=sort_by,
            sort_order=sort_order,
            connections=connections,
            content_mode=content_mode,
        )

        normalized_query = search_query.strip()
        normalized_ids = _normalize_id_list(id_list)
        if not normalized_query and not normalized_ids:
            raise ArxivPolicyError("arXiv requests require search_query or id_list")

        params: dict[str, str | int] = {
            "start": start,
            "max_results": max_results,
        }
        if normalized_query:
            params["search_query"] = normalized_query
        if normalized_ids:
            params["id_list"] = normalized_ids
        if sort_by:
            params["sortBy"] = sort_by
        if sort_order:
            params["sortOrder"] = sort_order
        return params

    def build_query_url(
        self,
        *,
        search_query: str = "",
        id_list: str | Iterable[str] | None = None,
        start: int = 0,
        max_results: int = 100,
        sort_by: str | None = None,
        sort_order: str | None = None,
        connections: int = 1,
        content_mode: str = "metadata",
    ) -> str:
        base_url = self.validate_api_url()
        params = self.build_query_params(
            search_query=search_query,
            id_list=id_list,
            start=start,
            max_results=max_results,
            sort_by=sort_by,
            sort_order=sort_order,
            connections=connections,
            content_mode=content_mode,
        )
        return f"{base_url}?{urlencode(params)}"


@dataclass
class ArxivRateLimiter:
    min_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS
    monotonic_fn: Callable[[], float] = field(default=monotonic, repr=False)
    sleep_fn: Callable[[float], None] = field(default=sleep, repr=False)
    _last_request_at: float | None = field(default=None, init=False, repr=False)

    def wait_for_slot(self) -> float:
        now = self.monotonic_fn()
        if self._last_request_at is None:
            self._last_request_at = now
            return 0.0

        elapsed = now - self._last_request_at
        delay = 0.0
        if elapsed < self.min_interval_seconds:
            delay = self.min_interval_seconds - elapsed
            self.sleep_fn(delay)
            now += delay

        self._last_request_at = now
        return delay


_ARXIV_RATE_LIMITER = ArxivRateLimiter()


def _collapse_whitespace(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_children_with_name(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if _strip_ns(child.tag) == name]


def _first_child_text(node: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in list(node):
        if _strip_ns(child.tag) in wanted and child.text:
            return _collapse_whitespace(child.text)
    return ""


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    return _collapse_whitespace(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def _extract_arxiv_id(entry_id: str) -> str:
    normalized = (entry_id or "").strip().rstrip("/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1]


def _cache_enabled() -> bool:
    return os.environ.get("PPR_ARXIV_CACHE", "1") != "0"


def _cache_path(source_id: str) -> Path:
    return ARXIV_CACHE_DIR / f"{source_id}.json"


def _read_cached_papers(source_id: str, *, allow_stale: bool = False) -> list[Paper] | None:
    if not _cache_enabled():
        return None

    path = _cache_path(source_id)
    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable arXiv cache file %s: %s", path, exc)
        return None

    fetched_at = float(payload.get("fetched_at", 0.0) or 0.0)
    is_fresh = (time() - fetched_at) <= ARXIV_CACHE_TTL_SECONDS
    if not is_fresh and not allow_stale:
        return None

    papers = [Paper.from_dict(item) for item in payload.get("papers", []) if item]
    if papers:
        logger.info(
            "Using %s arXiv cache for %s (%d items)",
            "fresh" if is_fresh else "stale",
            source_id,
            len(papers),
        )
    return papers or None


def _write_cached_papers(source_id: str, papers: list[Paper]) -> None:
    if not _cache_enabled():
        return

    path = _cache_path(source_id)
    payload = {
        "source_id": source_id,
        "fetched_at": time(),
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "papers": [paper.to_dict() for paper in papers],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        try:
            tmp_path.replace(path)
        except OSError:
            # Windows file replacement can be flaky in some sandboxed setups.
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        logger.warning("Could not write arXiv cache file %s: %s", path, exc)


def _fetch_query_xml(source: ArxivSource) -> str:
    policy = ArxivApiPolicy()
    params = policy.build_query_params(
        search_query=source.search_query,
        start=0,
        max_results=source.max_items,
        sort_by="submittedDate",
        sort_order="descending",
        connections=1,
        content_mode="metadata",
    )
    headers = build_arxiv_headers(contact_email=os.environ.get("PPR_ARXIV_CONTACT_EMAIL", ""))

    _ARXIV_RATE_LIMITER.wait_for_slot()
    response = requests.get(
        policy.validate_api_url(),
        params=params,
        headers=headers,
        timeout=ARXIV_REQUEST_TIMEOUT,
    )
    if response.status_code == 429:
        raise ArxivPolicyError(
            "arXiv returned 429 Rate exceeded. Refusing to retry automatically to "
            "protect shared access."
        )
    response.raise_for_status()
    return response.text


def _parse_arxiv_feed(xml_text: str, source: ArxivSource) -> list[Paper]:
    root = ET.fromstring(xml_text)
    papers: list[Paper] = []

    for entry in _iter_children_with_name(root, "entry")[: source.max_items]:
        title = _first_child_text(entry, "title")
        summary = _html_to_text(_first_child_text(entry, "summary"))
        published = _first_child_text(entry, "published")
        updated = _first_child_text(entry, "updated")

        entry_id = _first_child_text(entry, "id")
        arxiv_id = _extract_arxiv_id(entry_id)
        if not title or not arxiv_id:
            continue

        authors = [
            _first_child_text(author_node, "name")
            for author_node in _iter_children_with_name(entry, "author")
            if _first_child_text(author_node, "name")
        ]
        categories = []
        for category_node in _iter_children_with_name(entry, "category"):
            term = _collapse_whitespace(category_node.attrib.get("term", ""))
            if term:
                categories.append(term)
        categories = list(dict.fromkeys(categories))

        papers.append(
            Paper(
                title=title,
                link=build_arxiv_abs_link(arxiv_id),
                authors=authors,
                selection=source.source_category,
                keywords=categories,
                abstract=summary,
                publication_date=published or updated,
                fields_of_study=categories,
                open_access_pdf=build_arxiv_pdf_link(arxiv_id),
                external_ids={"ArXiv": arxiv_id},
                source_id=source.source_id,
                source_name=source.name,
                source_type=source.source_type,
                source_category=source.source_category,
                source_url=source.home_url,
            )
        )

    deduped: list[Paper] = []
    seen: set[str] = set()
    for paper in papers:
        arxiv_id = paper.external_ids.get("ArXiv", "")
        key = arxiv_id or paper.link or paper.title.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(paper)
    return deduped


def _scrape_arxiv_source(source_id: str) -> list[Paper]:
    source = ARXIV_SOURCES[source_id]
    logger.info("Scraping approved arXiv source %s via official API", source_id)

    cached = _read_cached_papers(source_id)
    if cached is not None:
        return cached

    try:
        xml_text = _fetch_query_xml(source)
        papers = _parse_arxiv_feed(xml_text, source)
    except (ArxivPolicyError, requests.RequestException, ET.ParseError) as exc:
        stale = _read_cached_papers(source_id, allow_stale=True)
        if stale is not None:
            logger.warning("Falling back to stale arXiv cache for %s after error: %s", source_id, exc)
            return stale
        raise RuntimeError(f"Failed to fetch approved arXiv source {source_id}: {exc}") from exc

    _write_cached_papers(source_id, papers)
    logger.info("Found %d items for %s", len(papers), source_id)
    return papers


SCRAPERS = {
    source_id: partial(_scrape_arxiv_source, source_id)
    for source_id in ARXIV_SOURCES
}
