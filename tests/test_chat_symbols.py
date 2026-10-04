"""Recognising a stock question, and which stock it is about.

The hard part is not the intent, it is the SYMBOL. English is full of
three-letter uppercase words: "RUN THE GAP SCENARIO ON NVDA" contains
RUN, THE, GAP and ON. A naive "any 1-5 capitals" rule analyses a stock
called RUN and reports it confidently.
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent import chat_symbols as CS                         # noqa: E402


class TestSymbolExtraction(unittest.TestCase):

    def test_a_plain_ticker(self):
        self.assertEqual(CS.extract_symbol("Analyze AAPL"), "AAPL")

    def test_a_dollar_prefixed_ticker_wins(self):
        # Unambiguous, so it beats anything else in the sentence.
        self.assertEqual(
            CS.extract_symbol("RUN a check on $nvda please"), "NVDA")

    def test_stopwords_that_look_like_tickers_are_skipped(self):
        """The defect this list exists to prevent."""
        for query, expected in (
                ("Run the gap-through-stop scenario on NVDA.", "NVDA"),
                ("WHY would we trade MSFT", "MSFT"),
                ("What are the signals for TSLA?", "TSLA"),
                ("IS AAPL a buy", "AAPL")):
            self.assertEqual(CS.extract_symbol(query), expected, query)

    def test_a_class_suffix_survives(self):
        self.assertEqual(CS.extract_symbol("analyze BRK.B"), "BRK.B")

    def test_no_symbol_returns_none_rather_than_guessing(self):
        # Analysing the wrong stock confidently is worse than asking.
        for query in ("what happened yesterday?", "analyze apple",
                      "how is the agent doing?", ""):
            self.assertIsNone(CS.extract_symbol(query), query)


class TestIntentClassification(unittest.TestCase):

    def test_the_seven_required_forms(self):
        cases = (
            ("Analyze AAPL", CS.ANALYZE, "AAPL"),
            ("Why would the agent trade NVDA?", CS.WHY_WOULD, "NVDA"),
            ("Why wouldn't it trade TSLA?", CS.WHY_WOULD_NOT, "TSLA"),
            ("What are the signals for MSFT?", CS.SIGNALS, "MSFT"),
            ("What does the prediction system say about AAPL?",
             CS.PREDICTION, "AAPL"),
            ("Run the gap-through-stop scenario on NVDA.",
             CS.SCENARIO, "NVDA"),
            ("Replay MSFT for six months.", CS.REPLAY, "MSFT"),
        )
        for query, intent, symbol in cases:
            got = CS.classify(query)
            self.assertEqual(got.intent, intent, query)
            self.assertEqual(got.symbol, symbol, query)

    def test_negation_is_tested_before_affirmation(self):
        """"why would" is a substring of "why wouldn't".

        Matching the affirmative first inverts the question, and the
        answer would be confidently backwards.
        """
        self.assertEqual(
            CS.classify("Why wouldn't it trade TSLA?").intent,
            CS.WHY_WOULD_NOT)
        self.assertEqual(
            CS.classify("Why would it trade TSLA?").intent,
            CS.WHY_WOULD)

    def test_the_scenario_name_is_extracted(self):
        got = CS.classify("run the gap-through-stop scenario on NVDA")
        self.assertEqual(got.scenario, "gap_through_stop")

    def test_an_unnamed_scenario_is_reported_not_guessed(self):
        got = CS.classify("run a scenario on NVDA")
        self.assertEqual(got.intent, CS.SCENARIO)
        self.assertIsNone(got.scenario)
        self.assertIn("no scenario named", got.note)

    def test_replay_months_are_parsed(self):
        self.assertEqual(
            CS.classify("Replay MSFT for six months.").months, 6)
        self.assertEqual(
            CS.classify("replay AAPL for 3 months").months, 3)
        self.assertEqual(
            CS.classify("replay AAPL for a year").months, 12)

    def test_a_dated_question_is_not_a_stock_question(self):
        # Otherwise "what happened yesterday" would be routed to an
        # analysis of a stock it never named.
        got = CS.classify("what happened yesterday?")
        self.assertIsNone(got.intent)
        self.assertFalse(got.actionable)

    def test_an_intent_without_a_symbol_is_not_actionable(self):
        got = CS.classify("analyze this stock")
        self.assertEqual(got.intent, CS.ANALYZE)
        self.assertIsNone(got.symbol)
        self.assertFalse(got.actionable)


class TestThereIsOnePipelineImplementation(unittest.TestCase):
    """Chat must route to the service, not recompute.

    A chat path with its own arithmetic is a second system, and the
    disagreement gets found by a user rather than a test.
    """

    def test_the_handler_extracted_a_single_pipeline_function(self):
        with open(os.path.join(REPO, "lambda-micro", "agent-api",
                               "handler.py")) as fh:
            src = fh.read()
        self.assertIn("def _pipeline_stages(symbol: str)", src)
        # Both the HTTP endpoint and the chat router must call it.
        self.assertGreaterEqual(src.count("_pipeline_stages("), 3)

    def test_chat_does_not_call_the_risk_engine_itself(self):
        # evaluate_risk appears inside _pipeline_stages only. A second
        # call site in the chat router would be a second decision.
        with open(os.path.join(REPO, "lambda-micro", "agent-api",
                               "handler.py")) as fh:
            src = fh.read()
        start = src.index("def _answer_symbol_question")
        end = src.index("\ndef handle_ask", start)
        body = src[start:end]
        self.assertNotIn("evaluate_risk(", body)
        self.assertNotIn("generate_hypothesis(", body)


if __name__ == "__main__":
    unittest.main()
