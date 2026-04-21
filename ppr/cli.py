import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from ppr.scrapers import SCRAPERS
from ppr.scrapers.arxiv import (
    ARXIV_SOURCE_GROUPS,
    ARXIV_SOURCES,
    DEFAULT_ARXIV_SOURCE_IDS,
    ensure_arxiv_access_allowed,
)
from ppr.scrapers.public_web import DEFAULT_PUBLIC_SOURCE_IDS, PUBLIC_SOURCE_GROUPS, PUBLIC_WEB_SOURCES
from ppr.scrapers.dblp import (
    DBLP_HISTORY_VENUES,
    discover_dblp_years,
    is_dblp_history_conf_id,
    scrape_dblp_conference_id,
    scrape_dblp_venue_history,
)
from ppr.api_client import OpenReviewAPIClient, create_openreview_client, create_openreview_v1_client
from ppr.citations import CitationFetcher
from ppr.config import CrawlConfig
from ppr.models import Paper

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SOURCE_GROUPS = {
    **PUBLIC_SOURCE_GROUPS,
    **ARXIV_SOURCE_GROUPS,
}
DIRECT_SOURCE_IDS = set(PUBLIC_WEB_SOURCES) | set(ARXIV_SOURCES)
DEFAULT_SOURCE_IDS = [*DEFAULT_PUBLIC_SOURCE_IDS, *DEFAULT_ARXIV_SOURCE_IDS]
SOURCE_GROUPS["default_sources"] = tuple(DEFAULT_SOURCE_IDS)


def _available_targets() -> list[str]:
    from_configs = {p.stem for p in CONFIGS_DIR.glob("*.yaml")}
    source_groups = set(SOURCE_GROUPS)
    return sorted(from_configs | SCRAPERS.keys() | source_groups)


def _dedupe_targets(targets: list[str]) -> list[str]:
    return list(dict.fromkeys(targets))


def _resolve_crawl_targets(
    targets: list[str],
    *,
    include_default_public_sources: bool,
) -> list[str]:
    expanded: list[str] = []
    for target in targets:
        if target in SOURCE_GROUPS:
            expanded.extend(SOURCE_GROUPS[target])
        else:
            expanded.append(target)
    expanded = _dedupe_targets(expanded)

    if not include_default_public_sources:
        return expanded
    if not expanded:
        return list(DEFAULT_SOURCE_IDS)

    # Explicit source-only calls should stay narrow; conference-oriented crawls
    # append the reviewed default source bundle automatically.
    if all(target in DIRECT_SOURCE_IDS for target in expanded):
        return expanded

    return _dedupe_targets([*expanded, *DEFAULT_SOURCE_IDS])


def _resolve_input(conf_id: str) -> Path:
    return DATA_DIR / conf_id / "papers.jsonl"


def _read_papers_jsonl(path: Path) -> list[Paper]:
    with open(path, encoding="utf-8") as f:
        return [Paper.from_dict(json.loads(line)) for line in f if line.strip()]


def _save_papers(papers: list[Paper], conf_id: str) -> Path:
    save_path = DATA_DIR / conf_id / "papers.jsonl"
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "w", encoding="utf-8") as f:
        for paper in papers:
            f.write(paper.to_json() + "\n")
    return save_path


def _add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--username",
        default=os.environ.get("OPENREVIEW_USERNAME"),
        help="OpenReview username (default: OPENREVIEW_USERNAME env var).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("OPENREVIEW_PASSWORD"),
        help="OpenReview password (default: OPENREVIEW_PASSWORD env var).",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ppr",
        description="Crawl conference papers plus approved arXiv, AI lab, and researcher writing sources.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # crawl
    crawl_parser = subparsers.add_parser(
        "crawl", help="Fetch conference papers plus approved default sources (e.g., ppr crawl iclr_2025 neurips_2025)"
    )
    crawl_parser.add_argument(
        "conferences", nargs="*",
        help=(
            "Conference IDs, public-source IDs, or source bundles "
            "(e.g., iclr_2025 openai_newsroom frontier_labs arxiv_recent). "
            "If omitted, crawl the default source bundle."
        ),
    )
    crawl_parser.add_argument(
        "--no-default-public-sources",
        "--no-default-sources",
        action="store_true",
        help="Do not append the default source bundle (frontier labs, AI writers, and approved arXiv feeds).",
    )
    _add_auth_args(crawl_parser)

    # dblp-history
    dblp_history_parser = subparsers.add_parser(
        "dblp-history",
        help="Scrape a DBLP venue range (e.g., ppr dblp-history ijcai --start-year 2000 --end-year 2025)",
    )
    dblp_history_parser.add_argument(
        "venue",
        choices=sorted(DBLP_HISTORY_VENUES),
        help="DBLP venue slug.",
    )
    dblp_history_parser.add_argument("--start-year", type=int, help="First year to scrape.")
    dblp_history_parser.add_argument("--end-year", type=int, help="Last year to scrape.")
    dblp_history_parser.add_argument(
        "--fallback-search",
        action="store_true",
        help="Use DBLP publication search only for requested years without discovered proceedings.",
    )
    dblp_history_parser.add_argument(
        "--max-results-per-year",
        type=int,
        default=100,
        help="Fallback publication-search cap per missing year (default: 100).",
    )
    dblp_history_parser.add_argument(
        "--output-id",
        help="Output dataset ID under data/ (default: <venue>_<start>_<end>).",
    )
    dblp_history_parser.add_argument(
        "--list-years",
        action="store_true",
        help="Print discovered DBLP proceedings years and exit.",
    )

    # enrich
    enrich_parser = subparsers.add_parser(
        "enrich", help="Enrich papers with Semantic Scholar metadata (e.g., ppr enrich iclr_2025 neurips_2025)"
    )
    enrich_parser.add_argument(
        "conferences", nargs="+",
        help="Conference IDs (e.g., iclr_2025 neurips_2025).",
    )
    enrich_parser.add_argument(
        "--api-key", default=os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
        help="Semantic Scholar API key (optional, increases rate limits).",
    )
    enrich_parser.add_argument(
        "--concurrency", type=int, default=1,
        help="Max concurrent requests (default: 1, matching Semantic Scholar rate limit).",
    )

    # validate
    validate_parser = subparsers.add_parser(
        "validate", help="Cross-reference paper counts against DBLP (e.g., ppr validate iclr_2025)"
    )
    validate_parser.add_argument(
        "conferences", nargs="+",
        help="Conference IDs to validate.",
    )
    validate_parser.add_argument(
        "--tolerance", type=float, default=0.1,
        help="Maximum allowed relative difference (default: 0.1 = 10%%).",
    )

    return parser


def cmd_crawl(args: argparse.Namespace) -> None:
    conf_ids = _resolve_crawl_targets(
        args.conferences,
        include_default_public_sources=not args.no_default_public_sources,
    )
    if not conf_ids:
        raise ValueError("No crawl targets selected.")
    ensure_arxiv_access_allowed(conf_ids)

    # Split into scraped vs OpenReview conferences
    scraped = [c for c in conf_ids if c in SCRAPERS]
    dblp_dynamic = [
        c for c in conf_ids
        if c not in SCRAPERS and is_dblp_history_conf_id(c)
    ]
    openreview = [
        c for c in conf_ids
        if c not in SCRAPERS and c not in dblp_dynamic and (CONFIGS_DIR / f"{c}.yaml").exists()
    ]
    unknown = [c for c in conf_ids if c not in scraped and c not in dblp_dynamic and c not in openreview]

    if unknown:
        available = _available_targets()
        raise FileNotFoundError(
            f"Unknown crawl target(s): {', '.join(unknown)}. "
            f"Available: {', '.join(available)}"
        )

    # Scrape non-OpenReview conferences (no login needed)
    for conf_id in scraped:
        papers = SCRAPERS[conf_id]()
        save_path = _save_papers(papers, conf_id)
        logger.info("Done: %s (%d papers) -> %s", conf_id, len(papers), save_path)

    for conf_id in dblp_dynamic:
        papers = scrape_dblp_conference_id(conf_id)
        save_path = _save_papers(papers, conf_id)
        logger.info("Done: %s (%d papers) -> %s", conf_id, len(papers), save_path)

    # Fetch OpenReview conferences (one login per API version, reuse clients)
    if openreview:
        configs = [CrawlConfig.from_yaml(CONFIGS_DIR / f"{c}.yaml") for c in openreview]
        api_versions = {c.api_version for c in configs}

        # Create only the clients needed (avoids unnecessary logins)
        or_clients = {}
        if 1 in api_versions:
            or_clients[1] = create_openreview_v1_client(
                username=args.username, password=args.password
            )
        if 2 in api_versions:
            or_clients[2] = create_openreview_client(
                username=args.username, password=args.password
            )

        for config in configs:
            client = OpenReviewAPIClient(config, or_clients[config.api_version])
            papers = client.fetch_papers()
            client.save_papers(papers)
            logger.info(
                "Done: %s %s (%d papers) -> %s",
                config.name, config.year, len(papers), config.get_save_path(),
            )


def _enrich_one(conf_id: str, fetcher: CitationFetcher) -> None:
    input_path = _resolve_input(conf_id)
    if not input_path.exists():
        raise FileNotFoundError(
            f"No papers found at {input_path}. Run 'ppr crawl {conf_id}' first."
        )

    papers = _read_papers_jsonl(input_path)

    logger.info("Loaded %d papers from %s", len(papers), input_path)

    output_dir = input_path.parent
    tmp_path = output_dir / ".papers_enriched.tmp.jsonl"
    final_path = output_dir / "papers_enriched.jsonl"

    # Resume: load already-enriched papers from tmp file
    done_papers = []
    if tmp_path.exists():
        done_papers = _read_papers_jsonl(tmp_path)
        done_titles = {p.title for p in done_papers}
        remaining = [p for p in papers if p.title not in done_titles]
        logger.info("Resuming: %d already done, %d remaining", len(done_papers), len(remaining))
    else:
        remaining = papers

    if remaining:
        new_papers = asyncio.run(fetcher.fetch_and_stream(remaining, tmp_path, append=bool(done_papers)))
        all_papers = done_papers + new_papers
    else:
        all_papers = done_papers
        logger.info("All papers already enriched")

    sorted_papers = sorted(
        all_papers,
        key=lambda p: p.citation_count if p.citation_count is not None else -1,
        reverse=True,
    )
    with open(final_path, "w", encoding="utf-8") as f:
        for paper in sorted_papers:
            f.write(paper.to_json() + "\n")
    tmp_path.unlink()
    logger.info("Saved %d papers (sorted by citations) to %s", len(sorted_papers), final_path)


def cmd_enrich(args: argparse.Namespace) -> None:
    api_key = args.api_key or None
    fetcher = CitationFetcher(api_key=api_key, max_concurrency=args.concurrency)

    for conf_id in args.conferences:
        logger.info("Enriching %s...", conf_id)
        _enrich_one(conf_id, fetcher)


def cmd_dblp_history(args: argparse.Namespace) -> None:
    years = discover_dblp_years(args.venue)
    if not years:
        raise RuntimeError(f"No DBLP proceedings years discovered for {args.venue}")

    if args.list_years:
        print(f"{args.venue}: {', '.join(str(y) for y in years)}")
        return

    start_year = args.start_year if args.start_year is not None else min(years)
    end_year = args.end_year if args.end_year is not None else max(years)
    output_id = args.output_id or f"{args.venue}_{start_year}_{end_year}"

    papers = scrape_dblp_venue_history(
        args.venue,
        start_year=start_year,
        end_year=end_year,
        max_results_per_year=args.max_results_per_year,
        fallback_to_search=args.fallback_search,
    )
    save_path = _save_papers(papers, output_id)
    logger.info("Done: %s %s-%s (%d papers) -> %s", args.venue, start_year, end_year, len(papers), save_path)


def cmd_validate(args: argparse.Namespace) -> None:
    from ppr.validate import validate_conference

    results = []
    for conf_id in args.conferences:
        logger.info("Validating %s...", conf_id)
        result = validate_conference(conf_id, tolerance=args.tolerance)
        results.append(result)

    # Print results table
    print(f"\n{'Conference':<28} {'Scraped':>8} {'DBLP':>8} {'Status':<10} {'Message'}")
    print("-" * 80)
    for r in results:
        scraped_str = str(r.scraped) if r.scraped else "-"
        dblp_str = str(r.dblp) if r.dblp else "-"
        print(f"{r.conf_id:<28} {scraped_str:>8} {dblp_str:>8} {r.status:<10} {r.message}")

    failures = [r for r in results if r.status == "FAIL"]
    if failures:
        print(f"\n{len(failures)} conference(s) FAILED validation.")
        raise SystemExit(1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "crawl": cmd_crawl,
        "dblp-history": cmd_dblp_history,
        "enrich": cmd_enrich,
        "validate": cmd_validate,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
