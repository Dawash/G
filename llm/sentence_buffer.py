"""
Sentence buffer for streaming LLM responses.

Takes a token iterator from a streaming LLM and yields complete, speakable
sentences. The first yield happens early (~6-8 words) to start TTS as fast
as possible. Subsequent yields happen on proper .!? boundaries.

Usage:
    for sentence in iter_sentences(ollama_token_stream):
        speak(sentence)   # starts playing before LLM finishes
"""

import re
from typing import Iterable, Generator

# Sentence-ending punctuation followed by whitespace
_SENT_END = re.compile(r'(?<=[.!?])\s+')
# Clause boundary — used as emergency exit when sentence runs very long
_CLAUSE_END = re.compile(r'(?<=[,;:])\s+')

# How many words to accumulate before forcing a first emit (no sentence end)
_FIRST_EMIT_WORDS = 7
# How many words max in buffer before forcing a split even mid-sentence
_MAX_BUFFER_WORDS = 40


def iter_sentences(token_stream: Iterable[str]) -> Generator[str, None, None]:
    """Buffer tokens into speakable sentences with low first-word latency.

    The first sentence is emitted after ~7 words (or first .!? boundary,
    whichever comes first) so TTS can start speaking immediately. All
    subsequent sentences are emitted on proper sentence boundaries.

    Code blocks (``` ... ```) are skipped entirely — not speakable.
    Markdown artifacts (***, ##, etc.) are stripped inline.

    Args:
        token_stream: Iterable of string tokens from a streaming LLM.

    Yields:
        str: Complete, speakable phrases suitable for TTS input.
    """
    buffer = ""
    first_emitted = False
    in_code_block = False

    for token in token_stream:
        # Code block fence detection
        if "```" in token:
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue  # Skip code block content — not speakable

        # Strip common markdown artifacts inline
        token = _strip_markdown(token)
        if not token:
            continue

        buffer += token

        # --- First emit: get TTS going as fast as possible ---
        if not first_emitted:
            # Emit on sentence boundary (highest quality split)
            m = _SENT_END.search(buffer)
            if m:
                sentence = buffer[:m.start() + 1].strip()
                if sentence:
                    yield sentence
                buffer = buffer[m.end():]
                first_emitted = True
                continue

            # Emit on word count threshold (no sentence end yet)
            word_count = len(buffer.split())
            if word_count >= _FIRST_EMIT_WORDS:
                # Prefer clause boundary for a cleaner audio cut
                cm = _CLAUSE_END.search(buffer)
                if cm:
                    phrase = buffer[:cm.start() + 1].strip()
                    if phrase:
                        yield phrase
                    buffer = buffer[cm.end():]
                else:
                    # Emit whole buffer — can't wait longer
                    yield buffer.strip()
                    buffer = ""
                first_emitted = True
            continue

        # --- Subsequent emits: full sentence boundaries ---
        m = _SENT_END.search(buffer)
        while m:
            sentence = buffer[:m.start() + 1].strip()
            if sentence:
                yield sentence
            buffer = buffer[m.end():]
            m = _SENT_END.search(buffer)

        # Safety: if buffer grows too long without a sentence end, force split
        if len(buffer.split()) >= _MAX_BUFFER_WORDS:
            cm = _CLAUSE_END.search(buffer)
            if cm:
                phrase = buffer[:cm.start() + 1].strip()
                if phrase:
                    yield phrase
                buffer = buffer[cm.end():]
            else:
                yield buffer.strip()
                buffer = ""

    # Flush remaining text
    remainder = buffer.strip()
    if remainder:
        yield remainder


_MARKDOWN_RE = re.compile(r'(\*{1,3}|_{1,3}|#{1,6}\s*|`[^`]*`|\[([^\]]*)\]\([^)]*\))')


def _strip_markdown(token: str) -> str:
    """Strip common markdown inline formatting from a token."""
    # Replace [text](url) with just text
    token = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', token)
    # Strip bold/italic markers, code spans, headers
    token = re.sub(r'\*{1,3}|_{1,3}|`', '', token)
    token = re.sub(r'^#{1,6}\s*', '', token)
    return token
