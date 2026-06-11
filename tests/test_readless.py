"""Offline tests for readless.py — no network, no LLM, no IMAP."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import readless  # noqa: E402
from readless import Item  # noqa: E402


# --------------------------------------------------------------------------
# Substack reference -> feed URL (all three accepted forms)
# --------------------------------------------------------------------------

def test_substack_handle() -> None:
    assert readless.substack_feed_url("@jackclark") == "https://jackclark.substack.com/feed"


def test_substack_profile_url() -> None:
    assert (readless.substack_feed_url("https://substack.com/@oneusefulthing")
            == "https://oneusefulthing.substack.com/feed")
    assert readless.substack_feed_url("substack.com/@thefp") == "https://thefp.substack.com/feed"


def test_substack_publication_url() -> None:
    assert (readless.substack_feed_url("https://www.noahpinion.blog/")
            == "https://www.noahpinion.blog/feed")
    assert (readless.substack_feed_url("https://www.slowboring.com")
            == "https://www.slowboring.com/feed")


# --------------------------------------------------------------------------
# Cleaner: removes scripts/styles/images and newsletter noise, keeps content
# --------------------------------------------------------------------------

NEWSLETTER_HTML = """
<html><head><style>p { color: red; }</style></head><body>
<script>trackEverything("pixel");</script>
<img src="https://example.com/tracking-pixel.gif">
<p>OpenAI-style labs shipped three new coding models this week.</p>
<p>Grid operators say interconnection queues are now measured in years.</p>
<p>--------------------------------------------------</p>
<p>Sponsored by MegaCorp — try our productivity suite today!</p>
<p>Unsubscribe from this list</p>
<p>View this email in your browser</p>
<p>Update your email preferences | Privacy Policy</p>
</body></html>
"""


def test_cleaner_removes_noise_but_keeps_content() -> None:
    cleaned = readless.clean_html(NEWSLETTER_HTML)
    assert "three new coding models" in cleaned
    assert "measured in years" in cleaned
    for noise in ("trackEverything", "tracking-pixel", "color: red", "Sponsored by",
                  "Unsubscribe", "in your browser", "email preferences", "-----"):
        assert noise not in cleaned, f"noise survived cleaning: {noise!r}"


def test_cleaner_caps_length() -> None:
    assert len(readless.clean_text("word " * 5000)) <= readless.MAX_ITEM_CHARS


# --------------------------------------------------------------------------
# Feed parsing from an in-memory RSS string, respecting the lookback cutoff
# --------------------------------------------------------------------------

def _rss_doc(now: datetime) -> str:
    fresh = format_datetime(now - timedelta(hours=2))
    stale = format_datetime(now - timedelta(days=10))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>Test Feed</title><link>https://example.com</link>
<item>
  <title>Fresh story</title>
  <link>https://example.com/fresh</link>
  <pubDate>{fresh}</pubDate>
  <description><![CDATA[<p>Something genuinely new happened.</p><script>bad()</script>]]></description>
</item>
<item>
  <title>Stale story</title>
  <link>https://example.com/stale</link>
  <pubDate>{stale}</pubDate>
  <description>Old news nobody needs.</description>
</item>
</channel></rss>"""


def test_parse_feed_from_string_respects_lookback() -> None:
    now = datetime.now(timezone.utc)
    items = readless.parse_feed(_rss_doc(now), "fallback name", "rss",
                                cutoff=now - timedelta(hours=26))
    assert len(items) == 1
    item = items[0]
    assert item.title == "Fresh story"
    assert item.url == "https://example.com/fresh"
    assert item.source == "Test Feed"  # feed <title> wins over the fallback name
    assert item.source_type == "rss"
    assert "genuinely new" in item.text
    assert "bad()" not in item.text


# --------------------------------------------------------------------------
# Shared fixtures: items + a mocked LLM response
# --------------------------------------------------------------------------

def _sample_items() -> list[Item]:
    ts = "2026-06-11T08:00:00+00:00"
    return [
        Item(id="aaa111", source="Ars Technica", source_type="rss",
             title="New chip export rules announced", url="https://example.com/ars-chips",
             text="Regulators announced new chip export rules.", published=ts),
        Item(id="bbb222", source="The Verge", source_type="rss",
             title="Industry reacts to chip export rules", url="https://example.com/verge-chips",
             text="Chipmakers responded to the new export rules.", published=ts),
        Item(id="ccc333", source="TLDR", source_type="email",
             title="TLDR Daily Update", url="",
             text="A roundup of everything in tech today.", published=ts),
    ]


LLM_RESULT = {
    "hot_topics": [
        {"topic": "Chip export crackdown",
         "synthesis": ("Two outlets covered the new export rules. "
                       "Both report enforcement starts next quarter."),
         "item_ids": ["aaa111", "bbb222"]},
        {"topic": "Newsletter-only exclusive",
         "synthesis": "Only the email newsletter mentioned this story. It still matters.",
         "item_ids": ["ccc333"]},
    ],
    "summaries": [
        {"id": "aaa111", "summary": "Regulators unveiled stricter chip export rules.",
         "tags": ["chips", "policy"], "priority": True},
        {"id": "bbb222", "summary": "Chipmakers warned the rules could slow shipments.",
         "tags": ["chips"], "priority": False},
        {"id": "ccc333", "summary": "A quick roundup of today's tech headlines.",
         "tags": ["roundup"], "priority": False},
    ],
    "skipped_ids": [],
}


def _sample_digest() -> dict:
    return readless.build_digest(
        "2026-06-11", _sample_items(), LLM_RESULT, filtered_by_rules=2,
        cfg={"site_url": "https://example.github.io/digest/"})


# --------------------------------------------------------------------------
# Site JSON writer respects publish_email_items
# --------------------------------------------------------------------------

def test_site_writer_excludes_email_items_by_default(tmp_path: Path) -> None:
    path = readless.write_site_files(_sample_digest(), tmp_path / "site",
                                     publish_email_items=False)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["id"] for e in data["items"]] == ["aaa111", "bbb222"]  # priority item first
    assert all(e["source_type"] != "email" for e in data["items"])
    assert data["counts"]["email_items_omitted"] == 1
    topics = [t["topic"] for t in data["hot_topics"]]
    assert "Chip export crackdown" in topics
    assert "Newsletter-only exclusive" not in topics  # email-only hot topic dropped
    index = json.loads((tmp_path / "site/digests/index.json").read_text(encoding="utf-8"))
    assert index["digests"] == ["2026-06-11"]


def test_site_writer_includes_email_items_when_enabled(tmp_path: Path) -> None:
    path = readless.write_site_files(_sample_digest(), tmp_path / "site",
                                     publish_email_items=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "ccc333" in [e["id"] for e in data["items"]]
    assert "email_items_omitted" not in data["counts"]


# --------------------------------------------------------------------------
# Email HTML renderer: trending, stars, filtered counts, no scripts
# --------------------------------------------------------------------------

def test_email_renderer_handles_mocked_llm_response() -> None:
    cfg = {"site_url": "https://example.github.io/digest/"}
    html_out = readless.render_email_html(_sample_digest(), cfg)
    assert "Your Digest" in html_out
    assert "Chip export crackdown" in html_out                          # trending topic
    assert "Two outlets covered the new export rules." in html_out      # synthesis
    assert "★" in html_out                                              # priority star
    assert "2 filtered by your rules" in html_out                       # footer counts
    assert "5 new items" in html_out                                    # 3 items + 2 filtered
    assert "Regulators unveiled stricter chip export rules." in html_out
    assert "https://example.com/ars-chips" in html_out
    assert "https://example.github.io/digest/" in html_out              # Pages site link
    assert "TLDR Daily Update" in html_out                              # email items stay in email
    assert "<script" not in html_out.lower()


# --------------------------------------------------------------------------
# Dedupe: seen items are excluded, state is capped
# --------------------------------------------------------------------------

def test_dedupe_excludes_seen_items() -> None:
    fresh = readless.filter_new_items(_sample_items(), seen={"aaa111"})
    assert [i.id for i in fresh] == ["bbb222", "ccc333"]


def test_dedupe_within_single_run() -> None:
    fresh = readless.filter_new_items(_sample_items() + _sample_items(), seen=set())
    assert len(fresh) == 3


def test_state_keeps_last_5000_hashes() -> None:
    state = {"seen": [f"hash{i:05d}" for i in range(readless.MAX_STATE_HASHES)]}
    readless.remember_items(state, _sample_items())
    assert len(state["seen"]) == readless.MAX_STATE_HASHES
    assert state["seen"][-1] == "ccc333"     # newest kept
    assert state["seen"][0] == "hash00003"   # oldest three dropped


# --------------------------------------------------------------------------
# LLM JSON parsing tolerates fences and prose
# --------------------------------------------------------------------------

def test_parse_llm_json_strips_code_fences() -> None:
    raw = '```json\n{"hot_topics": [], "summaries": [], "skipped_ids": []}\n```'
    assert readless.parse_llm_json(raw)["summaries"] == []


def test_parse_llm_json_tolerates_surrounding_prose() -> None:
    raw = ('Sure! Here is the JSON:\n'
           '{"hot_topics": [], "summaries": [], "skipped_ids": ["x"]}\nHope that helps.')
    assert readless.parse_llm_json(raw)["skipped_ids"] == ["x"]


# --------------------------------------------------------------------------
# The shipped config.yaml stays loadable (the settings page rewrites it)
# --------------------------------------------------------------------------

def test_shipped_config_yaml_is_valid() -> None:
    import yaml

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(cfg["rss_feeds"], list) and cfg["rss_feeds"]
    assert isinstance(cfg["substacks"], list) and cfg["substacks"]
    for ref in cfg["substacks"]:
        assert readless.substack_feed_url(str(ref)).endswith("/feed")
    assert cfg["provider"] in ("gemini", "anthropic", "ollama")
    assert isinstance(cfg["email"], dict) and isinstance(cfg["smtp"], dict)
    assert isinstance(cfg["topic_filters"]["exclude"], list)
