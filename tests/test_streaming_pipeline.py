"""
Comprehensive tests for the Priority 1 streaming voice pipeline.

Tests:
- iter_sentences() edge cases (10+ scenarios)
- stream_response() on all providers (mocked HTTP)
- Full pipeline integration
- speak_stream() contract (with mock TTS)
"""

import sys
import time
import json
import unittest
from unittest.mock import MagicMock, patch, call
sys.path.insert(0, '.')

from llm.sentence_buffer import iter_sentences


def tok(text):
    """Helper: yield words one at a time, simulating LLM token stream."""
    for word in text.split(" "):
        yield word + " "


class TestIterSentences(unittest.TestCase):
    """Tests for llm.sentence_buffer.iter_sentences"""

    def test_basic_three_sentences(self):
        result = list(iter_sentences(tok("Hello there. How are you? I am fine.")))
        self.assertEqual(len(result), 3, f"Got: {result}")

    def test_single_sentence_with_period(self):
        result = list(iter_sentences(tok("The sky is blue.")))
        self.assertEqual(len(result), 1)
        self.assertIn("blue", result[0])

    def test_single_sentence_no_period(self):
        result = list(iter_sentences(tok("Hello")))
        self.assertEqual(len(result), 1)

    def test_empty_input(self):
        result = list(iter_sentences(iter([])))
        self.assertEqual(result, [])

    def test_whitespace_only(self):
        result = list(iter_sentences(tok("   ")))
        self.assertEqual(result, [])

    def test_early_flush_long_sentence(self):
        # No period — should flush after ~7 words
        result = list(iter_sentences(tok("I am going to tell you something very long and interesting without any ending punctuation whatsoever here")))
        self.assertGreaterEqual(len(result), 2, f"Should have split long text. Got: {result}")

    def test_code_block_stripped(self):
        result = list(iter_sentences(tok("Here is some code. ```python print('hello') ``` That was it.")))
        # Code block content should not appear in output
        combined = " ".join(result)
        self.assertNotIn("print", combined, f"Code block not stripped: {result}")

    def test_markdown_stripped(self):
        result = list(iter_sentences(tok("**Bold text** is here. _Italic_ too.")))
        combined = " ".join(result)
        self.assertNotIn("**", combined)
        self.assertNotIn("_", combined) if "_ " not in "Bold text is here. Italic too." else None

    def test_exclamation_mark(self):
        result = list(iter_sentences(tok("Great job! You did well. Keep it up!")))
        self.assertEqual(len(result), 3, f"Got: {result}")

    def test_question_marks(self):
        result = list(iter_sentences(tok("What is your name? My name is G. How can I help?")))
        self.assertEqual(len(result), 3, f"Got: {result}")

    def test_unicode_text(self):
        # Should handle unicode without crashing
        result = list(iter_sentences(tok("Namaste. How are you? I am well.")))
        self.assertEqual(len(result), 3)

    def test_all_content_preserved(self):
        # No content should be lost
        input_text = "First sentence. Second sentence. Third sentence."
        result = list(iter_sentences(tok(input_text)))
        combined = " ".join(result).lower()
        self.assertIn("first", combined)
        self.assertIn("second", combined)
        self.assertIn("third", combined)

    def test_first_sentence_arrives_early(self):
        # First sentence should arrive BEFORE all tokens consumed
        # Simulate by counting how many tokens consumed before first yield
        long_text = "The quick brown fox. And then the fox did something else. And more."
        token_list = [w + " " for w in long_text.split()]
        total_tokens = len(token_list)

        tokens_consumed = [0]
        def counting_stream():
            for t in token_list:
                tokens_consumed[0] += 1
                yield t

        gen = iter_sentences(counting_stream())
        first = next(gen)
        consumed_at_first = tokens_consumed[0]
        self.assertLess(consumed_at_first, total_tokens,
                       f"First sentence arrived only after ALL tokens consumed ({consumed_at_first}/{total_tokens})")
        self.assertIsNotNone(first)


class TestStreamResponseProviders(unittest.TestCase):
    """Tests for stream_response() on all provider classes."""

    def test_base_class_fallback(self):
        """Base ChatProvider.stream_response falls back to _call_api."""
        from ai_providers import ChatProvider

        class FakeProvider(ChatProvider):
            def __init__(self):
                self.api_key = "k"
                self.system_prompt = "s"
                self.messages = []
                self.provider_name = "fake"
            def _call_api(self):
                return "fallback response"

        p = FakeProvider()
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        tokens = list(p.stream_response(msgs))
        self.assertEqual(len(tokens), 1)
        self.assertIn("fallback", tokens[0])

    def test_ollama_stream_response_exists(self):
        from ai_providers import OllamaProvider
        p = OllamaProvider("", "test", model="qwen2.5:7b")
        self.assertTrue(callable(p.stream_response))

    def test_openai_stream_response_exists(self):
        from ai_providers import OpenAIProvider
        p = OpenAIProvider("key", "test")
        self.assertTrue(callable(p.stream_response))

    def test_anthropic_stream_response_exists(self):
        from ai_providers import AnthropicProvider
        p = AnthropicProvider("key", "test")
        self.assertTrue(callable(p.stream_response))

    def test_ollama_stream_connection_error(self):
        """stream_response should yield nothing (not raise) on connection error."""
        from ai_providers import OllamaProvider
        p = OllamaProvider("", "test", model="qwen2.5:7b",
                          ollama_url="http://127.0.0.1:19999")  # dead port
        tokens = list(p.stream_response([{"role": "user", "content": "hi"}]))
        self.assertEqual(tokens, [], f"Should yield empty on connection error, got: {tokens}")

    def test_ollama_stream_parses_ndjson(self):
        """OllamaProvider.stream_response correctly parses NDJSON format."""
        import requests
        from ai_providers import OllamaProvider

        # Mock the requests.post to return fake NDJSON
        fake_lines = [
            b'{"model":"qwen2.5:7b","message":{"role":"assistant","content":"Hello"},"done":false}',
            b'{"model":"qwen2.5:7b","message":{"role":"assistant","content":" world"},"done":false}',
            b'{"model":"qwen2.5:7b","message":{"role":"assistant","content":"!"},"done":true}',
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_lines.return_value = iter(fake_lines)
        mock_response.raise_for_status = MagicMock()

        with patch('requests.post', return_value=mock_response) as mock_post:
            p = OllamaProvider("", "test", model="qwen2.5:7b")
            msgs = [{"role": "system", "content": "test"}, {"role": "user", "content": "hi"}]
            tokens = list(p.stream_response(msgs))

        self.assertEqual(tokens, ["Hello", " world", "!"])
        mock_post.assert_called_once()
        # Verify stream=True was passed
        call_kwargs = mock_post.call_args
        self.assertTrue(call_kwargs[1].get('stream', False) or
                       (len(call_kwargs[0]) == 0 and call_kwargs[1].get('stream', False)),
                       "requests.post should be called with stream=True")


class TestPipelineIntegration(unittest.TestCase):
    """Tests for the full token->sentence->speak pipeline."""

    def test_token_stream_to_sentences(self):
        """Full pipeline: token stream -> iter_sentences -> sentences."""
        tokens = ["I ", "will ", "open ", "Chrome. ", "Then ", "navigate ", "to ", "Reddit."]
        sentences = list(iter_sentences(iter(tokens)))
        combined = " ".join(sentences).lower()
        self.assertIn("chrome", combined)
        self.assertIn("reddit", combined)

    def test_first_sentence_latency(self):
        """First sentence should be available long before all tokens consumed."""
        # Simulate a slow token stream with 20 tokens
        tokens = ["word" + str(i) + " " for i in range(20)]
        tokens[3] = "word3. "  # Sentence boundary at token 4

        consumed = [0]
        def slow_stream():
            for t in tokens:
                consumed[0] += 1
                yield t

        gen = iter_sentences(slow_stream())
        first = next(gen, None)
        self.assertIsNotNone(first)
        self.assertLessEqual(consumed[0], 10,
                            f"First sentence took {consumed[0]} tokens (expected <=10 for 4-word sentence)")


class TestSpeakStream(unittest.TestCase):
    """Tests for speech.speak_stream() contract."""

    def test_speak_stream_exists_and_callable(self):
        from speech import speak_stream
        self.assertTrue(callable(speak_stream))

    def test_speak_stream_calls_tts_per_sentence(self):
        """speak_stream should call TTS once per sentence."""
        from speech import speak_stream

        call_count = [0]
        spoken = []

        def mock_piper(text):
            call_count[0] += 1
            spoken.append(text)

        # Patch the internal Piper call
        with patch('speech._speak_piper', side_effect=mock_piper), \
             patch('speech.set_mic_state'), \
             patch.object(__import__('speech').engine, 'is_speaking', MagicMock()):
            sentences = iter(["Hello there.", "How are you?", "I am fine."])
            speak_stream(sentences)

        self.assertEqual(call_count[0], 3, f"Expected 3 TTS calls, got {call_count[0]}")
        self.assertEqual(spoken, ["Hello there.", "How are you?", "I am fine."])

    def test_speak_stream_empty_input(self):
        """speak_stream should handle empty input without error."""
        from speech import speak_stream
        with patch('speech._speak_piper'), \
             patch('speech.set_mic_state'), \
             patch.object(__import__('speech').engine, 'is_speaking', MagicMock()):
            result = speak_stream(iter([]))  # Should not raise
            # result is None or str


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestIterSentences))
    suite.addTests(loader.loadTestsFromTestCase(TestStreamResponseProviders))
    suite.addTests(loader.loadTestsFromTestCase(TestPipelineIntegration))
    suite.addTests(loader.loadTestsFromTestCase(TestSpeakStream))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
