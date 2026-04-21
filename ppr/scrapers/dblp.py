"""DBLP scraper with exact TOC support and proceedings-first venue backfill.

Historical strategy:
- Discover per-year proceedings keys from the DBLP conference index page
- Fetch papers by exact TOC key for discovered years
- Fall back to publication search only for years without discovered proceedings keys
"""

import html
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import partial
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ppr.models import Paper

logger = logging.getLogger(__name__)

DBLP_PUBL_API_URL = "https://dblp.org/search/publ/api"
DBLP_CONF_INDEX_URL_TEMPLATE = "https://dblp.org/db/conf/{index_slug}/index.html"
DBLP_CACHE_DIR = Path(os.environ.get("PPR_DBLP_CACHE_DIR", "data/.dblp_cache"))

# Exact TOC fetches can stay large
HITS_PER_PAGE = 1000

# Historical search must stay small
SEARCH_PAGE_SIZE = 50

# Be conservative with DBLP
REQUEST_SLEEP_SECONDS = 2.5
BACKOFF_BASE_SECONDS = 5.0
MAX_RETRIES = 5

_USER_AGENT = {"User-Agent": "extended-paper-retriever/0.1"}

_REC_LINK_RE = re.compile(r'href="(?:https?://dblp\.org)?/rec/([^"#?]+)"', re.IGNORECASE)
_YEAR_SUFFIX_RE = re.compile(r"^(?P<year>(?:19|20)\d{2})$")
_ALIAS_YEAR_SUFFIX_RE_TEMPLATE = r"^(?:{alias})[-_]?(?P<year>(?:19|20)\d{{2}})$"
_DBLP_LAST_REQUEST_AT = 0.0


@dataclass(frozen=True)
class DblpProceedings:
    """Exact DBLP fetch target for one venue/year proceedings volume."""

    year: int
    toc_key: str
    rec_key: str = ""
    number: str | None = None
    source: str = "index"


def _clean_author(name: str) -> str:
    """Strip DBLP disambiguation suffix from author name."""
    return re.sub(r"\s+\d{4}$", "", (name or "")).strip()


def _normalize_title(title: str) -> str:
    """Normalize DBLP title text for display/storage."""
    title = html.unescape(title or "").strip()
    if title.endswith("."):
        title = title[:-1]
    return title.strip()


def _norm_key(text: str) -> str:
    """Normalize text for dedupe keys."""
    text = html.unescape(text or "").lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _safe_list(value) -> list:
    """Convert scalar/dict/list-ish values into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_authors(hit: dict) -> list[str]:
    """Extract authors from a DBLP hit safely."""
    authors_raw = hit.get("authors", {}).get("author", [])
    authors_raw = _safe_list(authors_raw)

    cleaned = []
    for a in authors_raw:
        if isinstance(a, dict):
            text = a.get("text", "")
        else:
            text = str(a)
        text = _clean_author(text)
        if text:
            cleaned.append(text)
    return cleaned


def _extract_link(hit: dict) -> str:
    """Extract the external electronic edition link."""
    ee = hit.get("ee")
    if isinstance(ee, list):
        return ee[0] if ee else ""
    return ee or ""


def _parse_year(value) -> int | None:
    """Parse year safely from DBLP hit."""
    try:
        year = int(str(value).strip())
        if 1800 <= year <= datetime.now().year + 1:
            return year
    except Exception:
        pass
    return None


def _cache_enabled() -> bool:
    """Return whether the on-disk DBLP cache should be used."""
    if os.environ.get("PPR_DBLP_CACHE", "1") == "0":
        return False
    return "PYTEST_CURRENT_TEST" not in os.environ


def _cache_path(kind: str, key: str) -> Path:
    digest = sha256(key.encode("utf-8")).hexdigest()
    return DBLP_CACHE_DIR / kind / f"{digest}.json"


def _read_json_cache(kind: str, key: str):
    if not _cache_enabled():
        return None

    path = _cache_path(kind, key)
    if not path.exists():
        return None

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Ignoring unreadable DBLP cache file %s: %s", path, e)
        return None


def _write_json_cache(kind: str, key: str, value) -> None:
    if not _cache_enabled():
        return

    path = _cache_path(kind, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
        tmp_path.replace(path)
    except OSError as e:
        logger.warning("Could not write DBLP cache file %s: %s", path, e)


def _dblp_throttle() -> None:
    """Keep DBLP requests spaced out across discovery and TOC fetches."""
    global _DBLP_LAST_REQUEST_AT

    if "PYTEST_CURRENT_TEST" in os.environ:
        return

    now = time.monotonic()
    if _DBLP_LAST_REQUEST_AT:
        elapsed = now - _DBLP_LAST_REQUEST_AT
        if elapsed < REQUEST_SLEEP_SECONDS:
            time.sleep(REQUEST_SLEEP_SECONDS - elapsed)

    _DBLP_LAST_REQUEST_AT = time.monotonic()


def _rate_limit_sleep_seconds(resp, attempt: int) -> float:
    retry_after = resp.headers.get("Retry-After") if hasattr(resp, "headers") else None
    if retry_after:
        try:
            return max(float(retry_after), BACKOFF_BASE_SECONDS)
        except ValueError:
            pass
    return BACKOFF_BASE_SECONDS * (attempt + 1)


def _dblp_api_get(url: str, params: dict, retries: int = MAX_RETRIES) -> dict:
    """Make a DBLP API request with retries and strong backoff."""
    last_err = None

    for attempt in range(retries):
        try:
            logger.info("DBLP API GET %s params=%s", url, params)
            _dblp_throttle()
            resp = requests.get(
                url,
                params=params,
                timeout=(10, 60),
                headers=_USER_AGENT,
            )

            if resp.status_code == 429:
                wait = _rate_limit_sleep_seconds(resp, attempt)
                logger.warning(
                    "DBLP rate limited (429). Waiting %.1f seconds before retry %d/%d",
                    wait,
                    attempt + 1,
                    retries,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()

        except requests.RequestException as e:
            last_err = e
            wait = BACKOFF_BASE_SECONDS * (attempt + 1)
            logger.warning(
                "DBLP request failed on attempt %d/%d: %s. Sleeping %.1f seconds",
                attempt + 1,
                retries,
                e,
                wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"DBLP API request failed after {retries} attempts: {last_err}")


def _dblp_page_get(url: str, retries: int = MAX_RETRIES) -> str:
    """Fetch a DBLP HTML page with retries and backoff."""
    last_err = None

    for attempt in range(retries):
        try:
            logger.info("DBLP page GET %s", url)
            _dblp_throttle()
            resp = requests.get(
                url,
                timeout=(10, 60),
                headers=_USER_AGENT,
            )

            if resp.status_code == 429:
                wait = _rate_limit_sleep_seconds(resp, attempt)
                logger.warning(
                    "DBLP rate limited while fetching page (429). Waiting %.1f seconds before retry %d/%d",
                    wait,
                    attempt + 1,
                    retries,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.text

        except requests.RequestException as e:
            last_err = e
            wait = BACKOFF_BASE_SECONDS * (attempt + 1)
            logger.warning(
                "DBLP page request failed on attempt %d/%d: %s. Sleeping %.1f seconds",
                attempt + 1,
                retries,
                e,
                wait,
            )
            time.sleep(wait)

    raise RuntimeError(f"DBLP page request failed after {retries} attempts: {last_err}")


def _toc_key_from_toc_href(href: str) -> str:
    """Convert a DBLP TOC page URL/path into a BHT key."""
    parsed = urlparse(html.unescape(href or ""))
    path = parsed.path if parsed.scheme else href
    path = html.unescape(path).strip().lstrip("/")

    for suffix in (".html", ".xml", ".json", ".bib"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break

    if path.endswith(".bht"):
        return path
    return f"{path}.bht"


def _fallback_toc_key_from_rec_key(rec_key: str, venue: str | None = None) -> str | None:
    """Best-effort conversion from a DBLP record key to the matching TOC BHT key."""
    parts = rec_key.strip("/").split("/")
    if len(parts) != 3:
        return None

    kind, rec_venue, suffix = parts
    if kind != "conf":
        return None

    venue = (venue or rec_venue).lower().strip()
    aliases = DBLP_VENUE_REC_ALIASES.get(venue, [rec_venue])
    year = _extract_main_year_from_rec_suffix(suffix, aliases=aliases)
    if year is None:
        return None

    toc_prefix = DBLP_VENUE_TOC_PREFIXES.get(venue, rec_venue)
    toc_slug = f"{toc_prefix}{year}" if suffix == str(year) else suffix
    return f"db/conf/{rec_venue}/{toc_slug}.bht"


def _normalize_dblp_toc_key(key: str) -> str:
    """Normalize DBLP TOC URL, BHT key, or main record key to a BHT key."""
    key = html.unescape((key or "").strip())
    if not key:
        return key

    if key.startswith("http://") or key.startswith("https://"):
        return _toc_key_from_toc_href(key)

    if key.startswith("db/"):
        return key if key.endswith(".bht") else _toc_key_from_toc_href(key)

    if key.startswith("conf/"):
        converted = _fallback_toc_key_from_rec_key(key)
        return converted or key

    return key


def _fetch_dblp_toc(key: str, number: str | None = None) -> list[dict]:
    """Fetch all paper records from DBLP for a given TOC key."""
    key = _normalize_dblp_toc_key(key)
    cache_key = json.dumps({"key": key, "number": number}, sort_keys=True)
    cached = _read_json_cache("toc", cache_key)
    if cached is not None:
        return cached

    all_hits: list[dict] = []
    offset = 0
    total = 0

    while True:
        params = {
            "q": f"toc:{key}:",
            "h": HITS_PER_PAGE,
            "f": offset,
            "format": "json",
        }
        data = _dblp_api_get(DBLP_PUBL_API_URL, params)

        hits_data = data["result"]["hits"]
        total = int(hits_data.get("@total", 0))

        if total == 0:
            break

        for hit in hits_data.get("hit", []):
            all_hits.append(hit["info"])

        offset += HITS_PER_PAGE
        if offset >= total:
            break

    if number:
        all_hits = [h for h in all_hits if h.get("number") == number]

    _write_json_cache("toc", cache_key, all_hits)
    logger.info("Fetched %d papers via TOC key %s (DBLP total=%s)", len(all_hits), key, total)
    return all_hits


def _fetch_dblp(key: str, number: str | None = None) -> list[dict]:
    """Backward-compatible alias for exact TOC-key fetch."""
    return _fetch_dblp_toc(key, number=number)


# In-memory per-run caches
_DBLP_SEARCH_CACHE: dict[tuple[str, int | None, int | None, int], list[dict]] = {}
_DBLP_INDEX_CACHE: dict[str, dict[int, DblpProceedings]] = {}


def _fetch_dblp_search(
    query: str,
    start_year: int | None = None,
    end_year: int | None = None,
    max_results: int = 200,
) -> list[dict]:
    """Fetch papers from DBLP publication search API.

    Notes:
    - This uses DBLP publication search, not exact TOC keys.
    - It is intentionally conservative to avoid rate limiting.
    - Year filtering is done locally.
    """
    cache_key = (query, start_year, end_year, max_results)
    if cache_key in _DBLP_SEARCH_CACHE:
        return _DBLP_SEARCH_CACHE[cache_key]

    all_hits: list[dict] = []
    offset = 0

    while True:
        batch_size = min(SEARCH_PAGE_SIZE, max_results - offset)
        if batch_size <= 0:
            break

        params = {
            "q": query,
            "h": batch_size,
            "f": offset,
            "format": "json",
        }
        data = _dblp_api_get(DBLP_PUBL_API_URL, params)
        hits_data = data["result"]["hits"]
        total = int(hits_data.get("@total", 0))

        if total == 0:
            break

        raw_batch = [hit["info"] for hit in hits_data.get("hit", [])]
        filtered_batch = []

        for h in raw_batch:
            year = _parse_year(h.get("year"))
            if start_year is not None and (year is None or year < start_year):
                continue
            if end_year is not None and (year is None or year > end_year):
                continue
            filtered_batch.append(h)

        all_hits.extend(filtered_batch)
        offset += batch_size

        if offset >= total or offset >= max_results:
            break

    _DBLP_SEARCH_CACHE[cache_key] = all_hits

    logger.info(
        "Fetched %d papers via DBLP search for query=%r year_range=%s-%s",
        len(all_hits),
        query,
        start_year,
        end_year,
    )
    return all_hits


def _info_to_paper(hit: dict, selection: str = "main") -> Paper | None:
    """Convert DBLP info dict to Paper."""
    if hit.get("type") == "Editorship":
        return None

    title = _normalize_title(hit.get("title", ""))
    if not title:
        return None

    link = _extract_link(hit)
    if not link:
        return None

    authors = _extract_authors(hit)

    return Paper(
        title=title,
        link=link,
        authors=authors,
        selection=selection,
    )


# ----------------------------
# Existing exact TOC-key map
# ----------------------------

DBLP_CONFERENCES = {
    # ICSE
    "icse_2023": {"key": "db/conf/icse/icse2023.bht"},
    "icse_2024": {"key": "db/conf/icse/icse2024.bht"},
    "icse_2025": {"key": "db/conf/icse/icse2025.bht"},

    # FSE
    "fse_2023": {"key": "db/conf/sigsoft/fse2023.bht"},
    "fse_2024": {"key": "db/journals/pacmse/pacmse1.bht"},
    "fse_2025": {"key": "db/journals/pacmse/pacmse2.bht", "number": "FSE"},

    # ASE
    "ase_2023": {"key": "db/conf/kbse/ase2023.bht"},
    "ase_2024": {"key": "db/conf/kbse/ase2024.bht"},
    "ase_2025": {"key": "db/conf/kbse/ase2025.bht"},

    # ISSTA
    "issta_2023": {"key": "db/conf/issta/issta2023.bht"},
    "issta_2024": {"key": "db/conf/issta/issta2024.bht"},
    "issta_2025": {"key": "db/journals/pacmse/pacmse2.bht", "number": "ISSTA"},

    # Robotics
    "icra_2023": {"key": "db/conf/icra/icra2023.bht"},
    "icra_2024": {"key": "db/conf/icra/icra2024.bht"},
    "icra_2025": {"key": "db/conf/icra/icra2025.bht"},
    "iros_2023": {"key": "db/conf/iros/iros2023.bht"},
    "iros_2024": {"key": "db/conf/iros/iros2024.bht"},
    "iros_2025": {"key": "db/conf/iros/iros2025.bht"},
    "rss_2023": {"key": "db/conf/rss/rss2023.bht"},
    "rss_2024": {"key": "db/conf/rss/rss2024.bht"},

    # AI
    "ijcai_2023": {"key": "db/conf/ijcai/ijcai2023.bht"},
    "ijcai_2024": {"key": "db/conf/ijcai/ijcai2024.bht"},
    "ijcai_2025": {"key": "db/conf/ijcai/ijcai2025.bht"},
}


def _scrape_dblp(conf_id: str) -> list[Paper]:
    """Existing compatibility path: scrape by exact DBLP TOC key."""
    conf = DBLP_CONFERENCES[conf_id]
    hits = _fetch_dblp(conf["key"], number=conf.get("number"))

    papers: list[Paper] = []
    seen_titles: set[str] = set()

    for hit in hits:
        paper = _info_to_paper(hit, selection="main")
        if not paper:
            continue

        norm_title = _norm_key(paper.title)
        if norm_title in seen_titles:
            continue

        seen_titles.add(norm_title)
        papers.append(paper)

    logger.info("Scraped %d papers for %s via exact DBLP TOC", len(papers), conf_id)
    return papers


# -------------------------------------------
# Proceedings-first venue historical backfill
# -------------------------------------------

DBLP_VENUE_INDEX_PATHS = {
    "icse": "icse",
    "fse": "sigsoft",
    "ase": "kbse",
    "issta": "issta",
    "ijcai": "ijcai",
    "icra": "icra",
    "iros": "iros",
    "rss": "rss",
}

DBLP_VENUE_REC_ALIASES = {
    "icse": ["icse"],
    "fse": ["sigsoft", "fse"],
    "ase": ["kbse", "ase"],
    "issta": ["issta"],
    "ijcai": ["ijcai"],
    "icra": ["icra"],
    "iros": ["iros"],
    "rss": ["rss"],
}

DBLP_VENUE_TOC_PREFIXES = {
    "icse": "icse",
    "fse": "fse",
    "ase": "ase",
    "issta": "issta",
    "ijcai": "ijcai",
    "icra": "icra",
    "iros": "iros",
    "rss": "rss",
}

# Legacy publication-search fallback queries (used only for missing years).
DBLP_VENUE_SEARCHES = {
    "icse": ["venue:ICSE"],
    "fse": ["venue:FSE", "venue:ESEC/FSE"],
    "ase": ["venue:ASE"],
    "issta": ["venue:ISSTA"],
    "ijcai": ["venue:IJCAI"],
    "icra": ["venue:ICRA"],
    "iros": ["venue:IROS"],
    "rss": ["venue:RSS"],
}


def _extract_rec_keys_from_index_html(index_html: str) -> set[str]:
    """Extract DBLP record keys from venue index HTML."""
    return {
        _clean_rec_key(match.group(1))
        for match in _REC_LINK_RE.finditer(index_html)
        if _clean_rec_key(match.group(1))
    }


def _clean_rec_key(rec_key: str) -> str:
    rec_key = html.unescape((rec_key or "").strip()).strip("/")
    if rec_key.endswith(".html"):
        rec_key = rec_key[:-5]
    return rec_key


def _extract_main_year_from_rec_suffix(suffix: str, aliases: list[str]) -> int | None:
    """Return a year only for likely main proceedings rec-key suffixes."""
    suffix = suffix.lower().strip()

    direct = _YEAR_SUFFIX_RE.match(suffix)
    if direct:
        return int(direct.group("year"))

    for alias in aliases:
        alias = re.escape(alias.lower().strip())
        pattern = re.compile(_ALIAS_YEAR_SUFFIX_RE_TEMPLATE.format(alias=alias))
        match = pattern.match(suffix)
        if match:
            return int(match.group("year"))

    return None


def _index_url_for_venue(venue: str) -> str:
    """Build DBLP conference index URL for venue."""
    venue = venue.lower().strip()
    index_slug = DBLP_VENUE_INDEX_PATHS.get(venue, venue)
    return DBLP_CONF_INDEX_URL_TEMPLATE.format(index_slug=index_slug)


def _known_proceedings_for_venue(venue: str) -> dict[int, DblpProceedings]:
    """Return hand-verified modern DBLP entries for a venue."""
    proceedings: dict[int, DblpProceedings] = {}

    for conf_id, conf in DBLP_CONFERENCES.items():
        prefix, _, year_text = conf_id.rpartition("_")
        if prefix != venue:
            continue

        year = _parse_year(year_text)
        if year is None:
            continue

        proceedings[year] = DblpProceedings(
            year=year,
            toc_key=conf["key"],
            number=conf.get("number"),
            source="known",
        )

    return proceedings


def _proceedings_from_entry(entry, venue: str, aliases: list[str]) -> DblpProceedings | None:
    rec_key = _clean_rec_key(entry.get("id", ""))
    if not rec_key:
        return None

    parts = rec_key.split("/")
    if len(parts) != 3:
        return None

    kind, rec_venue, suffix = parts
    if kind != "conf" or rec_venue.lower() not in {a.lower() for a in aliases}:
        return None

    year = _extract_main_year_from_rec_suffix(suffix, aliases=aliases)
    if year is None:
        return None

    toc_key = None
    for link in entry.find_all("a", href=True):
        if "table of contents" in link.get_text(" ", strip=True).lower():
            toc_key = _toc_key_from_toc_href(link["href"])
            break

    if not toc_key:
        toc_key = _fallback_toc_key_from_rec_key(rec_key, venue=venue)
    if not toc_key:
        return None

    return DblpProceedings(year=year, toc_key=toc_key, rec_key=rec_key)


def _extract_proceedings_from_index_html(index_html: str, venue: str) -> dict[int, DblpProceedings]:
    """Extract exact main-proceedings TOC keys from a DBLP venue index page."""
    aliases = DBLP_VENUE_REC_ALIASES.get(venue, [venue])
    proceedings: dict[int, DblpProceedings] = {}
    soup = BeautifulSoup(index_html, "html.parser")

    for entry in soup.select("li.entry"):
        discovered = _proceedings_from_entry(entry, venue=venue, aliases=aliases)
        if not discovered:
            continue

        existing = proceedings.get(discovered.year)
        if existing is None or discovered.rec_key.endswith(f"/{discovered.year}"):
            proceedings[discovered.year] = discovered

    if proceedings:
        return dict(sorted(proceedings.items()))

    # Fallback for simple mocked HTML or unexpected DBLP markup changes.
    for rec_key in _extract_rec_keys_from_index_html(index_html):
        toc_key = _fallback_toc_key_from_rec_key(rec_key, venue=venue)
        if not toc_key:
            continue
        year = _extract_main_year_from_rec_suffix(
            rec_key.rsplit("/", 1)[-1],
            aliases=aliases,
        )
        if year is None:
            continue
        proceedings[year] = DblpProceedings(year=year, toc_key=toc_key, rec_key=rec_key)

    return dict(sorted(proceedings.items()))


def _serialize_proceedings_map(proceedings: dict[int, DblpProceedings]) -> dict:
    return {
        "version": 2,
        "proceedings": {str(year): asdict(p) for year, p in proceedings.items()},
    }


def _deserialize_proceedings_map(raw: dict | None) -> dict[int, DblpProceedings] | None:
    if not isinstance(raw, dict) or raw.get("version") != 2:
        return None

    proceedings_raw = raw.get("proceedings")
    if not isinstance(proceedings_raw, dict):
        return None

    try:
        return {
            int(year): DblpProceedings(**payload)
            for year, payload in proceedings_raw.items()
            if isinstance(payload, dict)
        }
    except (TypeError, ValueError):
        return None


def discover_dblp_proceedings(venue: str) -> dict[int, DblpProceedings]:
    """Discover year -> exact DBLP proceedings fetch target for a venue."""
    venue = venue.lower().strip()

    if venue in _DBLP_INDEX_CACHE:
        return _DBLP_INDEX_CACHE[venue]

    cache_key = f"proceedings:{venue}"
    cached = _deserialize_proceedings_map(_read_json_cache("index", cache_key))
    if cached is not None:
        _DBLP_INDEX_CACHE[venue] = cached
        return cached

    index_html = _dblp_page_get(_index_url_for_venue(venue))
    proceedings_by_year = _extract_proceedings_from_index_html(index_html, venue=venue)

    # Known modern keys cover DBLP records whose proceedings live in a journal
    # volume, such as FSE/ISSTA in PACMSE, and avoid companion-only records.
    proceedings_by_year.update(_known_proceedings_for_venue(venue))
    proceedings_by_year = dict(sorted(proceedings_by_year.items()))
    _DBLP_INDEX_CACHE[venue] = proceedings_by_year
    _write_json_cache("index", cache_key, _serialize_proceedings_map(proceedings_by_year))

    logger.info(
        "Discovered %d proceedings keys for venue=%s from index page",
        len(proceedings_by_year),
        venue,
    )
    return proceedings_by_year


def discover_dblp_proceedings_keys(venue: str) -> dict[int, str]:
    """Discover year -> exact DBLP TOC BHT key mapping for a venue."""
    return {
        year: proceedings.toc_key
        for year, proceedings in discover_dblp_proceedings(venue).items()
    }


def discover_dblp_years(venue: str) -> list[int]:
    """Discover available proceedings years for a DBLP venue."""
    return sorted(discover_dblp_proceedings(venue).keys())


def scrape_dblp_venue_proceedings(
    venue: str,
    start_year: int | None = None,
    end_year: int | None = None,
    max_results_per_year: int = 100,
    fallback_to_search: bool = False,
) -> list[Paper]:
    """Historical backfill for a venue using discovered proceedings keys first."""
    venue = venue.lower().strip()

    discovered = discover_dblp_proceedings(venue)
    if not discovered:
        if start_year is None:
            start_year = 1950
        if end_year is None:
            end_year = datetime.now().year
    else:
        if start_year is None:
            start_year = min(discovered)
        if end_year is None:
            end_year = max(discovered)

    if start_year is None:
        start_year = 1950
    if end_year is None:
        end_year = datetime.now().year

    discovered_years = [y for y in sorted(discovered.keys()) if start_year <= y <= end_year]

    papers: list[Paper] = []
    seen_titles: set[str] = set()

    for year in discovered_years:
        proceedings = discovered[year]
        logger.info(
            "DBLP proceedings fetch: venue=%s year=%s key=%s number=%s",
            venue,
            year,
            proceedings.toc_key,
            proceedings.number,
        )
        hits = _fetch_dblp_toc(proceedings.toc_key, number=proceedings.number)

        for hit in hits:
            paper = _info_to_paper(hit, selection="main")
            if not paper:
                continue

            norm_title = _norm_key(paper.title)
            if norm_title in seen_titles:
                continue

            seen_titles.add(norm_title)
            papers.append(paper)

    if fallback_to_search:
        missing_years = [
            year
            for year in range(start_year, end_year + 1)
            if year not in discovered
        ]
        queries = DBLP_VENUE_SEARCHES.get(venue, [])

        for year in missing_years:
            logger.info("DBLP fallback search: venue=%s year=%s", venue, year)
            for query in queries:
                hits = _fetch_dblp_search(
                    query=query,
                    start_year=year,
                    end_year=year,
                    max_results=max_results_per_year,
                )

                for hit in hits:
                    paper = _info_to_paper(hit, selection="main")
                    if not paper:
                        continue

                    norm_title = _norm_key(paper.title)
                    if norm_title in seen_titles:
                        continue

                    seen_titles.add(norm_title)
                    papers.append(paper)

    logger.info(
        "Scraped %d DBLP papers for venue=%s year_range=%s-%s (proceedings-first)",
        len(papers),
        venue,
        start_year,
        end_year,
    )
    return papers


def scrape_dblp_venue_history(
    venue: str,
    start_year: int | None = None,
    end_year: int | None = None,
    max_results_per_year: int = 100,
    fallback_to_search: bool = False,
) -> list[Paper]:
    """Compatibility wrapper for venue history backfill.

    This now uses proceedings-first discovery and only falls back to publication
    search for missing years.
    """
    return scrape_dblp_venue_proceedings(
        venue=venue,
        start_year=start_year,
        end_year=end_year,
        max_results_per_year=max_results_per_year,
        fallback_to_search=fallback_to_search,
    )


DBLP_HISTORY_VENUES = frozenset(DBLP_VENUE_INDEX_PATHS)


def parse_dblp_conference_id(conf_id: str) -> tuple[str, int] | None:
    """Parse a dynamic DBLP conference ID like ijcai_2000."""
    venue, sep, year_text = conf_id.lower().rpartition("_")
    if not sep or venue not in DBLP_HISTORY_VENUES:
        return None

    year = _parse_year(year_text)
    if year is None:
        return None

    return venue, year


def is_dblp_history_conf_id(conf_id: str) -> bool:
    return parse_dblp_conference_id(conf_id) is not None


def scrape_dblp_conference_id(conf_id: str) -> list[Paper]:
    """Scrape a DBLP venue/year ID, including historical years."""
    if conf_id in DBLP_CONFERENCES:
        return _scrape_dblp(conf_id)

    parsed = parse_dblp_conference_id(conf_id)
    if parsed is None:
        raise ValueError(f"Not a supported DBLP conference ID: {conf_id}")

    venue, year = parsed
    return scrape_dblp_venue_proceedings(
        venue=venue,
        start_year=year,
        end_year=year,
        fallback_to_search=False,
    )


SCRAPERS = {conf_id: partial(_scrape_dblp, conf_id) for conf_id in DBLP_CONFERENCES}
