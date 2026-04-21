import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ppr.scrapers.arxiv import (
    ARXIV_ACCESS_DISABLED_REASON,
    ARXIV_API_URL,
    ARXIV_APPROVED_SOURCE_IDS,
    ARXIV_SOURCES,
    ArxivApiPolicy,
    ArxivPolicyError,
    ArxivRateLimiter,
    _parse_arxiv_feed,
    _read_cached_papers,
    _write_cached_papers,
    build_arxiv_abs_link,
    build_arxiv_headers,
    build_arxiv_pdf_link,
    ensure_arxiv_access_allowed,
    is_arxiv_target,
)
from ppr.models import Paper


ARXIV_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <updated>2026-04-19T12:00:00Z</updated>
    <published>2026-04-18T10:00:00Z</published>
    <title>World Models for Robotics</title>
    <summary>  A recent paper about   model-based learning. </summary>
    <author><name>Alice Example</name></author>
    <author><name>Bob Example</name></author>
    <category term="cs.RO" />
    <category term="cs.AI" />
  </entry>
</feed>
"""


class TestArxivApiPolicy:
    def test_build_query_url_uses_export_host_and_expected_params(self):
        policy = ArxivApiPolicy()

        url = policy.build_query_url(
            search_query='cat:cs.AI AND abs:"world model"',
            start=25,
            max_results=50,
            sort_by="submittedDate",
            sort_order="descending",
        )

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == ARXIV_API_URL
        assert params["search_query"] == ['cat:cs.AI AND abs:"world model"']
        assert params["start"] == ["25"]
        assert params["max_results"] == ["50"]
        assert params["sortBy"] == ["submittedDate"]
        assert params["sortOrder"] == ["descending"]

    def test_rejects_non_export_host(self):
        policy = ArxivApiPolicy(api_url="https://arxiv.org/api/query")

        with pytest.raises(ArxivPolicyError, match="export.arxiv.org"):
            policy.validate_api_url()

    def test_rejects_request_slices_larger_than_2000(self):
        policy = ArxivApiPolicy()

        with pytest.raises(ArxivPolicyError, match="<= 2000"):
            policy.validate_request(max_results=2001)

    def test_rejects_request_windows_past_30000(self):
        policy = ArxivApiPolicy()

        with pytest.raises(ArxivPolicyError, match="30000"):
            policy.validate_request(start=29990, max_results=20)

    def test_rejects_multiple_connections(self):
        policy = ArxivApiPolicy()

        with pytest.raises(ArxivPolicyError, match="single connection"):
            policy.validate_request(connections=2)

    def test_rejects_non_metadata_content_modes(self):
        policy = ArxivApiPolicy()

        with pytest.raises(ArxivPolicyError, match="metadata"):
            policy.validate_request(content_mode="pdf")

    def test_requires_search_query_or_id_list(self):
        policy = ArxivApiPolicy()

        with pytest.raises(ArxivPolicyError, match="search_query or id_list"):
            policy.build_query_params()

    def test_normalizes_iterable_id_list(self):
        policy = ArxivApiPolicy()

        params = policy.build_query_params(id_list=["2401.00001", " 2401.00002v2 "])

        assert params["id_list"] == "2401.00001,2401.00002v2"

    def test_validates_sort_options(self):
        policy = ArxivApiPolicy()

        with pytest.raises(ArxivPolicyError, match="sortBy"):
            policy.validate_request(sort_by="citationCount")

        with pytest.raises(ArxivPolicyError, match="sortOrder"):
            policy.validate_request(sort_order="desc")


class TestArxivRateLimiter:
    def test_first_request_runs_without_sleep(self):
        sleeps: list[float] = []
        limiter = ArxivRateLimiter(
            monotonic_fn=lambda: 100.0,
            sleep_fn=sleeps.append,
        )

        waited = limiter.wait_for_slot()

        assert waited == 0.0
        assert sleeps == []

    def test_second_request_waits_until_three_seconds_elapsed(self):
        sleeps: list[float] = []
        times = iter([100.0, 101.2])
        limiter = ArxivRateLimiter(
            monotonic_fn=lambda: next(times),
            sleep_fn=sleeps.append,
        )

        limiter.wait_for_slot()
        waited = limiter.wait_for_slot()

        assert waited == pytest.approx(1.8)
        assert sleeps == [pytest.approx(1.8)]


class TestArxivHelpers:
    def test_recognizes_reserved_arxiv_targets(self):
        assert is_arxiv_target("arxiv")
        assert is_arxiv_target("arxiv_topic")
        assert is_arxiv_target("arxiv-world-models")
        assert not is_arxiv_target("aaai_2025")

    def test_all_built_in_sources_are_approved(self):
        assert set(ARXIV_SOURCES) == set(ARXIV_APPROVED_SOURCE_IDS)

    def test_allows_reviewed_arxiv_sources(self):
        ensure_arxiv_access_allowed(["arxiv_cs_ai_recent", "arxiv_cs_lg_recent"])

    def test_blocks_unreviewed_arxiv_targets_before_any_request(self):
        with pytest.raises(ArxivPolicyError, match="before any arXiv request was sent"):
            ensure_arxiv_access_allowed(["arxiv_topic"])

    def test_block_message_uses_shared_reason(self):
        with pytest.raises(ArxivPolicyError) as exc_info:
            ensure_arxiv_access_allowed(["arxiv_topic", "arxiv_recent_experiment"])

        assert ARXIV_ACCESS_DISABLED_REASON in str(exc_info.value)
        assert "arxiv_topic, arxiv_recent_experiment" in str(exc_info.value)

    def test_prefers_abstract_page_links(self):
        assert build_arxiv_abs_link("2401.12345") == "https://arxiv.org/abs/2401.12345"
        assert build_arxiv_pdf_link("2401.12345v2") == "https://arxiv.org/pdf/2401.12345v2"

    def test_build_headers_are_descriptive(self):
        headers = build_arxiv_headers(contact_email="research@example.com")

        assert headers["Accept"] == "application/atom+xml"
        assert "research@example.com" in headers["User-Agent"]
        assert "metadata-only" in headers["User-Agent"]


class TestArxivParsingAndCache:
    def test_parse_feed_maps_metadata(self):
        source = ARXIV_SOURCES["arxiv_cs_ro_recent"]

        papers = _parse_arxiv_feed(ARXIV_FIXTURE, source)

        assert len(papers) == 1
        paper = papers[0]
        assert paper.title == "World Models for Robotics"
        assert paper.link == "https://arxiv.org/abs/2401.12345v2"
        assert paper.open_access_pdf == "https://arxiv.org/pdf/2401.12345v2"
        assert paper.authors == ["Alice Example", "Bob Example"]
        assert paper.keywords == ["cs.RO", "cs.AI"]
        assert paper.external_ids == {"ArXiv": "2401.12345v2"}
        assert paper.source_id == "arxiv_cs_ro_recent"
        assert paper.source_type == "preprint_archive"

    def test_cache_round_trip(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("PPR_ARXIV_CACHE_DIR", str(tmp_path))
        from ppr.scrapers import arxiv as arxiv_module

        monkeypatch.setattr(arxiv_module, "ARXIV_CACHE_DIR", tmp_path)
        monkeypatch.setattr(arxiv_module, "ARXIV_CACHE_TTL_SECONDS", 999999)

        papers = [
            Paper(
                title="Cached",
                link="https://arxiv.org/abs/2401.00001",
                authors=["A"],
                source_id="arxiv_cs_ai_recent",
                source_name="arXiv cs.AI Recent",
                source_type="preprint_archive",
                source_category="arxiv_recent_ai",
                source_url="https://arxiv.org/list/cs.AI/recent",
                external_ids={"ArXiv": "2401.00001"},
            )
        ]

        _write_cached_papers("arxiv_cs_ai_recent", papers)
        cached = _read_cached_papers("arxiv_cs_ai_recent")

        assert cached is not None
        assert len(cached) == 1
        assert cached[0].title == "Cached"
