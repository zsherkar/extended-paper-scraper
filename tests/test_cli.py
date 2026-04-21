import argparse
from unittest.mock import MagicMock, patch

import pytest

from ppr.cli import DEFAULT_SOURCE_IDS, _resolve_crawl_targets, cmd_crawl
from ppr.scrapers.arxiv import ArxivPolicyError, DEFAULT_ARXIV_SOURCE_IDS
from ppr.scrapers.public_web import DEFAULT_PUBLIC_SOURCE_IDS, PUBLIC_SOURCE_GROUPS


class TestResolveCrawlTargets:
    def test_defaults_to_combined_source_bundle_when_omitted(self):
        targets = _resolve_crawl_targets([], include_default_public_sources=True)
        assert targets == list(DEFAULT_SOURCE_IDS)

    def test_appends_default_sources_for_conference_crawls(self):
        targets = _resolve_crawl_targets(
            ["iclr_2025"],
            include_default_public_sources=True,
        )

        assert targets[0] == "iclr_2025"
        for source_id in DEFAULT_PUBLIC_SOURCE_IDS:
            assert source_id in targets
        for source_id in DEFAULT_ARXIV_SOURCE_IDS:
            assert source_id in targets

    def test_public_source_only_calls_stay_narrow(self):
        targets = _resolve_crawl_targets(
            ["openai_newsroom"],
            include_default_public_sources=True,
        )

        assert targets == ["openai_newsroom"]

    def test_reviewed_arxiv_source_only_calls_stay_narrow(self):
        targets = _resolve_crawl_targets(
            ["arxiv_cs_ai_recent"],
            include_default_public_sources=True,
        )

        assert targets == ["arxiv_cs_ai_recent"]

    def test_group_alias_expands_without_pulling_in_other_groups(self):
        targets = _resolve_crawl_targets(
            ["frontier_labs"],
            include_default_public_sources=True,
        )

        assert targets == list(PUBLIC_SOURCE_GROUPS["frontier_labs"])

    def test_arxiv_group_alias_expands_without_pulling_in_other_groups(self):
        targets = _resolve_crawl_targets(
            ["arxiv_recent"],
            include_default_public_sources=True,
        )

        assert targets == list(DEFAULT_ARXIV_SOURCE_IDS)

    def test_default_sources_alias_matches_combined_default_bundle(self):
        targets = _resolve_crawl_targets(
            ["default_sources"],
            include_default_public_sources=True,
        )

        assert targets == list(DEFAULT_SOURCE_IDS)

    def test_can_opt_out_of_default_sources(self):
        targets = _resolve_crawl_targets(
            ["iclr_2025"],
            include_default_public_sources=False,
        )

        assert targets == ["iclr_2025"]


class TestArxivCrawlBlocking:
    def test_blocks_direct_unreviewed_arxiv_target_before_resolution_falls_through(self):
        args = argparse.Namespace(
            conferences=["arxiv_topic"],
            no_default_public_sources=True,
            username=None,
            password=None,
        )

        with pytest.raises(ArxivPolicyError, match="before any arXiv request was sent"):
            cmd_crawl(args)

    def test_blocks_mixed_target_sets_before_other_scrapers_run(self):
        args = argparse.Namespace(
            conferences=["aaai_2025", "arxiv_topic"],
            no_default_public_sources=True,
            username=None,
            password=None,
        )

        fake_scraper = MagicMock(return_value=[])
        with patch.dict("ppr.cli.SCRAPERS", {"aaai_2025": fake_scraper}, clear=True):
            with pytest.raises(ArxivPolicyError, match="before any arXiv request was sent"):
                cmd_crawl(args)

        fake_scraper.assert_not_called()

    def test_allows_reviewed_arxiv_targets_to_run(self):
        args = argparse.Namespace(
            conferences=["arxiv_cs_ai_recent"],
            no_default_public_sources=True,
            username=None,
            password=None,
        )

        fake_papers = [MagicMock(to_json=lambda: "{}")]
        with patch.dict("ppr.cli.SCRAPERS", {"arxiv_cs_ai_recent": MagicMock(return_value=fake_papers)}, clear=True):
            with patch("ppr.cli._save_papers") as mock_save:
                cmd_crawl(args)

        mock_save.assert_called_once()
