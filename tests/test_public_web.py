from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from ppr.scrapers.public_web import (
    PUBLIC_WEB_SOURCES,
    _extract_article_urls,
    _parse_atom_entries,
    _parse_rss_items,
    _scrape_html_listing,
)


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>OpenAI News</title>
    <item>
      <title>Introducing a New Model</title>
      <link>https://openai.com/news/new-model</link>
      <description><![CDATA[<p>Technical details and release notes.</p>]]></description>
      <dc:creator>OpenAI</dc:creator>
      <pubDate>Fri, 18 Apr 2026 12:00:00 GMT</pubDate>
      <category>Research</category>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Chip Huyen</title>
  <entry>
    <title>Designing Better ML Systems</title>
    <link href="https://huyenchip.com/2026/04/18/designing-better-ml-systems.html" rel="alternate" />
    <id>tag:huyenchip.com,2026:1</id>
    <updated>2026-04-18T09:30:00Z</updated>
    <summary>&lt;p&gt;Notes on robust system design.&lt;/p&gt;</summary>
    <author>
      <name>Chip Huyen</name>
    </author>
    <category term="ml-systems" />
  </entry>
</feed>
"""

LISTING_FIXTURE = """
<html>
  <body>
    <a href="/blog/">Home</a>
    <a href="https://ai.meta.com/blog/introducing-muse-spark-msl/">Introducing Muse Spark</a>
    <a href="https://ai.meta.com/blog/introducing-muse-spark-msl/">Learn More</a>
    <a href="https://ai.meta.com/blog/?page=2">Next</a>
  </body>
</html>
"""

EMBEDDED_URL_FIXTURE = """
<html>
  <body>
    <script>
      {"items":["/news/voxtral-tts","/news/mistral-small-4","/news/opengraph-image-1o1w9k"]}
    </script>
  </body>
</html>
"""

ARTICLE_FIXTURE = """
<html>
  <head>
    <meta property="og:title" content="Introducing Muse Spark" />
    <meta property="og:description" content="A new personal superintelligence research update." />
    <meta property="article:published_time" content="2026-04-18T14:00:00Z" />
    <meta name="author" content="Meta AI" />
    <meta name="keywords" content="AI, research, models" />
  </head>
  <body></body>
</html>
"""


class TestParsePublicFeeds:
    def test_parse_rss_items_maps_metadata(self):
        source = PUBLIC_WEB_SOURCES["openai_newsroom"]
        root = ET.fromstring(RSS_FIXTURE)

        papers = _parse_rss_items(root, source, source.feed_url)

        assert len(papers) == 1
        paper = papers[0]
        assert paper.title == "Introducing a New Model"
        assert paper.authors == ["OpenAI"]
        assert paper.abstract == "Technical details and release notes."
        assert paper.publication_date == "Fri, 18 Apr 2026 12:00:00 GMT"
        assert paper.source_id == "openai_newsroom"
        assert paper.source_name == "OpenAI Newsroom"
        assert paper.source_type == "frontier_lab"
        assert paper.external_ids == {"feed_url": "https://openai.com/news/rss.xml"}

    def test_parse_atom_entries_maps_metadata(self):
        source = PUBLIC_WEB_SOURCES["chip_huyen"]
        root = ET.fromstring(ATOM_FIXTURE)

        papers = _parse_atom_entries(root, source, source.feed_url)

        assert len(papers) == 1
        paper = papers[0]
        assert paper.title == "Designing Better ML Systems"
        assert paper.link == "https://huyenchip.com/2026/04/18/designing-better-ml-systems.html"
        assert paper.authors == ["Chip Huyen"]
        assert paper.keywords == ["ml-systems"]
        assert paper.abstract == "Notes on robust system design."
        assert paper.source_category == "researcher_blog"


class TestHtmlFallback:
    def test_extract_article_urls_skips_listing_navigation(self):
        source = PUBLIC_WEB_SOURCES["meta_ai_blog"]

        urls = _extract_article_urls(source, LISTING_FIXTURE)

        assert urls == ["https://ai.meta.com/blog/introducing-muse-spark-msl"]

    def test_extract_article_urls_uses_embedded_url_fallback(self):
        source = PUBLIC_WEB_SOURCES["mistral_news"]

        urls = _extract_article_urls(source, EMBEDDED_URL_FIXTURE)

        assert urls == [
            "https://mistral.ai/news/voxtral-tts",
            "https://mistral.ai/news/mistral-small-4",
        ]

    @patch("ppr.scrapers.public_web.requests.get")
    def test_scrape_html_listing_uses_article_metadata(self, mock_get):
        source = PUBLIC_WEB_SOURCES["meta_ai_blog"]

        listing_response = MagicMock()
        listing_response.text = LISTING_FIXTURE
        listing_response.headers = {"content-type": "text/html"}
        listing_response.raise_for_status = MagicMock()

        article_response = MagicMock()
        article_response.text = ARTICLE_FIXTURE
        article_response.headers = {"content-type": "text/html"}
        article_response.raise_for_status = MagicMock()

        mock_get.side_effect = [listing_response, article_response]

        papers = _scrape_html_listing(source)

        assert len(papers) == 1
        paper = papers[0]
        assert paper.title == "Introducing Muse Spark"
        assert paper.authors == ["Meta AI"]
        assert paper.publication_date == "2026-04-18T14:00:00Z"
        assert paper.keywords == ["AI", "research", "models"]
        assert paper.source_id == "meta_ai_blog"
