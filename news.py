"""
News briefing system via RSS feeds — no API key needed.

Features:
  - Multiple sources (Google News RSS, BBC, Reuters)
  - Category support (general, tech, sports, entertainment, science)
  - Natural language output for voice
  - Caching to avoid repeated fetches
  - Offline fallback from cache
"""

import logging
import os
import json
import re
import time
import xml.etree.ElementTree as ET
from html import unescape

import requests

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_cache.json")
CACHE_MAX_AGE = 3600  # 1 hour

# Google News RSS by category
GOOGLE_NEWS_FEEDS = {
    "general": "https://news.google.com/rss?hl=en&gl=US&ceid=US:en",
    "tech": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US&ceid=US:en",
    "sports": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US&ceid=US:en",
    "entertainment": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNREpxYW5RU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US&ceid=US:en",
    "science": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y1RjU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US&ceid=US:en",
    "business": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US&ceid=US:en",
    "health": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTRGdvSUwyMHZNR3QwTlRFU0FtVnVLQUFQAQ?hl=en&gl=US&ceid=US:en",
}

# Fallback feeds if Google News is unavailable
FALLBACK_FEEDS = {
    "general": [
        "https://feeds.bbci.co.uk/news/rss.xml",
        "http://rss.cnn.com/rss/edition.rss",
    ],
    "tech": [
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
    ],
}


def _clean_title(title):
    """Clean up RSS title text for voice output."""
    title = unescape(title)
    title = re.sub(r"\s*-\s*[A-Z][\w\s]+$", "", title)  # Remove source suffix
    title = title.strip()
    return title


def _parse_rss(xml_text, limit=10):
    """Parse RSS XML and extract headlines."""
    try:
        root = ET.fromstring(xml_text)
        items = root.findall(".//item")
        headlines = []

        for item in items[:limit]:
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                clean = _clean_title(title_el.text)
                if clean and len(clean) > 10:
                    headlines.append(clean)

        return headlines
    except ET.ParseError as e:
        logger.error(f"RSS parse error: {e}")
        return []


def _parse_rss_detailed(xml_text, limit=10):
    """Parse RSS XML and extract headlines with descriptions.

    Returns list of dicts: [{"title": ..., "description": ...}, ...]
    """
    try:
        root = ET.fromstring(xml_text)
        items = root.findall(".//item")
        results = []

        for item in items[:limit]:
            title_el = item.find("title")
            desc_el = item.find("description")
            if title_el is not None and title_el.text:
                title = _clean_title(title_el.text)
                if title and len(title) > 10:
                    desc = ""
                    if desc_el is not None and desc_el.text:
                        desc = unescape(desc_el.text)
                        # Strip HTML tags from description
                        desc = re.sub(r'<[^>]+>', '', desc).strip()
                        # Limit description length
                        if len(desc) > 300:
                            desc = desc[:300] + "..."
                    results.append({"title": title, "description": desc})

        return results
    except ET.ParseError as e:
        logger.error(f"RSS parse error (detailed): {e}")
        return []


def _fetch_feed(url, timeout=8):
    """Fetch an RSS feed."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text


# --- Cache management ---

def _load_cache():
    """Load cached news if fresh."""
    if not os.path.isfile(CACHE_FILE):
        return None
    try:
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age > CACHE_MAX_AGE:
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(data):
    """Save news to cache."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


# --- Public API ---

# Google News country codes for regional news
_COUNTRY_CODES = {
    "nepal": ("NP", "ne"), "india": ("IN", "en"), "usa": ("US", "en"),
    "united states": ("US", "en"), "uk": ("GB", "en"), "united kingdom": ("GB", "en"),
    "germany": ("DE", "en"), "france": ("FR", "en"), "japan": ("JP", "en"),
    "china": ("CN", "en"), "australia": ("AU", "en"), "canada": ("CA", "en"),
    "brazil": ("BR", "en"), "russia": ("RU", "en"), "south korea": ("KR", "en"),
    "spain": ("ES", "en"), "italy": ("IT", "en"), "mexico": ("MX", "en"),
}


def get_headlines(category="general", count=5, query=None, country=None):
    """
    Get top headlines for a category, with optional query or country filter.

    Args:
        category: "general", "tech", "sports", etc.
        count: Number of headlines to return.
        query: Search term (e.g., "Nepal", "AI", "climate change")
        country: Country name or code (e.g., "Nepal", "India", "US")

    Returns a list of headline strings.
    """
    cache_key = f"{category}:{query or ''}:{country or ''}"

    # Check cache first (only for non-query requests)
    if not query and not country:
        cache = _load_cache()
        if cache and category in cache:
            return cache[category][:count]
    else:
        cache = _load_cache() or {}

    headlines = []

    # Query-based search via Google News RSS search endpoint
    if query:
        from urllib.parse import quote
        search_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en&gl=US&ceid=US:en"
        # Apply country code if provided
        if country:
            country_lower = country.lower().strip()
            if country_lower in _COUNTRY_CODES:
                gl, hl = _COUNTRY_CODES[country_lower]
                search_url = f"https://news.google.com/rss/search?q={quote(query)}&hl={hl}&gl={gl}&ceid={gl}:{hl}"
        try:
            xml = _fetch_feed(search_url)
            headlines = _parse_rss(xml, limit=count * 2)
        except Exception as e:
            logger.warning(f"Google News search failed for '{query}': {e}")

    # Country-only filter (no query)
    elif country:
        country_lower = country.lower().strip()
        if country_lower in _COUNTRY_CODES:
            gl, hl = _COUNTRY_CODES[country_lower]
            country_url = f"https://news.google.com/rss?hl={hl}&gl={gl}&ceid={gl}:{hl}"
            try:
                xml = _fetch_feed(country_url)
                headlines = _parse_rss(xml, limit=count * 2)
            except Exception as e:
                logger.warning(f"Google News country feed failed for {country}: {e}")
        # Fallback: search for country name
        if not headlines:
            from urllib.parse import quote
            search_url = f"https://news.google.com/rss/search?q={quote(country)}&hl=en&gl=US&ceid=US:en"
            try:
                xml = _fetch_feed(search_url)
                headlines = _parse_rss(xml, limit=count * 2)
            except Exception as e:
                logger.warning(f"Google News search fallback failed for {country}: {e}")

    # Standard category feed (no query, no country)
    if not headlines:
        feed_url = GOOGLE_NEWS_FEEDS.get(category, GOOGLE_NEWS_FEEDS["general"])
        try:
            xml = _fetch_feed(feed_url)
            headlines = _parse_rss(xml, limit=count * 2)
        except Exception as e:
            logger.warning(f"Google News feed failed for {category}: {e}")

    # Fallback feeds
    if not headlines and category in FALLBACK_FEEDS:
        for url in FALLBACK_FEEDS[category]:
            try:
                xml = _fetch_feed(url)
                headlines = _parse_rss(xml, limit=count * 2)
                if headlines:
                    break
            except Exception:
                continue

    # Cache the results
    if headlines:
        cache = cache or {}
        cache[cache_key if (query or country) else category] = headlines
        _save_cache(cache)

    return headlines[:count]


def get_news_detailed(category="general", count=5):
    """Fetch news with titles AND descriptions for LLM summarization.

    Returns list of dicts: [{"title": ..., "description": ...}, ...]
    The description contains the actual article snippet, not just the headline.
    """
    feed_url = GOOGLE_NEWS_FEEDS.get(category, GOOGLE_NEWS_FEEDS["general"])
    try:
        xml = _fetch_feed(feed_url)
        articles = _parse_rss_detailed(xml, limit=count * 2)
        if articles:
            return articles[:count]
    except Exception as e:
        logger.warning(f"Detailed news fetch failed for {category}: {e}")

    # Fallback feeds
    if category in FALLBACK_FEEDS:
        for url in FALLBACK_FEEDS[category]:
            try:
                xml = _fetch_feed(url)
                articles = _parse_rss_detailed(xml, limit=count * 2)
                if articles:
                    return articles[:count]
            except Exception:
                continue

    return []


def get_briefing(category="general", count=5, query=None, country=None):
    """
    Get a voice-friendly news briefing.
    Returns a natural language string ready for TTS.
    """
    headlines = get_headlines(category, count, query=query, country=country)

    if not headlines:
        return "I couldn't fetch the news right now. Try again later."

    cat_label = category if category != "general" else "top"

    parts = [f"Here are today's {cat_label} headlines."]

    for i, headline in enumerate(headlines):
        if i == 0:
            parts.append(f"First, {headline}.")
        elif i == len(headlines) - 1:
            parts.append(f"And finally, {headline}.")
        else:
            parts.append(f"Next, {headline}.")

    return " ".join(parts)


def get_startup_briefing(count=3):
    """Short briefing for startup greeting (fewer headlines)."""
    headlines = get_headlines("general", count)
    if not headlines:
        return ""

    parts = ["In the news today:"]
    for h in headlines:
        parts.append(f"{h}.")

    return " ".join(parts)
