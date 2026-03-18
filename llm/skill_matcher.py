"""
Skill matching — check if a stored skill matches the user's request.

Extracted from: brain.py  Brain._check_skill_library()

Responsibility:
  - Query the SkillLibrary for matching skills (TF-IDF + regex triggers)
  - Filter out low-quality skills (quality_score < 50)
  - Gate on credential availability
  - Return the best match or None
"""

import logging

logger = logging.getLogger(__name__)


def check_skill_match(skill_lib, user_input, min_similarity=0.7):
    """Check if a stored skill matches the user's request (Voyager pattern).

    Args:
        skill_lib: A SkillLibrary instance (must not be None).
        user_input: The user's natural language request.
        min_similarity: Minimum TF-IDF similarity threshold (default 0.7).

    Returns:
        The matching skill dict if found (with 'name', 'similarity',
        'success_count', 'quality_score', 'tool_sequence'), or None.
    """
    if not skill_lib:
        return None

    try:
        matches = skill_lib.find_skill(user_input, min_similarity=min_similarity, limit=1)
        if matches:
            match = matches[0]
            # Skip low-quality skills — they contain vague/incomplete sequences
            q = match.get("quality_score", 0)
            if q > 0 and q < 50:
                logger.info(f"Skill {match['name']} skipped — quality too low ({q})")
                return None
            logger.info(f"Skill library match: {match['name']} "
                        f"(similarity={match['similarity']:.2f}, "
                        f"used {match['success_count']}x, q={q})")

            # Check if required credentials are available
            ok, missing = skill_lib.check_credentials(match["name"])
            if not ok:
                logger.info(f"Skill {match['name']} skipped — "
                            f"missing credentials: {missing}")
                return None  # Fall through to LLM

            return match
    except Exception as e:
        logger.debug(f"Skill library lookup failed: {e}")
    return None
