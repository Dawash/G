"""
Web Agent — agentic web control for G.

Gives the AI the ability to:
- Fetch and read web page content
- Extract specific information from websites
- Interact with web APIs
- Download files

This is what makes G truly agentic — it can gather real information
from the web instead of just opening a browser.
"""

import json
import logging
import os
import re
import time
import requests
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

# User agent for web requests
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/json",
}


def web_read(url):
    """
    Fetch a web page and extract readable text content.
    Returns the main text content, stripped of HTML.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "")

        # JSON response — return formatted
        if "application/json" in content_type:
            try:
                data = resp.json()
                return json.dumps(data, indent=2)[:3000]
            except json.JSONDecodeError:
                pass

        # HTML — strip tags and extract text
        text = resp.text
        # Remove script and style blocks
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        # Remove HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Clean up whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Limit length
        return text[:3000] if text else "Page loaded but no readable content found."

    except requests.Timeout:
        return "The page took too long to load."
    except requests.ConnectionError:
        return "Could not connect to the website."
    except Exception as e:
        logger.error(f"Web read error: {e}")
        return f"Error reading web page: {e}"


def web_search_extract(query, num_results=3):
    """
    Search the web and extract actual content from top results.
    Uses multiple sources for reliability.
    Returns a summary of findings.
    """
    # Try DuckDuckGo Instant Answer first
    result = _ddg_search(query, num_results)
    if result:
        return result

    # Try Wikipedia search
    result = _wiki_search(query)
    if result and result != "I couldn't find a quick answer for that.":
        return result

    # Try DuckDuckGo HTML search as last resort
    result = _ddg_html_search(query)
    if result:
        return result

    return "I couldn't find specific information about that. Try asking differently."


def _ddg_search(query, num_results=3):
    """DuckDuckGo Instant Answer API."""
    try:
        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()

        results = []

        abstract = data.get("AbstractText", "")
        if abstract:
            results.append(abstract)

        answer = data.get("Answer", "")
        if answer:
            results.append(answer)

        for topic in data.get("RelatedTopics", [])[:num_results]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append(topic["Text"])

        infobox = data.get("Infobox", {})
        if infobox and "content" in infobox:
            for item in infobox["content"][:5]:
                label = item.get("label", "")
                value = item.get("value", "")
                if label and value:
                    results.append(f"{label}: {value}")

        return "\n".join(results) if results else None

    except Exception as e:
        logger.debug(f"DDG search error: {e}")
        return None


def _wiki_search(query):
    """Search Wikipedia using the search API (more reliable than REST summary)."""
    try:
        url = (
            f"https://en.wikipedia.org/w/api.php?"
            f"action=query&list=search&srsearch={quote_plus(query)}"
            f"&format=json&utf8=1&srlimit=3"
        )
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        results = data.get("query", {}).get("search", [])

        if not results:
            return None

        # Get the top result's extract
        title = results[0].get("title", "")
        extract_url = (
            f"https://en.wikipedia.org/w/api.php?"
            f"action=query&titles={quote_plus(title)}"
            f"&prop=extracts&exintro=1&explaintext=1&format=json"
        )
        resp2 = requests.get(extract_url, headers=HEADERS, timeout=10)
        pages = resp2.json().get("query", {}).get("pages", {})

        for page_id, page in pages.items():
            extract = page.get("extract", "")
            if extract:
                return extract[:1500]

        return None

    except Exception as e:
        logger.debug(f"Wiki search error: {e}")
        return None


def _ddg_html_search(query):
    """Scrape DuckDuckGo HTML results as last resort."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = requests.get(url, headers=HEADERS, timeout=10)

        # Extract result snippets
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</[at]',
            resp.text, re.DOTALL
        )

        if snippets:
            # Clean HTML from snippets
            clean = []
            for s in snippets[:3]:
                text = re.sub(r'<[^>]+>', '', s).strip()
                if text:
                    clean.append(text[:500])
            return "\n".join(clean) if clean else None

        return None

    except Exception as e:
        logger.debug(f"DDG HTML search error: {e}")
        return None


def get_webpage_title(url):
    """Get the title of a web page."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        match = re.search(r'<title[^>]*>(.*?)</title>', resp.text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return "Untitled page"
    except Exception:
        return "Could not fetch page title"


# ===================================================================
# Deep Research — multi-step research with citations and source scoring
# ===================================================================

def deep_research(query, llm_fn=None, max_sources=6, max_follow_links=3, max_time=30):
    """Multi-step research with citation tracking and source scoring.

    Steps:
      1. Generate 2-4 search queries (LLM or heuristic)
      2. Search each (DuckDuckGo + Wikipedia) with URL tracking
      3. Score source quality (heuristic)
      4. Follow top links via web_read() for deeper content
      5. Return structured report with [1], [2] citations

    Args:
        query: The research question
        llm_fn: Optional LLM function for query generation (callable(prompt) -> str)
        max_sources: Max total sources to collect
        max_follow_links: Max links to follow for deeper content
        max_time: Total time budget in seconds (default 30). Stops collecting
                  sources and following links once exceeded.

    Returns:
        dict with keys: report (str), sources (list of {url, title, snippet, score})
    """
    # Step 1: Generate diverse search queries
    queries = _generate_research_queries(query, llm_fn)
    logger.info(f"Deep research: {len(queries)} queries for '{query[:60]}'")

    # Step 2: Search all queries with citation tracking
    start_time = time.time()
    all_sources = []
    seen_urls = set()
    for q in queries:
        if time.time() - start_time > max_time:
            logger.info(f"Deep research hit time budget ({max_time}s) during search, returning {len(all_sources)} sources")
            break
        results = _search_with_citations(q)
        for src in results:
            url = src.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_sources.append(src)
        if len(all_sources) >= max_sources:
            break

    # Step 3: Score source quality
    for src in all_sources:
        src["score"] = _score_relevance(src.get("snippet", ""), query, src.get("url", ""))

    # Sort by score descending
    all_sources.sort(key=lambda s: s.get("score", 0), reverse=True)
    all_sources = all_sources[:max_sources]

    # Step 4: Follow top links for deeper content
    followed = 0
    for src in all_sources:
        if followed >= max_follow_links:
            break
        if time.time() - start_time > max_time:
            logger.info(f"Deep research hit time budget ({max_time}s) during link following, followed {followed} links")
            break
        url = src.get("url", "")
        if not url or not url.startswith("http"):
            continue
        try:
            deep_content = web_read(url)
            if deep_content and len(deep_content) > 100:
                src["deep_content"] = deep_content[:1500]
                followed += 1
                logger.debug(f"Followed link: {url[:60]} ({len(deep_content)} chars)")
        except Exception as e:
            logger.debug(f"Failed to follow {url[:60]}: {e}")

    # Step 5: Build report with citations
    if not all_sources:
        return {
            "report": "I couldn't find relevant information about that.",
            "sources": [],
        }

    report_parts = []
    for i, src in enumerate(all_sources, 1):
        content = src.get("deep_content", src.get("snippet", ""))
        if content:
            # Truncate per-source content
            content = content[:600]
            title = src.get("title", "Source")
            report_parts.append(f"[{i}] {title}:\n{content}")

    report = "\n\n".join(report_parts)

    # Build source list for citation
    sources_list = []
    for i, src in enumerate(all_sources, 1):
        sources_list.append({
            "index": i,
            "url": src.get("url", ""),
            "title": src.get("title", ""),
            "snippet": src.get("snippet", "")[:200],
            "score": src.get("score", 0),
        })

    return {
        "report": report[:4000],
        "sources": sources_list,
    }


def _generate_research_queries(query, llm_fn=None):
    """Generate 2-4 diverse search queries from the user's question.

    Uses LLM if available, otherwise heuristic expansion.
    """
    queries = [query]

    if llm_fn:
        try:
            prompt = (
                f"Generate 2-3 different web search queries to thoroughly research: '{query}'\n"
                f"Make them diverse — different angles, specific sub-topics.\n"
                f"Output ONLY the queries, one per line, no numbering or bullets."
            )
            extra = llm_fn(prompt)
            if extra:
                for line in extra.strip().split("\n"):
                    line = line.strip().lstrip("0123456789.-) •")
                    if line and len(line) > 5 and line != query:
                        queries.append(line)
        except Exception as e:
            logger.debug(f"LLM query generation failed: {e}")

    # Heuristic fallback: add variations if we don't have enough
    if len(queries) < 3:
        # Add "what is" variant for definitional queries
        lower = query.lower()
        if not lower.startswith(("what", "how", "why", "who", "when")):
            queries.append(f"what is {query}")
        # Add comparison variant
        if "vs" in lower or "compare" in lower or "difference" in lower:
            queries.append(f"{query} comparison review")
        # Add "best" variant for recommendation queries
        if "best" in lower or "recommend" in lower:
            queries.append(f"{query} top rated review 2024")
        # Generic expansion
        if len(queries) < 3:
            queries.append(f"{query} explained overview")

    return queries[:4]


def _search_with_citations(query):
    """Search DuckDuckGo + Wikipedia and return results with URL tracking.

    Returns list of {url, title, snippet, source_type}.
    """
    results = []

    # DuckDuckGo Instant Answer API
    try:
        url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()

        abstract = data.get("AbstractText", "")
        abstract_url = data.get("AbstractURL", "")
        abstract_src = data.get("AbstractSource", "")
        if abstract and abstract_url:
            results.append({
                "url": abstract_url,
                "title": abstract_src or "DuckDuckGo",
                "snippet": abstract,
                "source_type": "ddg_abstract",
            })

        for topic in data.get("RelatedTopics", [])[:3]:
            if isinstance(topic, dict):
                text = topic.get("Text", "")
                first_url = topic.get("FirstURL", "")
                if text and first_url:
                    results.append({
                        "url": first_url,
                        "title": text[:60],
                        "snippet": text,
                        "source_type": "ddg_related",
                    })
    except Exception as e:
        logger.debug(f"DDG citation search error: {e}")

    # DuckDuckGo HTML search for real URLs
    html_results = _ddg_html_search_with_urls(query)
    results.extend(html_results)

    # Wikipedia
    try:
        wiki_result = _wiki_search(query)
        if wiki_result:
            title_match = re.search(r'^(.+?)[\.\n]', wiki_result)
            title = title_match.group(1)[:60] if title_match else "Wikipedia"
            results.append({
                "url": f"https://en.wikipedia.org/wiki/{quote_plus(query.replace(' ', '_'))}",
                "title": f"Wikipedia: {title}",
                "snippet": wiki_result[:500],
                "source_type": "wikipedia",
            })
    except Exception as e:
        logger.debug(f"Wiki citation search error: {e}")

    return results


def _ddg_html_search_with_urls(query):
    """Scrape DuckDuckGo HTML results with link extraction."""
    results = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = requests.get(url, headers=HEADERS, timeout=10)

        # Extract result blocks with URLs and snippets
        # Pattern: result__a has the link, result__snippet has the text
        links = re.findall(
            r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            resp.text, re.DOTALL
        )
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</[at]',
            resp.text, re.DOTALL
        )

        for i, (link, title_html) in enumerate(links[:4]):
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            snippet = ""
            if i < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[i][:2000]).strip()[:500]

            # DDG wraps URLs in a redirect — extract real URL
            real_url = link
            uddg_match = re.search(r'uddg=([^&]+)', link)
            if uddg_match:
                from urllib.parse import unquote
                real_url = unquote(uddg_match.group(1))

            if real_url and title:
                results.append({
                    "url": real_url,
                    "title": title[:80],
                    "snippet": snippet[:300],
                    "source_type": "ddg_html",
                })
    except Exception as e:
        logger.debug(f"DDG HTML URL search error: {e}")

    return results


def _score_relevance(content, query, url=""):
    """Heuristic quality scoring for a source (0-10).

    Scores based on keyword overlap, content length, source indicators,
    and domain authority.
    """
    if not content:
        return 0

    score = 0
    content_lower = content.lower()
    query_words = set(query.lower().split())
    # Remove common words
    query_words -= {"the", "a", "an", "is", "are", "was", "were", "what",
                    "how", "why", "who", "when", "where", "which", "do",
                    "does", "can", "should", "would", "about", "for", "in",
                    "on", "to", "of", "and", "or", "vs", "versus"}

    # Keyword overlap (0-4 points)
    if query_words:
        matches = sum(1 for w in query_words if w in content_lower)
        score += min(4, int(4 * matches / len(query_words)))

    # Content length (0-3 points) — longer = more detailed
    if len(content) > 500:
        score += 3
    elif len(content) > 200:
        score += 2
    elif len(content) > 50:
        score += 1

    # Quality indicators (0-3 points)
    quality_signals = ["according to", "study", "research", "data",
                       "percent", "million", "official", "report"]
    for signal in quality_signals:
        if signal in content_lower:
            score += 0.5

    # Domain authority bonus (0-1.5 points)
    url_lower = url.lower() if url else ""
    if "wikipedia.org" in url_lower:
        score += 1.5
    elif any(d in url_lower for d in [".gov", ".edu", ".org"]):
        score += 1.0
    elif any(d in url_lower for d in ["stackoverflow.com", "github.com", "arxiv.org"]):
        score += 0.5

    score = min(10, score)

    return round(score, 1)


# ===================================================================
# Research When Stuck — agent calls this when it can't solve a problem
# ===================================================================

def research_solution(goal, error_message, failed_approaches=None, llm_fn=None):
    """Research a solution online when the agent is stuck.

    Inspired by OpenHands/SWE-Agent: when stuck, search the web,
    GitHub, StackOverflow for solutions and synthesize a recovery plan.

    Args:
        goal: What the agent was trying to achieve
        error_message: The error or failure description
        failed_approaches: List of approaches already tried
        llm_fn: Optional LLM function for query generation and synthesis

    Returns:
        dict with keys:
            solution (str): Synthesized solution description
            steps (list): Concrete recovery steps
            sources (list): URLs of sources consulted
            confidence (float): 0.0-1.0 confidence in the solution
    """
    failed_approaches = failed_approaches or []
    logger.info(f"Researching solution for: {goal[:60]} | Error: {error_message[:60]}")

    # Step 1: Generate targeted search queries
    queries = _generate_stuck_queries(goal, error_message, failed_approaches, llm_fn)

    # Step 2: Search multiple sources in parallel (threaded)
    all_results = []
    seen_urls = set()
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _search_one(query):
        """Search a single query across multiple engines."""
        results = []
        # DuckDuckGo
        ddg = _ddg_search(query)
        if ddg:
            results.append({"query": query, "content": ddg[:500], "source": "ddg"})
        # DDG HTML with URLs
        html_results = _ddg_html_search_with_urls(query)
        for r in html_results[:2]:
            results.append({
                "query": query,
                "content": r.get("snippet", "")[:500],
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "source": "web",
            })
        # Wikipedia
        wiki = _wiki_search(query)
        if wiki:
            results.append({"query": query, "content": wiki[:500], "source": "wikipedia"})
        return results

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_search_one, q): q for q in queries[:4]}
        for future in as_completed(futures, timeout=20):
            try:
                results = future.result()
                for r in results:
                    url = r.get("url", "")
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)
                    all_results.append(r)
            except Exception as e:
                logger.debug(f"Search query failed: {e}")

    if not all_results:
        return {
            "solution": "Could not find relevant solutions online.",
            "steps": [],
            "sources": [],
            "confidence": 0.0,
        }

    # Step 3: Follow top links for deeper content
    top_urls = [r["url"] for r in all_results if r.get("url")][:3]
    deep_content = []
    for url in top_urls:
        try:
            content = web_read(url)
            if content and len(content) > 100:
                deep_content.append({"url": url, "content": content[:1000]})
        except Exception:
            pass

    # Step 4: Synthesize solution
    solution = _synthesize_solution(
        goal, error_message, failed_approaches,
        all_results, deep_content, llm_fn
    )

    return solution


def _generate_stuck_queries(goal, error, failed_approaches, llm_fn=None):
    """Generate search queries for when the agent is stuck."""
    queries = []

    if llm_fn:
        try:
            failed_str = "\n".join(f"- {a}" for a in failed_approaches[:5]) if failed_approaches else "none"
            prompt = (
                f"I'm an AI agent trying to: {goal}\n"
                f"I got this error: {error}\n"
                f"Failed approaches:\n{failed_str}\n\n"
                f"Generate 3 web search queries to find a solution. "
                f"Focus on practical how-to guides and code examples.\n"
                f"Output ONLY the queries, one per line."
            )
            resp = llm_fn(prompt)
            if resp:
                for line in resp.strip().split("\n"):
                    line = line.strip().lstrip("0123456789.-) •\"'")
                    if line and len(line) > 5:
                        queries.append(line)
        except Exception:
            pass

    # Heuristic fallback queries
    if len(queries) < 2:
        # Error-specific query
        clean_error = re.sub(r'[^\w\s]', '', error)[:50].strip()
        if clean_error:
            queries.append(f"how to fix {clean_error}")

        # Goal-specific query
        queries.append(f"how to {goal.lower()[:50]} Windows Python")

        # Tool-specific query (if a tool name is mentioned)
        tool_match = re.search(r'\b(click_at|type_text|open_app|press_key|search_in_app|'
                               r'run_terminal|manage_files|browser_action)\b', error)
        if tool_match:
            queries.append(f"Python {tool_match.group()} automation alternative approach")

        # StackOverflow specific
        queries.append(f"site:stackoverflow.com {goal.lower()[:40]}")

    return queries[:4]


def _synthesize_solution(goal, error, failed_approaches, search_results,
                         deep_content, llm_fn=None):
    """Synthesize a solution from search results."""
    sources = []
    for r in search_results:
        if r.get("url"):
            sources.append(r["url"])

    # Build context for synthesis
    context_parts = []
    for r in search_results[:5]:
        content = r.get("content", "")
        if content:
            context_parts.append(content[:300])
    for dc in deep_content[:2]:
        context_parts.append(dc["content"][:500])

    research_text = "\n---\n".join(context_parts)

    if llm_fn and research_text:
        try:
            failed_str = ", ".join(failed_approaches[:3]) if failed_approaches else "none"
            prompt = (
                f"I'm an AI agent stuck on this task: {goal}\n"
                f"Error: {error}\n"
                f"Already tried: {failed_str}\n\n"
                f"Web research found:\n{research_text[:2000]}\n\n"
                f"Based on the research, provide:\n"
                f"1. A brief solution description (1-2 sentences)\n"
                f"2. Concrete steps to try (numbered list, max 5 steps)\n"
                f"3. Confidence level (low/medium/high)\n"
                f"Format: SOLUTION: ...\nSTEPS:\n1. ...\n2. ...\nCONFIDENCE: ..."
            )
            resp = llm_fn(prompt)
            if resp:
                return _parse_synthesis(resp, sources)
        except Exception:
            pass

    # Heuristic fallback: extract actionable content
    steps = []
    for r in search_results[:3]:
        content = r.get("content", "")
        # Look for instruction-like sentences
        sentences = re.split(r'[.!]\s+', content)
        for s in sentences:
            if re.search(r'\b(try|use|run|open|click|type|install|download|set|configure)\b', s, re.I):
                clean = s.strip()[:150]
                if clean and clean not in steps:
                    steps.append(clean)
                    if len(steps) >= 4:
                        break
        if len(steps) >= 4:
            break

    return {
        "solution": f"Based on web research for '{goal[:40]}': try the approaches below.",
        "steps": steps[:5],
        "sources": sources[:5],
        "confidence": 0.4 if steps else 0.1,
    }


def _parse_synthesis(text, sources):
    """Parse LLM synthesis response into structured format."""
    solution = ""
    steps = []
    confidence = 0.5

    # Extract solution
    sol_match = re.search(r'SOLUTION:\s*(.+?)(?=STEPS:|$)', text, re.DOTALL | re.I)
    if sol_match:
        solution = sol_match.group(1).strip()

    # Extract steps
    steps_match = re.search(r'STEPS:\s*(.+?)(?=CONFIDENCE:|$)', text, re.DOTALL | re.I)
    if steps_match:
        step_text = steps_match.group(1)
        for line in step_text.strip().split("\n"):
            line = line.strip().lstrip("0123456789.-) •")
            if line and len(line) > 5:
                steps.append(line[:200])

    # Extract confidence
    conf_match = re.search(r'CONFIDENCE:\s*(low|medium|high)', text, re.I)
    if conf_match:
        conf_map = {"low": 0.3, "medium": 0.6, "high": 0.85}
        confidence = conf_map.get(conf_match.group(1).lower(), 0.5)

    if not solution:
        solution = text[:200]

    return {
        "solution": solution,
        "steps": steps[:5],
        "sources": sources[:5],
        "confidence": confidence,
    }


def download_file(url, save_dir=None):
    """Download a file from URL to the Downloads folder."""
    try:
        if not save_dir:
            save_dir = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")

        # Get filename from URL
        filename = url.split("/")[-1].split("?")[0]
        if not filename:
            filename = "download"

        filepath = os.path.join(save_dir, filename)

        resp = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        resp.raise_for_status()

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        return f"Downloaded {filename} ({size_mb:.1f}MB) to {save_dir}"

    except Exception as e:
        logger.error(f"Download error: {e}")
        return f"Download failed: {e}"


