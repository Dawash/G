"""
Researcher Agent — Web research when stuck or needing information.

Upgrades from desktop_agent._research_when_stuck:
  - Targeted query generation based on specific failure
  - Multi-source search (DuckDuckGo + Wikipedia + web_read)
  - Summarizes findings into actionable fix suggestions
  - Stores findings for future retrieval via blackboard vector memory

Called by orchestrator when critic scores < SCORE_RESEARCH.
"""

import json
import logging
import re
import time

from .base import BaseAgent

logger = logging.getLogger(__name__)


class ResearcherAgent(BaseAgent):
    """Web research agent that finds solutions when executor is stuck."""

    name = "researcher"
    role = "Web researcher that finds solutions for failed tasks"

    def run(self, failed_step: str = "", error: str = "", goal: str = "", **kwargs) -> dict:
        """Research a solution for a failed step.

        Args:
            failed_step: Description of what failed.
            error: The error message.
            goal: The overall goal for context.

        Returns:
            {"status": "ok", "solution": str, "sources": list, "confidence": float}
        """
        goal = goal or self.bb.get("goal", "")
        self._log(f"Researching: {failed_step[:60]} (error: {error[:60]})")
        self.bb.set("phase", "researching")

        # --- Step 1: Check blackboard vector memory for similar past problems ---
        cached = self._check_cached_solutions(failed_step, error)
        if cached:
            self._log(f"Found cached solution (similarity={cached['similarity']:.2f})")
            return {
                "status": "ok",
                "solution": cached["text"],
                "sources": ["cached"],
                "confidence": cached["similarity"],
            }

        # --- Step 2: Generate targeted search queries ---
        queries = self._generate_queries(failed_step, error, goal)
        if not queries:
            queries = [f"{failed_step} {error}"]

        # --- Step 3: Multi-source search ---
        findings = []
        for query in queries[:3]:
            result = self._search(query)
            if result:
                findings.extend(result)

        if not findings:
            return {"status": "error", "solution": "", "sources": [], "confidence": 0.0}

        # --- Step 4: Synthesize into actionable solution ---
        solution = self._synthesize(failed_step, error, goal, findings)

        # --- Step 5: Store in vector memory for future retrieval ---
        doc_id = f"research_{int(time.time())}"
        self.bb.index_document(doc_id, f"{failed_step} {error} -> {solution}")

        # Post to blackboard
        self.bb.append("research_results", {
            "step": failed_step,
            "error": error,
            "solution": solution,
            "sources": [f.get("url", "unknown") for f in findings[:3]],
            "timestamp": time.time(),
        })

        self._post({"solution": solution, "query_count": len(queries)}, msg_type="result")

        return {
            "status": "ok",
            "solution": solution,
            "sources": [f.get("url", "") for f in findings[:3]],
            "confidence": 0.7,
        }

    def _check_cached_solutions(self, step: str, error: str) -> dict | None:
        """Check if we've solved a similar problem before."""
        results = self.bb.search_similar(f"{step} {error}", top_k=1)
        if results and results[0]["similarity"] > 0.6:
            return results[0]
        return None

    def _generate_queries(self, step: str, error: str, goal: str) -> list:
        """Generate targeted search queries for the specific failure."""
        prompt = (
            f"Generate 2-3 web search queries to solve this automation problem:\n\n"
            f"Task: {step}\n"
            f"Error: {error}\n"
            f"Goal: {goal}\n\n"
            f"Make queries specific and actionable. Focus on Windows automation.\n"
            f"Return JSON array: [\"query1\", \"query2\", \"query3\"]\n"
            f"JSON only."
        )

        raw = self._llm_call(prompt)
        try:
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                queries = json.loads(match.group())
                return [q for q in queries if isinstance(q, str) and len(q) > 5]
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: construct basic queries
        clean_error = re.sub(r'[^\w\s]', ' ', error)[:50].strip()
        return [
            f"windows automation {step}",
            f"{clean_error} fix solution",
        ]

    def _search(self, query: str) -> list:
        """Search the web for information."""
        findings = []
        try:
            from web_agent import web_search_extract
            result = web_search_extract(query, num_results=3)
            if result and len(result) > 10:
                findings.append({"query": query, "text": result[:500], "url": "search"})
        except Exception as e:
            logger.debug(f"Web search failed: {e}")

        return findings

    def _synthesize(self, step: str, error: str, goal: str, findings: list) -> str:
        """Synthesize research findings into an actionable solution."""
        findings_text = "\n".join(
            f"  - {f['text'][:200]}" for f in findings[:5]
        )

        prompt = (
            f"Based on web research, suggest a specific fix for this automation failure.\n\n"
            f"Failed step: {step}\n"
            f"Error: {error}\n"
            f"Goal: {goal}\n\n"
            f"Research findings:\n{findings_text}\n\n"
            f"Provide a SPECIFIC actionable fix in 1-3 sentences. "
            f"Include exact tool names and arguments if possible. "
            f"If the task is impossible with available tools, say so clearly."
        )

        solution = self._llm_call(prompt)
        return solution.strip() if solution else "No solution found from research."
