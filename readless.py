#!/usr/bin/env python3
"""Readless — a personal, self-hosted "read less, know more" digest.

Pulls new items from RSS feeds, Substacks and email newsletters, makes ONE
LLM call to cluster and summarize them, publishes the result to a GitHub
Pages site and emails it to you.

Designed to run for $0: GitHub Actions (scheduler + compute), the Gemini
free tier (LLM), GitHub Pages (hosting) and Gmail (newsletters in, digest out).
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import imaplib
import json
import logging
import os
import re
import smtplib
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import yaml
from bs4 import BeautifulSoup

log = logging.getLogger("readless")

MAX_ITEM_CHARS = 6000        # cap on cleaned text per item (keeps the LLM call cheap)
MAX_STATE_HASHES = 5000      # how many seen-item hashes state.json remembers
MAX_ARCHIVE_DIGESTS = 60     # daily JSON files kept on the Pages site
FETCH_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; Readless/1.0; +https://github.com/jackdengler/digest)"

DEFAULT_MODELS: dict[str, str] = {
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-sonnet-4-5",
    "ollama": "llama3.2",
}

STYLE_RULES: dict[str, str] = {
    "concise": "Each summary must be 1-2 tight sentences capturing the core point.",
    "detailed": ("Each summary must be 3-5 sentences covering the key facts, "
                 "numbers and names from the item."),
}

# Newsletter boilerplate: lines shorter than NOISE_MAX_LINE_LEN matching any of
# these are dropped. Long lines are kept so article prose that merely mentions
# e.g. a privacy policy survives.
NOISE_MAX_LINE_LEN = 200
NOISE_PATTERNS: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in (
    r"\bunsubscribe\b",
    r"view (this email |this |it )?(online|in (your |the )?browser)",
    r"\bsponsored by\b",
    r"\bsponsor(ed)? (content|post|message|section)\b",
    r"\b(update|manage|change) (your )?(email |subscription |notification )?preferences\b",
    r"\bemail preferences\b",
    r"you('re| are) receiving this (email|newsletter|message)",
    r"\bthis email was sent to\b",
    r"\bsent to [\w.+-]+@[\w.-]+\b",
    r"add (us|this address) to your (address book|contacts)",
    r"forward(ed)? (this email )?to a friend",
    r"\bprivacy policy\b",
    r"\bterms of (service|use)\b",
    r"if you no longer wish to receive",
    r"(copyright|©)\s*\d{4}",
    r"\ball rights reserved\b",
    r"powered by (substack|beehiiv|mailchimp|convertkit|ghost)",
    r"\bread in app\b",
    r"\bopen in (the )?app\b",
    r"\bshare this post\b",
    r"\bupgrade to paid\b",
    r"\bpledge your support\b",
    r"\brefer a friend\b",
    r"\bclick here to (read|view|see)\b",
    r"\bview this post on the web\b",
)]

DIVIDER_RE = re.compile(r"^[\s\-_=~*+.•·#─-╿—–…]{3,}$")

IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Shared palette for the email renderer (the site has its own copy inline).
INK, FAINT, ACCENT, LINE, BG = "#1f1d1a", "#8a857d", "#9a4b1f", "#e6e1d8", "#faf8f4"
SERIF = "Georgia, 'Times New Roman', serif"


@dataclass
class Item:
    """One piece of content pulled from any source."""

    id: str
    source: str
    source_type: str  # "rss" | "substack" | "email"
    title: str
    url: str
    text: str
    published: str  # ISO 8601


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        log.warning("config: %s not found, using built-in defaults", path)
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Drop newsletter noise lines and divider lines, then cap the length."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if DIVIDER_RE.match(line):
            continue
        if len(line) <= NOISE_MAX_LINE_LEN and any(p.search(line) for p in NOISE_PATTERNS):
            continue
        lines.append(line)
    return "\n".join(lines)[:MAX_ITEM_CHARS]


def clean_html(raw_html: str) -> str:
    """Strip scripts/styles/images and newsletter noise from an HTML fragment."""
    soup = BeautifulSoup(raw_html or "", "html.parser")
    for tag in soup(["script", "style", "img", "picture", "source", "head", "title",
                     "iframe", "svg", "noscript", "video", "audio", "form", "button"]):
        tag.decompose()
    return clean_text(soup.get_text("\n"))


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def item_hash(source: str, title: str, url: str) -> str:
    """Stable id / dedupe hash for an item, derived from (source|title|url)."""
    return hashlib.sha256(f"{source}|{title}|{url}".encode("utf-8")).hexdigest()[:16]


def substack_feed_url(ref: str) -> str:
    """Resolve "@handle", substack.com/@handle URLs or publication URLs to a feed URL."""
    ref = ref.strip()
    if ref.startswith("@"):
        return f"https://{ref.lstrip('@')}.substack.com/feed"
    match = re.search(r"substack\.com/@([A-Za-z0-9_.-]+)", ref)
    if match:
        return f"https://{match.group(1)}.substack.com/feed"
    url = ref if re.match(r"^https?://", ref) else f"https://{ref}"
    return url.rstrip("/") + "/feed"


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read()


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    return None


def parse_feed(content: bytes | str, source_name: str, source_type: str,
               cutoff: datetime) -> list[Item]:
    """Parse an RSS/Atom document and return only items newer than `cutoff`."""
    parsed = feedparser.parse(content)
    if parsed.get("bozo") and not parsed.entries:
        raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")
    feed_title = str(parsed.feed.get("title") or source_name).strip() or source_name
    items: list[Item] = []
    for entry in parsed.entries:
        published = _entry_datetime(entry)
        if published is None or published < cutoff:
            continue
        title = str(entry.get("title") or "(untitled)").strip()
        url = str(entry.get("link") or "").strip()
        if entry.get("content"):
            raw = entry.content[0].get("value", "")
        else:
            raw = entry.get("summary") or ""
        items.append(Item(
            id=item_hash(feed_title, title, url),
            source=feed_title,
            source_type=source_type,
            title=title,
            url=url,
            text=clean_html(raw),
            published=published.isoformat(timespec="seconds"),
        ))
    return items


def _decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:  # noqa: BLE001 - a malformed header must not kill the run
        return value.strip()


def _email_body(msg: Message) -> tuple[str, bool]:
    """Pick the best body part: prefer HTML, skip attachments. Returns (body, is_html)."""
    html_part: Message | None = None
    text_part: Message | None = None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.is_multipart():
            continue
        if "attachment" in str(part.get("Content-Disposition") or "").lower():
            continue
        content_type = part.get_content_type()
        if content_type == "text/html" and html_part is None:
            html_part = part
        elif content_type == "text/plain" and text_part is None:
            text_part = part
    part = html_part or text_part
    if part is None:
        return "", False
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace"), part is html_part
    except LookupError:
        return payload.decode("utf-8", errors="replace"), part is html_part


def fetch_email_items(cfg: dict[str, Any], cutoff: datetime) -> list[Item]:
    """Pull newsletters from an IMAP label. Never raises: warns and returns []."""
    email_cfg = cfg.get("email") or {}
    username = str(email_cfg.get("username") or "").strip()
    password = os.environ.get("IMAP_PASSWORD", "")
    if not username:
        log.info("email: no email.username configured, skipping email ingestion")
        return []
    if not password:
        log.warning("email: IMAP_PASSWORD not set, skipping email ingestion")
        return []
    host = str(email_cfg.get("imap_host") or "imap.gmail.com")
    label = str(email_cfg.get("label") or "Newsletters")
    mark_as_read = bool(email_cfg.get("mark_as_read", False))
    items: list[Item] = []
    try:
        with imaplib.IMAP4_SSL(host) as imap:
            imap.login(username, password)
            status, _ = imap.select(f'"{label}"', readonly=not mark_as_read)
            if status != "OK":
                log.warning("email: could not open label %r, skipping", label)
                return []
            since = f"{cutoff.day:02d}-{IMAP_MONTHS[cutoff.month - 1]}-{cutoff.year}"
            status, data = imap.search(None, f"(SINCE {since})")
            if status != "OK":
                log.warning("email: IMAP search failed, skipping")
                return []
            for num in (data[0].split() if data and data[0] else []):
                status, msg_data = imap.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = message_from_bytes(msg_data[0][1])
                try:
                    received = parsedate_to_datetime(msg.get("Date") or "")
                except (TypeError, ValueError):
                    received = None
                if received is not None and received.tzinfo is None:
                    received = received.replace(tzinfo=timezone.utc)
                if received is None or received < cutoff:
                    continue
                subject = _decode_mime_header(msg.get("Subject")) or "(no subject)"
                display_name, address = parseaddr(_decode_mime_header(msg.get("From")))
                sender = display_name or address or "Email newsletter"
                body, is_html = _email_body(msg)
                items.append(Item(
                    id=item_hash(sender, subject, ""),
                    source=sender,
                    source_type="email",
                    title=subject,
                    url="",
                    text=clean_html(body) if is_html else clean_text(body),
                    published=received.isoformat(timespec="seconds"),
                ))
                if mark_as_read:
                    imap.store(num, "+FLAGS", "\\Seen")
        log.info("email: %d newsletter(s) from label %r", len(items), label)
    except Exception as exc:  # noqa: BLE001 - email must never crash the digest
        log.warning("email: ingestion failed (%s), continuing without email items", exc)
    return items


def gather_items(cfg: dict[str, Any], cutoff: datetime) -> list[Item]:
    """Fetch every configured source. A failing source logs a warning and is skipped."""
    items: list[Item] = []
    for url in cfg.get("rss_feeds") or []:
        try:
            found = parse_feed(fetch_bytes(str(url)), source_name=str(url),
                               source_type="rss", cutoff=cutoff)
            items.extend(found)
            log.info("rss: %d new from %s", len(found), url)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not sink the run
            log.warning("rss: failed %s (%s)", url, exc)
    for ref in cfg.get("substacks") or []:
        feed_url = substack_feed_url(str(ref))
        try:
            found = parse_feed(fetch_bytes(feed_url), source_name=str(ref),
                               source_type="substack", cutoff=cutoff)
            items.extend(found)
            log.info("substack: %d new from %s", len(found), feed_url)
        except Exception as exc:  # noqa: BLE001
            log.warning("substack: failed %s (%s)", feed_url, exc)
    items.extend(fetch_email_items(cfg, cutoff))
    return items


# --------------------------------------------------------------------------
# Dedupe state
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and isinstance(state.get("seen"), list):
                return state
            log.warning("state: %s has an unexpected shape, starting fresh", path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("state: could not read %s (%s), starting fresh", path, exc)
    return {"seen": []}


def save_state(state: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(state), encoding="utf-8")


def filter_new_items(items: list[Item], seen: set[str]) -> list[Item]:
    """Drop items already digested in a previous run (or duplicated in this one)."""
    fresh: list[Item] = []
    seen_this_run: set[str] = set()
    for item in items:
        if item.id in seen or item.id in seen_this_run:
            continue
        seen_this_run.add(item.id)
        fresh.append(item)
    return fresh


def remember_items(state: dict[str, Any], items: list[Item]) -> None:
    """Append item hashes, keeping only the newest MAX_STATE_HASHES."""
    seen = list(state.get("seen") or []) + [item.id for item in items]
    state["seen"] = seen[-MAX_STATE_HASHES:]


# --------------------------------------------------------------------------
# Topic filters
# --------------------------------------------------------------------------

def matches_any_keyword(item: Item, keywords: list[Any]) -> bool:
    if not keywords:
        return False
    haystack = f"{item.title}\n{item.text}".lower()
    return any(str(k).lower() in haystack for k in keywords if k)


def apply_exclude_filters(items: list[Item], cfg: dict[str, Any]) -> tuple[list[Item], int]:
    """Return (kept items, count filtered by topic_filters.exclude)."""
    exclude = (cfg.get("topic_filters") or {}).get("exclude") or []
    kept: list[Item] = []
    filtered = 0
    for item in items:
        if matches_any_keyword(item, exclude):
            filtered += 1
            log.info("filtered by your rules: [%s] %s", item.source, item.title)
        else:
            kept.append(item)
    return kept, filtered


# --------------------------------------------------------------------------
# LLM layer (one call per run, pluggable provider)
# --------------------------------------------------------------------------

def build_prompt(items: list[Item], cfg: dict[str, Any]) -> str:
    style = str(cfg.get("summary_style") or "concise").lower()
    style_rule = STYLE_RULES.get(style, STYLE_RULES["concise"])
    prioritize = (cfg.get("topic_filters") or {}).get("prioritize") or []
    if prioritize:
        priority_rule = ('Set "priority": true when an item clearly concerns one of these '
                         "topics: " + ", ".join(str(t) for t in prioritize)
                         + ". Otherwise set it to false.")
    else:
        priority_rule = 'Always set "priority": false.'
    payload = json.dumps(
        [{"id": i.id, "source": i.source, "title": i.title, "url": i.url, "text": i.text}
         for i in items],
        ensure_ascii=False,
    )
    return f"""You are the summarization engine of "Readless", a personal daily news digest.
Below is a JSON array of items collected in the last day. Each item has: id, source, title, url, text.

Respond with ONLY a single JSON object -- no markdown, no code fences, no commentary -- in exactly this shape:
{{"hot_topics": [{{"topic": "short headline", "synthesis": "2-4 sentences", "item_ids": ["id1", "id2"]}}],
 "summaries": [{{"id": "item id", "summary": "...", "tags": ["tag1", "tag2"], "priority": false}}],
 "skipped_ids": ["id3"]}}

Rules:
1. hot_topics: include a topic ONLY when the same story or theme is covered by items from at least 2 DISTINCT sources. Synthesize the combined coverage into one insight (2-4 sentences) instead of repeating each item, and list the supporting item ids. If nothing qualifies, return an empty list.
2. summaries: one entry per worthwhile item. {style_rule} Base every statement strictly on the provided text -- never invent facts, numbers or names.
3. tags: 1-3 short lowercase topic words per item (e.g. "ai", "energy", "policy").
4. {priority_rule}
5. skipped_ids: ids of items that are pure advertisements, sponsor messages, event promos or other content-free marketing. When unsure, summarize instead of skipping.
6. Every input id must appear exactly once across summaries and skipped_ids.

ITEMS:
{payload}"""


def strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def parse_llm_json(raw: str) -> dict[str, Any]:
    """Parse the LLM reply, tolerating code fences and surrounding prose."""
    text = strip_code_fences(raw)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        result = json.loads(text[start:end + 1])
    if not isinstance(result, dict):
        raise json.JSONDecodeError("top-level JSON value is not an object", text, 0)
    return result


def llm_credentials_present(cfg: dict[str, Any]) -> bool:
    provider = str(cfg.get("provider") or "gemini").lower()
    if provider == "gemini":
        return bool(os.environ.get("GEMINI_API_KEY"))
    if provider == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return True  # ollama runs locally, no key needed


def call_llm(prompt: str, cfg: dict[str, Any]) -> str:
    provider = str(cfg.get("provider") or "gemini").lower()
    model = str(cfg.get("model") or "") or DEFAULT_MODELS.get(provider, "")
    if provider == "gemini":
        return _call_gemini(prompt, model)
    if provider == "anthropic":
        return _call_anthropic(prompt, model)
    if provider == "ollama":
        return _call_ollama(prompt, model, str(cfg.get("ollama_url") or "http://localhost:11434"))
    raise ValueError(f"unknown provider {provider!r} (expected gemini, anthropic or ollama)")


def _call_gemini(prompt: str, model: str) -> str:
    from google import genai  # lazy import so other providers don't need the package
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text or ""


def _call_anthropic(prompt: str, model: str) -> str:
    import anthropic  # lazy import

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


def _call_ollama(prompt: str, model: str, base_url: str) -> str:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/generate",
        data=json.dumps({"model": model, "prompt": prompt,
                         "stream": False, "format": "json"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode("utf-8")).get("response", "")


def summarize_items(items: list[Item], cfg: dict[str, Any]) -> dict[str, Any]:
    """One LLM call per run, plus one retry if the reply isn't valid JSON."""
    prompt = build_prompt(items, cfg)
    retry_suffix = ("\n\nREMINDER: your previous reply was not valid JSON. "
                    "Respond with VALID JSON only, exactly matching the schema.")
    last_error: Exception | None = None
    for attempt in (1, 2):
        raw = call_llm(prompt if attempt == 1 else prompt + retry_suffix, cfg)
        try:
            result = parse_llm_json(raw)
            result.setdefault("hot_topics", [])
            result.setdefault("summaries", [])
            result.setdefault("skipped_ids", [])
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            log.warning("LLM returned invalid JSON (attempt %d/2): %s", attempt, exc)
    raise RuntimeError(f"LLM did not return valid JSON after 2 attempts: {last_error}")


# --------------------------------------------------------------------------
# Digest assembly
# --------------------------------------------------------------------------

def _fallback_summary(item: Item) -> str:
    text = item.text.strip() or item.title
    return text[:280].rstrip() + ("…" if len(text) > 280 else "")


def build_digest(date_str: str, items: list[Item], llm_result: dict[str, Any],
                 filtered_by_rules: int, cfg: dict[str, Any]) -> dict[str, Any]:
    """Join the LLM output with item metadata into the canonical digest dict."""
    by_id = {item.id: item for item in items}
    prioritize = (cfg.get("topic_filters") or {}).get("prioritize") or []
    skipped = {str(i) for i in llm_result.get("skipped_ids") or [] if str(i) in by_id}

    entries: list[dict[str, Any]] = []
    covered: set[str] = set()
    for summary in llm_result.get("summaries") or []:
        item = by_id.get(str(summary.get("id")))
        if item is None or item.id in skipped or item.id in covered:
            continue
        covered.add(item.id)
        tags = [str(t).strip().lower() for t in (summary.get("tags") or []) if str(t).strip()][:3]
        entries.append({
            "id": item.id,
            "source": item.source,
            "source_type": item.source_type,
            "title": item.title,
            "url": item.url,
            "summary": str(summary.get("summary") or "").strip() or _fallback_summary(item),
            "tags": tags,
            "priority": bool(summary.get("priority")) or matches_any_keyword(item, prioritize),
        })
    for item in items:  # safety net: nothing silently disappears if the LLM forgets an id
        if item.id in covered or item.id in skipped:
            continue
        log.warning("LLM omitted item %r, using a fallback summary", item.title)
        entries.append({
            "id": item.id, "source": item.source, "source_type": item.source_type,
            "title": item.title, "url": item.url,
            "summary": _fallback_summary(item), "tags": [],
            "priority": matches_any_keyword(item, prioritize),
        })
    entries.sort(key=lambda e: (not e["priority"], e["source"].lower(), e["title"].lower()))

    valid_ids = {entry["id"] for entry in entries}
    hot_topics: list[dict[str, Any]] = []
    for topic in llm_result.get("hot_topics") or []:
        name = str(topic.get("topic") or "").strip()
        synthesis = str(topic.get("synthesis") or "").strip()
        if not name or not synthesis:
            continue
        hot_topics.append({
            "topic": name,
            "synthesis": synthesis,
            "item_ids": [str(i) for i in (topic.get("item_ids") or []) if str(i) in valid_ids],
        })

    return {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hot_topics": hot_topics,
        "items": entries,
        "counts": {
            "new_items": len(items) + filtered_by_rules,
            "summarized": len(entries),
            "skipped_promos": len(skipped),
            "filtered_by_rules": filtered_by_rules,
            "sources": len({entry["source"] for entry in entries}),
        },
    }


# --------------------------------------------------------------------------
# Output 1: GitHub Pages site files
# --------------------------------------------------------------------------

def write_site_files(digest: dict[str, Any], site_dir: Path,
                     publish_email_items: bool) -> Path:
    """Write site/digests/<date>.json, refresh index.json and prune the archive."""
    digests_dir = site_dir / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    published = dict(digest)
    if not publish_email_items:
        # The Pages site is public; keep email-newsletter content out of it.
        kept = [e for e in digest["items"] if e["source_type"] != "email"]
        omitted = len(digest["items"]) - len(kept)
        kept_ids = {e["id"] for e in kept}
        published["items"] = kept
        published["hot_topics"] = [
            {**topic, "item_ids": [i for i in topic["item_ids"] if i in kept_ids]}
            for topic in digest["hot_topics"]
            if not topic["item_ids"] or any(i in kept_ids for i in topic["item_ids"])
        ]
        published["counts"] = {**digest["counts"], "email_items_omitted": omitted}

    digest_path = digests_dir / f"{digest['date']}.json"
    digest_path.write_text(json.dumps(published, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    dates = sorted((p.stem for p in digests_dir.glob("????-??-??.json")), reverse=True)
    for stale in dates[MAX_ARCHIVE_DIGESTS:]:
        (digests_dir / f"{stale}.json").unlink(missing_ok=True)
    dates = dates[:MAX_ARCHIVE_DIGESTS]
    (digests_dir / "index.json").write_text(json.dumps({"digests": dates}, indent=2) + "\n",
                                            encoding="utf-8")
    return digest_path


# --------------------------------------------------------------------------
# Output 2: email
# --------------------------------------------------------------------------

def _pretty_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return date_str
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


def group_by_source(entries: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group entries by source; groups containing priority items come first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(entry["source"], []).append(entry)
    return sorted(groups.items(),
                  key=lambda kv: (not any(e["priority"] for e in kv[1]), kv[0].lower()))


def _counts_line(counts: dict[str, Any]) -> str:
    return (f"{counts.get('new_items', 0)} new items · "
            f"{counts.get('summarized', 0)} summarized · "
            f"{counts.get('skipped_promos', 0)} skipped as promos · "
            f"{counts.get('filtered_by_rules', 0)} filtered by your rules")


def render_email_html(digest: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Self-contained, email-client-safe HTML (inline CSS, tables, no JS)."""
    e = html.escape
    site_url = str(cfg.get("site_url") or "").strip()
    counts = digest.get("counts") or {}
    parts: list[str] = [
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Your Digest — {e(str(digest["date"]))}</title></head>'
        f'<body style="margin:0;padding:0;background-color:{BG};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:{BG};">'
        '<tr><td align="center" style="padding:28px 12px;">'
        '<table role="presentation" width="680" cellpadding="0" cellspacing="0" border="0" style="max-width:680px;width:100%;">'
        f'<tr><td style="font-family:{SERIF};color:{INK};">',
        '<h1 style="margin:0;font-size:30px;letter-spacing:-0.5px;">Your Digest</h1>',
        f'<p style="margin:4px 0 0;color:{FAINT};font-size:14px;">{e(_pretty_date(str(digest["date"])))}'
        f' — {counts.get("summarized", 0)} stories from {counts.get("sources", 0)} sources</p>',
    ]
    if site_url:
        parts.append(f'<p style="margin:8px 0 0;font-size:14px;"><a href="{e(site_url)}" '
                     f'style="color:{ACCENT};">Browse the archive on your digest site →</a></p>')
    parts.append(f'<hr style="border:none;border-top:3px double {LINE};margin:18px 0;">')

    items_by_id = {entry["id"]: entry for entry in digest.get("items") or []}
    hot_topics = digest.get("hot_topics") or []
    if hot_topics:
        parts.append(f'<h2 style="margin:0 0 10px;font-size:13px;letter-spacing:1.5px;'
                     f'text-transform:uppercase;color:{ACCENT};">Trending</h2>')
        for topic in hot_topics:
            sources: list[str] = []
            for item_id in topic.get("item_ids") or []:
                entry = items_by_id.get(item_id)
                if entry and entry["source"] not in sources:
                    sources.append(entry["source"])
            source_line = (f'<p style="margin:4px 0 0;color:{FAINT};font-size:12px;">'
                           f'{e(" · ".join(sources))}</p>') if sources else ""
            parts.append(
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 14px;">'
                f'<tr><td style="border-left:3px solid {ACCENT};padding:2px 0 2px 12px;'
                f'font-family:{SERIF};color:{INK};">'
                f'<p style="margin:0;font-size:17px;"><strong>{e(str(topic["topic"]))}</strong></p>'
                f'<p style="margin:4px 0 0;font-size:15px;line-height:1.5;">{e(str(topic["synthesis"]))}</p>'
                f'{source_line}</td></tr></table>')
        parts.append(f'<hr style="border:none;border-top:1px solid {LINE};margin:8px 0 18px;">')

    for source, group in group_by_source(digest.get("items") or []):
        parts.append(f'<h2 style="margin:24px 0 4px;font-size:13px;letter-spacing:1.5px;'
                     f'text-transform:uppercase;color:{ACCENT};border-bottom:1px solid {LINE};'
                     f'padding-bottom:4px;">{e(source)}</h2>')
        for entry in group:
            star = f'<span style="color:{ACCENT};">★</span> ' if entry.get("priority") else ""
            title = e(str(entry["title"]))
            if entry.get("url"):
                title_markup = (f'<a href="{e(str(entry["url"]))}" style="color:{INK};'
                                f'text-decoration:none;font-weight:bold;">{title}</a>')
            else:
                title_markup = f'<span style="font-weight:bold;">{title}</span>'
            parts.append(f'<p style="margin:14px 0 2px;font-size:17px;line-height:1.35;">'
                         f'{star}{title_markup}</p>')
            parts.append(f'<p style="margin:0;font-size:15px;line-height:1.5;color:#3a3833;">'
                         f'{e(str(entry["summary"]))}</p>')
            if entry.get("tags"):
                parts.append(f'<p style="margin:3px 0 0;font-size:12px;color:{FAINT};">'
                             f'{e(" · ".join(entry["tags"]))}</p>')

    parts.append(f'<hr style="border:none;border-top:3px double {LINE};margin:26px 0 10px;">')
    parts.append(f'<p style="margin:0;font-size:12px;color:{FAINT};">{e(_counts_line(counts))}</p>')
    footer_link = (f' · <a href="{e(site_url)}" style="color:{FAINT};">web version</a>'
                   if site_url else "")
    parts.append(f'<p style="margin:4px 0 0;font-size:12px;color:{FAINT};">'
                 f'Generated by Readless{footer_link}</p>')
    parts.append('</td></tr></table></td></tr></table></body></html>')
    return "".join(parts)


def render_email_text(digest: dict[str, Any], cfg: dict[str, Any]) -> str:
    """Plain-text alternative part for the email."""
    lines = [f"Your Digest — {digest['date']}", ""]
    for topic in digest.get("hot_topics") or []:
        lines.append(f"TRENDING: {topic['topic']}")
        lines.append(str(topic["synthesis"]))
        lines.append("")
    for source, group in group_by_source(digest.get("items") or []):
        lines.append(str(source).upper())
        for entry in group:
            star = "★ " if entry.get("priority") else ""
            suffix = f" ({entry['url']})" if entry.get("url") else ""
            lines.append(f"- {star}{entry['title']}{suffix}")
            lines.append(f"  {entry['summary']}")
        lines.append("")
    lines.append(_counts_line(digest.get("counts") or {}))
    site_url = str(cfg.get("site_url") or "").strip()
    if site_url:
        lines.append(f"Archive: {site_url}")
    return "\n".join(lines)


def send_email(digest: dict[str, Any], html_body: str, cfg: dict[str, Any]) -> bool:
    """Send the digest over SMTP. Missing password or a failure only warns."""
    smtp_cfg = cfg.get("smtp") or {}
    email_cfg = cfg.get("email") or {}
    username = str(smtp_cfg.get("username") or email_cfg.get("username") or "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    if not password:
        log.warning("smtp: SMTP_PASSWORD not set, skipping email delivery")
        return False
    if not username:
        log.warning("smtp: no smtp.username (or email.username) configured, "
                    "skipping email delivery")
        return False
    recipient = str(smtp_cfg.get("to") or "").strip() or username
    host = str(smtp_cfg.get("host") or "smtp.gmail.com")
    port = int(smtp_cfg.get("port") or 465)

    message = MIMEMultipart("alternative")
    message["Subject"] = f"Your Digest — {digest['date']}"
    message["From"] = username
    message["To"] = recipient
    message.attach(MIMEText(render_email_text(digest, cfg), "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
            smtp.login(username, password)
            smtp.sendmail(username, [recipient], message.as_string())
        log.info("smtp: digest emailed to %s", recipient)
        return True
    except Exception as exc:  # noqa: BLE001 - a mail hiccup must not lose the site update
        log.warning("smtp: sending failed (%s) -- the site was still updated", exc)
        return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="readless",
        description="Pull RSS/Substack/email newsletters, summarize them with one "
                    "LLM call, publish to GitHub Pages and email the digest.")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="build everything locally: no email, no state update, "
                             "no site files written")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = load_config(Path(args.config))
    state_path = Path("state.json")
    state = load_state(state_path)
    lookback_hours = int(cfg.get("lookback_hours") or 26)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    log.info("Collecting items newer than %s (%dh lookback)",
             cutoff.isoformat(timespec="minutes"), lookback_hours)

    items = gather_items(cfg, cutoff)
    new_items = filter_new_items(items, set(state.get("seen") or []))
    log.info("%d items fetched, %d new after dedupe", len(items), len(new_items))
    if not new_items:
        log.info("Nothing new since the last digest — enjoy the quiet.")
        return 0

    kept, filtered_by_rules = apply_exclude_filters(new_items, cfg)
    if not kept:
        log.info("All %d new items were filtered by your rules; no digest today.",
                 len(new_items))
        if not args.dry_run:
            remember_items(state, new_items)
            save_state(state, state_path)
        return 0

    if args.dry_run and not llm_credentials_present(cfg):
        log.warning("dry-run: no LLM credentials for provider %r, stopping before the LLM call",
                    str(cfg.get("provider") or "gemini"))
        for item in kept:
            log.info("  would summarize: [%s] %s", item.source, item.title)
        return 0

    try:
        llm_result = summarize_items(kept, cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("LLM call failed: %s", exc)
        return 1

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = build_digest(date_str, kept, llm_result, filtered_by_rules, cfg)
    email_html = render_email_html(digest, cfg)

    if args.dry_run:
        preview = Path(f"digest_{date_str}.html")
        preview.write_text(email_html, encoding="utf-8")
        log.info("dry-run: wrote %s (no email sent, no site files written, state untouched)",
                 preview)
        return 0

    site_path = write_site_files(digest, Path(cfg.get("site_dir") or "site"),
                                 bool(cfg.get("publish_email_items", False)))
    log.info("site: wrote %s", site_path)
    send_email(digest, email_html, cfg)
    remember_items(state, new_items)
    save_state(state, state_path)
    log.info("Done: %d summarized, %d hot topics, %d skipped as promos, %d filtered.",
             digest["counts"]["summarized"], len(digest["hot_topics"]),
             digest["counts"]["skipped_promos"], digest["counts"]["filtered_by_rules"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
