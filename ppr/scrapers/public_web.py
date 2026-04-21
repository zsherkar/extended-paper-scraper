"""Scrape recent public AI posts from frontier labs and widely read researchers."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ppr.models import Paper

logger = logging.getLogger(__name__)

REQUEST_HEADERS = {
    "User-Agent": (
        "paper-explorer/0.1 "
        "(https://github.com/brightjade/paper-explorer; public source crawler)"
    ),
}

GENERIC_LINK_TEXTS = {
    "",
    "learn more",
    "read more",
    "read",
    "next",
    "previous",
    "view all",
}


@dataclass(frozen=True)
class PublicWebSource:
    source_id: str
    name: str
    source_type: str
    source_category: str
    home_url: str
    feed_url: str = ""
    listing_mode: str = "auto"
    include_url_substrings: tuple[str, ...] = ()
    exclude_url_substrings: tuple[str, ...] = ()
    max_items: int = 15


PUBLIC_WEB_SOURCES = {
    "openai_newsroom": PublicWebSource(
        source_id="openai_newsroom",
        name="OpenAI Newsroom",
        source_type="frontier_lab",
        source_category="lab_news",
        home_url="https://openai.com/newsroom/",
        feed_url="https://openai.com/news/rss.xml",
        listing_mode="feed",
    ),
    "anthropic_news": PublicWebSource(
        source_id="anthropic_news",
        name="Anthropic News",
        source_type="frontier_lab",
        source_category="lab_news",
        home_url="https://www.anthropic.com/news",
        listing_mode="html",
        include_url_substrings=("/news/",),
        exclude_url_substrings=("/newsroom", "responsible-scaling-policy"),
    ),
    "mistral_news": PublicWebSource(
        source_id="mistral_news",
        name="Mistral AI News",
        source_type="frontier_lab",
        source_category="lab_news",
        home_url="https://mistral.ai/news",
        listing_mode="html",
        include_url_substrings=("/news/",),
        exclude_url_substrings=("opengraph-image",),
    ),
    "google_ai_blog": PublicWebSource(
        source_id="google_ai_blog",
        name="Google AI Blog",
        source_type="frontier_lab",
        source_category="lab_blog",
        home_url="https://blog.google/technology/ai/",
        feed_url="https://blog.google/technology/ai/rss/",
        listing_mode="feed",
    ),
    "google_research_blog": PublicWebSource(
        source_id="google_research_blog",
        name="Google Research Blog",
        source_type="frontier_lab",
        source_category="research_blog",
        home_url="https://research.google/blog/",
        feed_url="https://research.google/blog/rss/",
        listing_mode="feed",
    ),
    "meta_ai_blog": PublicWebSource(
        source_id="meta_ai_blog",
        name="Meta AI Blog",
        source_type="frontier_lab",
        source_category="lab_blog",
        home_url="https://ai.meta.com/blog/",
        listing_mode="html",
        include_url_substrings=("/blog/",),
        exclude_url_substrings=("?page=",),
    ),
    "huggingface_blog": PublicWebSource(
        source_id="huggingface_blog",
        name="Hugging Face Blog",
        source_type="frontier_lab",
        source_category="lab_blog",
        home_url="https://huggingface.co/blog",
        feed_url="https://huggingface.co/blog/feed.xml",
        listing_mode="feed",
    ),
    "nvidia_blog": PublicWebSource(
        source_id="nvidia_blog",
        name="NVIDIA Blog",
        source_type="frontier_lab",
        source_category="lab_blog",
        home_url="https://blogs.nvidia.com/blog/",
        feed_url="https://blogs.nvidia.com/feed/",
        listing_mode="feed",
    ),
    "allenai_blog": PublicWebSource(
        source_id="allenai_blog",
        name="AI2 Blog",
        source_type="frontier_lab",
        source_category="research_blog",
        home_url="https://allenai.org/blog",
        feed_url="https://allenai.org/rss.xml",
        listing_mode="feed",
    ),
    "sakana_ai_blog": PublicWebSource(
        source_id="sakana_ai_blog",
        name="Sakana AI Blog",
        source_type="frontier_lab",
        source_category="lab_blog",
        home_url="https://sakana.ai/",
        feed_url="https://sakana.ai/feed.xml",
        listing_mode="feed",
    ),
    "together_ai_blog": PublicWebSource(
        source_id="together_ai_blog",
        name="Together AI Blog",
        source_type="frontier_lab",
        source_category="lab_blog",
        home_url="https://www.together.ai/blog",
        feed_url="https://www.together.ai/blog/rss.xml",
        listing_mode="feed",
    ),
    "lilian_weng": PublicWebSource(
        source_id="lilian_weng",
        name="Lilian Weng",
        source_type="ai_person",
        source_category="researcher_blog",
        home_url="https://lilianweng.github.io/",
        feed_url="https://lilianweng.github.io/index.xml",
        listing_mode="feed",
    ),
    "chip_huyen": PublicWebSource(
        source_id="chip_huyen",
        name="Chip Huyen",
        source_type="ai_person",
        source_category="researcher_blog",
        home_url="https://huyenchip.com/",
        feed_url="https://huyenchip.com/feed.xml",
        listing_mode="feed",
    ),
    "jay_alammar": PublicWebSource(
        source_id="jay_alammar",
        name="Jay Alammar",
        source_type="ai_person",
        source_category="researcher_blog",
        home_url="https://jalammar.github.io/",
        feed_url="https://jalammar.github.io/feed.xml",
        listing_mode="feed",
    ),
    "sebastian_raschka": PublicWebSource(
        source_id="sebastian_raschka",
        name="Sebastian Raschka",
        source_type="ai_person",
        source_category="researcher_newsletter",
        home_url="https://magazine.sebastianraschka.com/",
        feed_url="https://magazine.sebastianraschka.com/feed",
        listing_mode="feed",
    ),
    "simon_willison": PublicWebSource(
        source_id="simon_willison",
        name="Simon Willison",
        source_type="ai_person",
        source_category="widely_read_blog",
        home_url="https://simonwillison.net/",
        feed_url="https://simonwillison.net/atom/everything/",
        listing_mode="feed",
    ),
    "hamel_husain": PublicWebSource(
        source_id="hamel_husain",
        name="Hamel Husain",
        source_type="ai_person",
        source_category="researcher_blog",
        home_url="https://hamel.dev/",
        feed_url="https://hamel.dev/index.xml",
        listing_mode="feed",
    ),
    "ethan_mollick": PublicWebSource(
        source_id="ethan_mollick",
        name="Ethan Mollick",
        source_type="ai_person",
        source_category="widely_read_newsletter",
        home_url="https://www.oneusefulthing.org/",
        feed_url="https://www.oneusefulthing.org/feed",
        listing_mode="feed",
    ),
    "nathan_lambert": PublicWebSource(
        source_id="nathan_lambert",
        name="Nathan Lambert",
        source_type="ai_person",
        source_category="researcher_newsletter",
        home_url="https://www.interconnects.ai/",
        feed_url="https://www.interconnects.ai/feed",
        listing_mode="feed",
    ),
    "latent_space": PublicWebSource(
        source_id="latent_space",
        name="Latent Space",
        source_type="ai_person",
        source_category="widely_read_newsletter",
        home_url="https://www.latent.space/",
        feed_url="https://www.latent.space/feed",
        listing_mode="feed",
    ),
    "the_gradient": PublicWebSource(
        source_id="the_gradient",
        name="The Gradient",
        source_type="ai_person",
        source_category="research_community",
        home_url="https://thegradient.pub/",
        feed_url="https://thegradient.pub/rss/",
        listing_mode="feed",
    ),
    "jack_clark": PublicWebSource(
        source_id="jack_clark",
        name="Jack Clark",
        source_type="ai_person",
        source_category="widely_read_newsletter",
        home_url="https://jack-clark.net/",
        feed_url="https://jack-clark.net/feed/",
        listing_mode="feed",
    ),
}

PUBLIC_SOURCE_GROUPS = {
    "frontier_labs": tuple(
        source_id
        for source_id, source in PUBLIC_WEB_SOURCES.items()
        if source.source_type == "frontier_lab"
    ),
    "ai_people": tuple(
        source_id
        for source_id, source in PUBLIC_WEB_SOURCES.items()
        if source.source_type == "ai_person"
    ),
}
PUBLIC_SOURCE_GROUPS["default_public_sources"] = tuple(
    [*PUBLIC_SOURCE_GROUPS["frontier_labs"], *PUBLIC_SOURCE_GROUPS["ai_people"]]
)
PUBLIC_SOURCE_GROUPS["default_sources"] = PUBLIC_SOURCE_GROUPS["default_public_sources"]
DEFAULT_PUBLIC_SOURCE_IDS = PUBLIC_SOURCE_GROUPS["default_public_sources"]


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    return _collapse_whitespace(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _is_feed_response(response: requests.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    text = response.text.lstrip()
    return (
        "xml" in content_type
        or "rss" in content_type
        or "atom" in content_type
        or text.startswith("<?xml")
        or text.startswith("<rss")
        or text.startswith("<feed")
    )


def _fetch(url: str) -> requests.Response:
    response = requests.get(url, timeout=60, headers=REQUEST_HEADERS)
    response.raise_for_status()
    return response


def _discover_feed_url(source: PublicWebSource) -> str | None:
    html = _fetch(source.home_url).text
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    for tag in soup.find_all(["link", "a"], href=True):
        href = urljoin(source.home_url, tag["href"])
        href_lower = href.lower()
        rel = " ".join(tag.get("rel", [])) if tag.get("rel") else ""
        type_attr = (tag.get("type") or "").lower()
        if (
            "rss" in href_lower
            or "feed" in href_lower
            or "atom" in href_lower
            or "rss" in type_attr
            or "atom" in type_attr
            or ("alternate" in rel.lower() and href_lower.endswith((".xml", "/feed", "/rss")))
        ):
            candidates.append(href)

    page_url = source.home_url.rstrip("/")
    candidates.extend(
        [
            f"{page_url}/feed",
            f"{page_url}/rss",
            f"{page_url}/rss/",
            f"{page_url}/feed.xml",
            f"{page_url}/rss.xml",
            f"{page_url}/index.xml",
            f"{page_url}/atom.xml",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        normalized = _normalize_url(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            response = _fetch(candidate)
        except requests.RequestException:
            continue
        if _is_feed_response(response):
            return candidate
    return None


def _iter_children_with_name(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if _strip_ns(child.tag) == name]


def _first_child_text(node: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in list(node):
        if _strip_ns(child.tag) in wanted and child.text:
            return _collapse_whitespace(child.text)
    return ""


def _all_category_values(node: ET.Element) -> list[str]:
    values = []
    for child in list(node):
        if _strip_ns(child.tag) != "category":
            continue
        term = child.attrib.get("term") or child.text or ""
        term = _collapse_whitespace(term)
        if term:
            values.append(term)
    return list(dict.fromkeys(values))


def _parse_rss_items(root: ET.Element, source: PublicWebSource, source_url: str) -> list[Paper]:
    channel = next((child for child in list(root) if _strip_ns(child.tag) == "channel"), root)
    papers = []
    for item in _iter_children_with_name(channel, "item")[: source.max_items]:
        title = _first_child_text(item, "title")
        link = _first_child_text(item, "link")
        summary = _html_to_text(_first_child_text(item, "description", "encoded"))
        published = _first_child_text(item, "pubDate", "published", "updated")
        authors = []

        creator = _first_child_text(item, "creator", "author")
        if creator:
            authors = [creator]

        if not title or not link:
            continue

        papers.append(
            Paper(
                title=title,
                link=link,
                authors=authors or [source.name],
                selection=source.source_category,
                keywords=_all_category_values(item),
                abstract=summary,
                publication_date=published,
                source_id=source.source_id,
                source_name=source.name,
                source_type=source.source_type,
                source_category=source.source_category,
                source_url=source.home_url,
                external_ids={"feed_url": source_url},
            )
        )
    return papers


def _parse_atom_entries(root: ET.Element, source: PublicWebSource, source_url: str) -> list[Paper]:
    papers = []
    for entry in _iter_children_with_name(root, "entry")[: source.max_items]:
        title = _first_child_text(entry, "title")
        summary = _html_to_text(_first_child_text(entry, "summary", "content"))
        published = _first_child_text(entry, "published", "updated")
        authors = []
        link = ""

        for child in list(entry):
            child_name = _strip_ns(child.tag)
            if child_name == "author":
                name = _first_child_text(child, "name")
                if name:
                    authors.append(name)
            elif child_name == "link":
                rel = child.attrib.get("rel", "alternate")
                href = child.attrib.get("href", "")
                if href and (rel == "alternate" or not link):
                    link = href

        if not title or not link:
            continue

        papers.append(
            Paper(
                title=title,
                link=link,
                authors=authors or [source.name],
                selection=source.source_category,
                keywords=_all_category_values(entry),
                abstract=summary,
                publication_date=published,
                source_id=source.source_id,
                source_name=source.name,
                source_type=source.source_type,
                source_category=source.source_category,
                source_url=source.home_url,
                external_ids={"feed_url": source_url},
            )
        )
    return papers


def _scrape_feed(source: PublicWebSource, feed_url: str) -> list[Paper]:
    response = _fetch(feed_url)
    root = ET.fromstring(response.text)
    root_name = _strip_ns(root.tag)
    if root_name == "feed":
        return _parse_atom_entries(root, source, feed_url)
    return _parse_rss_items(root, source, feed_url)


def _same_domain(url: str, other: str) -> bool:
    left = urlparse(url).netloc.removeprefix("www.")
    right = urlparse(other).netloc.removeprefix("www.")
    return left == right


def _extract_article_urls(source: PublicWebSource, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    home_url = _normalize_url(source.home_url)
    found: list[str] = []

    def add_candidate(raw_url: str) -> None:
        full_url = _normalize_url(urljoin(source.home_url, raw_url))
        if not _same_domain(source.home_url, full_url):
            return
        if full_url == home_url:
            return
        if source.include_url_substrings and not any(
            token in full_url for token in source.include_url_substrings
        ):
            return
        if any(token in full_url for token in source.exclude_url_substrings):
            return
        found.append(full_url)

    for tag in soup.find_all("a", href=True):
        link_text = _collapse_whitespace(tag.get_text(" ", strip=True)).lower()
        raw_href = tag["href"]
        if link_text in GENERIC_LINK_TEXTS and _normalize_url(urljoin(source.home_url, raw_href)) in found:
            continue
        add_candidate(raw_href)

    # Some modern sites expose recent article paths in embedded JSON rather than
    # navigable anchors. Sweep those relative paths as a fallback.
    regex_hits: list[str] = []
    for prefix in source.include_url_substrings:
        normalized_prefix = prefix.rstrip("/")
        if not normalized_prefix.startswith("/"):
            continue
        pattern = rf"(?:https?://[^\"'\s<>]+)?{re.escape(normalized_prefix)}/[A-Za-z0-9][A-Za-z0-9/_-]*"
        regex_hits.extend(re.findall(pattern, html, flags=re.IGNORECASE))
    for raw_url in regex_hits:
        add_candidate(raw_url)

    return list(dict.fromkeys(found))[: source.max_items]


def _meta_content(soup: BeautifulSoup, key: str, value: str) -> str:
    tag = soup.find("meta", attrs={key: value})
    if not tag:
        return ""
    return _collapse_whitespace(tag.get("content", ""))


def _article_authors(soup: BeautifulSoup) -> list[str]:
    authors: list[str] = []

    for key, value in [
        ("name", "author"),
        ("property", "article:author"),
        ("name", "parsely-author"),
    ]:
        content = _meta_content(soup, key, value)
        if content:
            authors.extend(
                [
                    _collapse_whitespace(part)
                    for part in re.split(r",| and ", content)
                    if _collapse_whitespace(part)
                ]
            )

    for anchor in soup.select("a[rel='author']"):
        text = _collapse_whitespace(anchor.get_text(" ", strip=True))
        if text:
            authors.append(text)

    return list(dict.fromkeys(authors))


def _article_keywords(soup: BeautifulSoup) -> list[str]:
    keywords = _meta_content(soup, "name", "keywords")
    if not keywords:
        return []
    return [_collapse_whitespace(part) for part in keywords.split(",") if _collapse_whitespace(part)]


def _scrape_article_page(source: PublicWebSource, article_url: str) -> Paper | None:
    try:
        response = _fetch(article_url)
    except requests.RequestException as exc:
        logger.warning("Skipping %s article %s: %s", source.source_id, article_url, exc)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    title = (
        _meta_content(soup, "property", "og:title")
        or _meta_content(soup, "name", "twitter:title")
        or _collapse_whitespace(soup.title.get_text(" ", strip=True) if soup.title else "")
    )
    description = (
        _meta_content(soup, "property", "og:description")
        or _meta_content(soup, "name", "description")
        or _meta_content(soup, "name", "twitter:description")
    )
    published = (
        _meta_content(soup, "property", "article:published_time")
        or _meta_content(soup, "name", "article:published_time")
        or _meta_content(soup, "name", "date")
    )
    if not published:
        time_tag = soup.find("time")
        if time_tag:
            published = _collapse_whitespace(time_tag.get("datetime", "") or time_tag.get_text(" ", strip=True))

    if not title:
        return None

    authors = _article_authors(soup) or [source.name]
    return Paper(
        title=title,
        link=article_url,
        authors=authors,
        selection=source.source_category,
        keywords=_article_keywords(soup),
        abstract=description,
        publication_date=published,
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        source_category=source.source_category,
        source_url=source.home_url,
    )


def _scrape_html_listing(source: PublicWebSource) -> list[Paper]:
    listing_html = _fetch(source.home_url).text
    article_urls = _extract_article_urls(source, listing_html)
    papers = []
    for article_url in article_urls:
        paper = _scrape_article_page(source, article_url)
        if paper is not None:
            papers.append(paper)
    return papers


def _scrape_public_web_source(source_id: str) -> list[Paper]:
    source = PUBLIC_WEB_SOURCES[source_id]
    logger.info("Scraping public source %s from %s", source_id, source.home_url)

    feed_url = source.feed_url
    if source.listing_mode in {"feed", "auto"} and not feed_url:
        try:
            feed_url = _discover_feed_url(source)
        except requests.RequestException as exc:
            logger.warning("Feed discovery failed for %s: %s", source_id, exc)

    papers: list[Paper]
    if source.listing_mode == "feed" and feed_url:
        papers = _scrape_feed(source, feed_url)
    elif source.listing_mode == "html":
        papers = _scrape_html_listing(source)
    elif feed_url:
        papers = _scrape_feed(source, feed_url)
    else:
        papers = _scrape_html_listing(source)

    deduped = []
    seen: set[str] = set()
    for paper in papers:
        key = _normalize_url(paper.link) if paper.link else paper.title.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(paper)

    logger.info("Found %d items for %s", len(deduped), source_id)
    return deduped


SCRAPERS = {
    source_id: partial(_scrape_public_web_source, source_id)
    for source_id in PUBLIC_WEB_SOURCES
}
