# extender paper retriever

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![GitHub stars](https://img.shields.io/github/stars/brightjade/paper-explorer)](https://github.com/brightjade/paper-explorer/stargazers)
[![GitHub last commit](https://img.shields.io/github/last-commit/brightjade/paper-explorer)](https://github.com/brightjade/paper-explorer/commits/main)
[![Sponsor](https://img.shields.io/badge/sponsor-brightjade-ea4aaa?logo=github-sponsors)](https://github.com/sponsors/brightjade)

Retrieve accepted paper metadata from ML/DL/NLP/CV/Robotics/Security/SE conferences. This customized version preserves the original paper-explorer workflow while adding default source pipelines for frontier-lab blogs/news, AI researcher feeds, approved recent arXiv feeds, historical DBLP backfill, and spreadsheet-oriented literature export.

This repository remains MIT licensed. The original `paper-explorer` work is copyright Minseok Choi, and the custom extensions added in this fork are copyright Ziauddin Sherkar.

## Getting Started

### 1. Install uv

[uv](https://docs.astral.sh/uv/) is a fast Python package manager used to manage dependencies.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installing, restart your terminal so the `uv` command is available.

### 2. Clone and install dependencies

```bash
git clone https://github.com/zsherkar/extended-paper-scraper.git
cd extended-paper-scraper
uv sync
```

This creates a virtual environment and installs all required packages automatically.

### 3. Download paper data

The paper data is hosted as a GitHub Release asset. Run the setup script to download and extract it:

```bash
./setup.sh
```

This downloads the latest data snapshot (~60 MB) and extracts it to `data/`.

> **Note:** Requires either the [GitHub CLI](https://cli.github.com/) (`gh`) or `curl`. If you don't have `gh`, the script falls back to `curl` automatically.

### 4. Set up OpenReview credentials (optional)

Only needed if you want to crawl OpenReview conferences (ICLR, NeurIPS, ICML, COLM, CoRL). Other conferences are scraped from public websites and don't require authentication.

1. Create a free account at https://openreview.net/signup
2. Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

3. Edit `.env` with your OpenReview username and password.

## Custom Additions In This Version

This repo now includes several additions beyond the original conference-only workflow:

- A default source pipeline that appends frontier AI lab blogs/news and widely read AI researcher/writer feeds to conference crawls, so operators do not need to remember extra source flags every time.
- An approved arXiv default pipeline using the official API for recent `cs.AI`, `cs.LG`, `cs.CL`, `cs.CV`, `cs.RO`, `cs.NE`, and `stat.ML` feeds.
- Hard arXiv guardrails: unreviewed `arxiv_*` targets are blocked before any request is sent, approved sources use the `export.arxiv.org` API host, the code enforces pacing, and recent results are cached locally to reduce repeated traffic.
- Historical DBLP backfill support for venues like IJCAI, RSS, ICRA, IROS, ICSE, FSE, ASE, and ISSTA.
- Downstream handling for yearless source datasets so the extraction/export pipeline can use public-source and arXiv data without breaking the conference-focused web build.
- Broader literature-mining and spreadsheet export workflows, including the distillation/model-extraction database exporter and Google-Sheets-ready output bundle.

## Usage

```bash
# Crawl one or more conferences
# Default behavior also appends the built-in source bundle
# (frontier lab blogs/news, AI researcher/writer feeds, and reviewed arXiv feeds).
uv run ppr crawl iclr_2025
uv run ppr crawl iclr_2025 neurips_2025 icml_2025
uv run ppr crawl ijcai_2000

# Refresh just the default source bundle
uv run ppr crawl

# Crawl specific source bundles or source IDs
uv run ppr crawl frontier_labs
uv run ppr crawl ai_people
uv run ppr crawl openai_newsroom anthropic_news
uv run ppr crawl arxiv_recent
uv run ppr crawl arxiv_cs_ai_recent arxiv_cs_lg_recent

# Opt out of the default bundle when you want conference-only crawl output
uv run ppr crawl iclr_2025 --no-default-sources

# Enrich with citation counts and abstracts
uv run ppr enrich iclr_2025
uv run ppr enrich iclr_2025 neurips_2025 icml_2025

# Scrape a DBLP venue range using discovered proceedings years
uv run ppr dblp-history ijcai --start-year 2000 --end-year 2025
uv run ppr dblp-history fse --list-years

# Extract distillation-related papers from local data
uv run python scripts/extract_distillation.py

# Build a filterable distillation paper database + Google Sheets bundle
uv run python scripts/export_distillation_database.py
uv run python scripts/export_distillation_database.py --sync-google-sheet --google-credentials path/to/service-account.json

# Validate paper counts against DBLP
uv run ppr validate iclr_2025
uv run ppr validate iclr_2025 neurips_2025 --tolerance 0.15

# Build static JSON for web app
./build.sh
```

The distillation extractor scans `data/` plus `master_literature.csv` when present and writes CSV, JSONL, Markdown, and Excel outputs under `outputs/distillation_candidates.*`. The Excel workbook includes focused sheets for LLM, non-LLM, attacks, black-box attacks, defenses, data/synthetic methods, compression, policy, techniques, web sources, and the query taxonomy.

The database exporter builds a more spreadsheet-friendly output under `outputs/distillation_paper_database.*` with sortable columns such as title, venue, year, citations, score, matched groups, and filter booleans (`is_llm`, `is_attack`, `is_defense`, etc.). It also writes a Google Sheets import bundle under `outputs/distillation_paper_database_google_sheet/` and can sync directly to a shared sheet when given a Google service account key.

The default source bundle writes recent source data into yearless dataset folders under `data/` such as `data/openai_newsroom/`, `data/lilian_weng/`, and `data/arxiv_cs_ai_recent/`. The extraction/export scripts scan those automatically; the conference-focused web build skips them so the dashboard remains venue/year based.

## Available conferences

Conference ID format: `<venue>_<year>` (e.g., `iclr_2025`). Selections indicate available paper tracks.

### ML

| Conference | 2026 | 2025 | 2024 | 2023 |
|---|---|---|---|---|
| ICLR | oral, poster | oral, spotlight, poster | oral, spotlight, poster | oral, spotlight, poster |
| NeurIPS | | oral, spotlight, poster | oral, spotlight, poster | oral, spotlight, poster |
| ICML | | oral, spotlight, poster | oral, spotlight, poster | oral, poster |
| AAAI | | main | main | main |
| IJCAI | | main | main | main |

NeurIPS also includes `datasets_oral`, `datasets_spotlight`, `datasets_poster` tracks.

### NLP

| Conference | 2025 | 2024 | 2023 |
|---|---|---|---|
| ACL | main, findings, industry | main, findings | main, findings, industry |
| EMNLP | main, findings, industry | main, findings, industry | main, findings, industry |
| NAACL | main, findings, industry | main, findings, industry | |
| COLM | main | main | |
| EACL | | main, findings | main, findings |
| COLING | main | main | |

### CV (CVF / ECVA)

| Conference | 2026 | 2025 | 2024 | 2023 |
|---|---|---|---|---|
| CVPR | | main | main | main |
| ICCV | | main | | main |
| ECCV | | | main | |
| WACV | main | main | main | main |

### Robotics

| Conference | 2025 | 2024 | 2023 |
|---|---|---|---|
| CoRL | oral, poster | main | oral, poster |
| ICRA | main | main | main |
| IROS | main | main | main |
| RSS | main | main | main |

### Security

| Conference | 2025 | 2024 | 2023 |
|---|---|---|---|
| USENIX Security | main | main | main |

### Software Engineering (DBLP)

| Conference | 2025 | 2024 | 2023 |
|---|---|---|---|
| ICSE | main | main | main |
| FSE | main | main | main |
| ASE | main | main | main |
| ISSTA | main | main | main |

DBLP-backed venues can also be scraped historically with `ppr dblp-history <venue> --start-year <year> --end-year <year>`. Supported DBLP venue slugs are `icse`, `fse`, `ase`, `issta`, `icra`, `iros`, `rss`, and `ijcai`.

## Default Source Pipeline

`ppr crawl` now includes a built-in source pipeline so operators do not need to remember extra flags each run. The current default bundle includes:

- Frontier labs and research orgs: OpenAI, Anthropic, Mistral AI, Google AI Blog, Google Research Blog, Meta AI, Hugging Face, NVIDIA, AI2, Sakana AI, Together AI
- Researchers and widely read writers: Lilian Weng, Chip Huyen, Jay Alammar, Sebastian Raschka, Simon Willison, Hamel Husain, Ethan Mollick, Nathan Lambert, Latent Space, The Gradient, Jack Clark
- Reviewed arXiv recent feeds: `arxiv_cs_ai_recent`, `arxiv_cs_lg_recent`, `arxiv_cs_cl_recent`, `arxiv_cs_cv_recent`, `arxiv_cs_ro_recent`, `arxiv_cs_ne_recent`, `arxiv_stat_ml_recent`

Use the bundle aliases `frontier_labs`, `ai_people`, `arxiv_recent`, `preprint_sources`, `default_public_sources`, or `default_sources` if you want to target those groups directly.

## arXiv API Guardrails

This repo now includes a dedicated arXiv policy layer in `ppr/scrapers/arxiv.py`. The approved built-in arXiv feeds above are allowed; unreviewed `arxiv` / `arxiv_*` targets are still blocked before any request is sent. The arXiv integration follows these rules:

- Only reviewed built-in arXiv source IDs are allowed. Unknown `arxiv_*` targets are blocked before any request is sent.
- Use the dedicated `export.arxiv.org` API host rather than scraping `arxiv.org` pages directly.
- Keep arXiv API traffic to one request every 3 seconds and a single connection at a time.
- Keep query slices at `max_results <= 2000` and within the API's `start + max_results <= 30000` window.
- Cache approved arXiv responses on disk, so repeated crawls reuse recent results instead of repeatedly hitting the API.
- Treat arXiv as a metadata source first. Link users to arXiv abstract pages instead of mirroring PDFs or source files from this repo.
- Use the API for topic-scoped, near-real-time retrieval. Reserve OAI-PMH or other bulk mirrors for separate backfill jobs, not the default crawl path.

Primary references:

- arXiv API Terms of Use: https://info.arxiv.org/help/api/tou.html
- arXiv API User Manual: https://info.arxiv.org/help/api/user-manual.html
- arXiv Bulk Data Access: https://info.arxiv.org/help/bulk_data.html

## Literature Survey (Claude Code skill)

Use the `/survey` command in Claude Code to generate a grounded literature survey from the accepted papers in this repository. Requires enriched data (`papers_enriched.jsonl`) for the target conferences.

```
# Specify conferences and year range
/survey I'm exploring efficient inference methods for large language models,
like speculative decoding and early exit strategies. Search NLP and ML
conferences from 2023-2025.

# Let it ask you for scope
/survey What papers exist on 3D object detection from point clouds?

# Target specific venues
/survey Find related work on code generation with LLMs. Search ICSE, FSE,
ASE, ACL, and EMNLP from 2023-2025.
```

The survey searches through real accepted papers, ranks by citation count, identifies datasets and benchmarks, and highlights research gaps. Output is saved to `outputs/<topic>_<timestamp>.md`.
