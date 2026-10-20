"""Groq reports one request-level no_speech_prob; confident speech over music must not be dropped."""
import unittest
import pipeline


class GroqNoSpeechFilterTests(unittest.TestCase):
    def test_confident_speech_with_high_no_speech_is_kept(self):
        # job 714b23bfc9c0, 360-480s: 85 narrated segments, no_speech 0.926, avg_logprob -0.076
        self.assertFalse(pipeline.is_silence(0.926, -0.076))
        self.assertFalse(pipeline.is_silence(0.98, -0.18))

    def test_low_confidence_high_no_speech_is_still_silence(self):
        self.assertTrue(pipeline.is_silence(0.701, -0.5))
        self.assertTrue(pipeline.is_silence(0.701, -1.2))

    def test_confident_known_hallucination_is_still_silence(self):
        self.assertTrue(pipeline.is_silence(0.95, -0.05, "Thank you."))
        self.assertTrue(pipeline.is_silence(0.95, -0.05, "谢谢观看"))
        self.assertFalse(pipeline.is_silence(0.3, -0.05, "Thank you."))  # below limit: untouched

    def test_missing_logprob_keeps_legacy_threshold(self):
        self.assertTrue(pipeline.is_silence(0.701))
        self.assertTrue(pipeline.is_silence(0.95, None))
        self.assertTrue(pipeline.is_silence(0.95, "-0.1"))
        self.assertFalse(pipeline.is_silence(0.414))
        self.assertFalse(pipeline.is_silence(None, -0.1))


if __name__ == "__main__":
    unittest.main()
