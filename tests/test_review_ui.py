"""
UI tests for the analysis review build (web/review/index.html).

These run the *shipped* rendering code rather than a copy of it: the page's
<script> block is extracted and executed under node with a minimal DOM stub,
then the render functions are called with structured fixtures and the
resulting HTML is asserted on.

That matters because the whole point of this UX work is what the page says.
A test that re-implemented the wording would pass while the page said
something else.

Requires node; skips cleanly when node is unavailable.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_HTML = os.path.join(REPO_ROOT, "web", "review", "index.html")

NODE = shutil.which("node")

# Enough of a DOM for the module body to evaluate. The render functions
# themselves are pure string builders, so nothing more is needed.
DOM_STUB = """
const __noop = () => {};
globalThis.document = {
  addEventListener: __noop,
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  documentElement: { setAttribute: __noop, getAttribute: () => "dark" },
};
globalThis.localStorage = {
  _v: {},
  getItem(k) { return this._v[k] ?? null; },
  setItem(k, v) { this._v[k] = v; },
};
globalThis.window = globalThis;
globalThis.fetch = () => { throw new Error("network disabled in tests"); };
"""


def extract_script() -> str:
    with open(REVIEW_HTML, encoding="utf-8") as fh:
        html = fh.read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    if not blocks:
        raise AssertionError("no <script> block found in web/review/index.html")
    return max(blocks, key=len)


def run_js(body: str) -> str:
    script = DOM_STUB + "\n" + extract_script() + "\n" + body
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise AssertionError(f"node failed: {out.stderr.strip()}")
    return out.stdout


# --------------------------------------------------------------------------
# Fixtures mirror the /agent/analysis contract (agent/analysis.py).
# --------------------------------------------------------------------------

def _group(key, label, direction, summary, members, mixed=False):
    return {
        "key": key, "label": label, "direction": direction,
        "description": f"{label} description", "summary": summary,
        "internal_disagreement": mixed, "members": members,
    }


BUY_FIXTURE = {
    "symbol": "NVDA", "price": 230.60, "change": 3.39, "change_percent": "1.4900%",
    "recommendation": "BUY", "signal_strength": "Strong", "signal_agreement": 0.72,
    "agreement_meaning": "How much the independent groups agree.",
    "analysis_available": True, "risk_level": "MEDIUM",
    "agreement_summary": {
        "buy_groups": 2, "sell_groups": 0, "neutral_or_silent": 1,
        "verdict": "Independent groups agree",
        "raw_signals_fired": 4, "groups_with_opinion": 2, "groups_total": 3,
    },
    "groups": [
        _group("trend", "Trend", "BUY", "Supports upward direction",
               [{"label": "MA crossover (20/50)", "direction": "BUY"}]),
        _group("momentum", "Momentum", "BUY", "Supports upward direction",
               [{"label": "MACD", "direction": "BUY"}]),
        _group("mean_reversion", "Mean Reversion", "NO_SIGNAL",
               "No signal from this group", []),
    ],
    "explanation": {
        "headline": "Why BUY?",
        "lines": ["Trend and Momentum independently support the same direction."],
    },
    "indicators": {
        "macd": {"available": True, "macd_line": 2.99, "signal_line": 2.34,
                 "histogram": 0.65, "relation": "MACD > Signal",
                 "crossover": "bullish",
                 "interpretation": "Bullish crossover - MACD above its signal line",
                 "note": "The signal line is a 9-period EMA of the MACD line."},
        "rsi": {"available": True, "value": 65.9, "zone": "leaning",
                "engine_vote": "none", "reading": "Moderately strong",
                "note": "The engine only votes on RSI below 30 or above 70."},
        "moving_averages": {"available": True, "price": 230.60, "sma_20": 223.31,
                            "sma_50": 217.45, "structure": "Price > SMA 20 > SMA 50",
                            "reading": "Bullish structure"},
        "bollinger": {"available": True, "upper": 234.93, "middle": 223.31,
                      "lower": 211.69, "price": 230.60, "position_pct": 81.0,
                      "where": "Near upper band", "engine_vote": "none",
                      "note": "Being near a band is not itself a signal."},
        "momentum_10d": 5.25, "volatility_pct": 2.52,
    },
    "data_quality": {"freshness": "FRESH", "price_source": "alpaca",
                     "sources": {"price_and_bars": "alpaca"},
                     "age_seconds": 0.1, "is_delayed": True,
                     "feed_note": "delayed 15 minutes", "history_bars": 220},
    "market_context": {"available": True, "regime": "MIXED",
                       "risk_posture": "CAUTIOUS", "market_session": "OPEN",
                       "trend": "FLAT", "volatility": "NORMAL",
                       "breadth_proxy": "NARROW_LARGE_CAP",
                       "regime_agreement": 0.20, "indices": {},
                       "agreement_meaning": "How much the indices agree."},
    "disclaimer": "Not investment advice.",
}


def _variant(**over):
    import copy
    f = copy.deepcopy(BUY_FIXTURE)
    f.update(over)
    return f


HOLD_FIXTURE = _variant(
    symbol="AAPL", recommendation="HOLD", signal_strength="Mixed",
    signal_agreement=0.5,
    agreement_summary={
        "buy_groups": 1, "sell_groups": 1, "neutral_or_silent": 1,
        "verdict": "Signals disagree",
        "raw_signals_fired": 4, "groups_with_opinion": 2, "groups_total": 3,
    },
    groups=[
        _group("trend", "Trend", "BUY", "Supports upward direction",
               [{"label": "MA crossover (20/50)", "direction": "BUY"}]),
        _group("momentum", "Momentum", "SELL", "Supports downward direction",
               [{"label": "MACD", "direction": "SELL"}]),
        _group("mean_reversion", "Mean Reversion", "NO_SIGNAL",
               "No signal from this group", []),
    ],
    explanation={"headline": "Why HOLD?",
                 "lines": ["Momentum and Trend point in different directions."]},
)

MIXED_GROUP_FIXTURE = _variant(
    symbol="TSLA", recommendation="HOLD", signal_strength="Mixed",
    signal_agreement=0.5,
    agreement_summary=HOLD_FIXTURE["agreement_summary"],
    groups=[
        _group("trend", "Trend", "NEUTRAL",
               "Signals inside this group disagree, which reduces its influence",
               [{"label": "MA crossover (20/50)", "direction": "BUY"},
                {"label": "Golden/death cross (50/200)", "direction": "SELL"}],
               mixed=True),
        _group("momentum", "Momentum", "SELL", "Supports downward direction",
               [{"label": "MACD", "direction": "SELL"}]),
        _group("mean_reversion", "Mean Reversion", "NO_SIGNAL",
               "No signal from this group", []),
    ],
    explanation={"headline": "Why HOLD?", "lines": ["Inside Trend, signals conflict."]},
)

# A positive MACD line that is nonetheless a bearish crossover. Before the
# signal-line fix this state was unreachable, because signal was derived as
# macd * 0.9 and so macd > signal was identical to macd > 0.
POSITIVE_MACD_BEARISH = _variant(
    symbol="TSLA",
    indicators={**BUY_FIXTURE["indicators"],
                "macd": {"available": True, "macd_line": 2.08, "signal_line": 4.28,
                         "histogram": -2.20, "relation": "MACD < Signal",
                         "crossover": "bearish",
                         "interpretation": "Bearish crossover - MACD below its signal line",
                         "note": "A positive MACD line can still be a bearish crossover."}},
)

NEGATIVE_CHANGE = _variant(
    symbol="BA", price=186.28, change=-1.4000000000000057,
    change_percent="-0.7460%", recommendation="SELL", signal_strength="Strong",
    agreement_summary={"buy_groups": 0, "sell_groups": 2, "neutral_or_silent": 1,
                       "verdict": "Independent groups agree",
                       "raw_signals_fired": 4, "groups_with_opinion": 2,
                       "groups_total": 3},
    explanation={"headline": "Why SELL?", "lines": ["Trend and Momentum agree."]},
)

STALE = _variant(data_quality={**BUY_FIXTURE["data_quality"],
                               "freshness": "STALE", "age_seconds": 4200})

UNAVAILABLE = {"symbol": "ZZZZ", "analysis_available": False,
               "message": "Not enough price history to analyse ZZZZ."}


def render(fixture) -> str:
    return run_js(
        "console.log(renderAnalysis(" + json.dumps(fixture) + "));"
    )


@unittest.skipIf(NODE is None, "node not available")
class TestRecommendationStates(unittest.TestCase):

    def test_buy_renders_buy_verdict(self):
        html = render(BUY_FIXTURE)
        self.assertIn("dir-buy", html)
        self.assertRegex(html, r'class="call">.*?BUY')

    def test_sell_renders_sell_verdict(self):
        html = render(NEGATIVE_CHANGE)
        self.assertIn("dir-sell", html)

    def test_hold_renders_hold_verdict(self):
        html = render(HOLD_FIXTURE)
        self.assertIn("dir-hold", html)
        self.assertIn("HOLD", html)

    def test_unavailable_analysis_states_so_rather_than_inventing_a_call(self):
        html = render(UNAVAILABLE)
        self.assertIn("Not enough price history", html)
        for word in ("BUY", "SELL", "HOLD"):
            self.assertNotIn(f">{word}<", html)


@unittest.skipIf(NODE is None, "node not available")
class TestAgreementLanguage(unittest.TestCase):

    def test_agreeing_groups_are_described_as_agreement_not_probability(self):
        html = render(BUY_FIXTURE)
        self.assertIn("Independent groups agree", html)
        self.assertIn("groups support Buy", html)
        self.assertIn("Signal agreement", html)

    def test_disagreement_is_stated_plainly(self):
        html = render(HOLD_FIXTURE)
        self.assertIn("Signals disagree", html)
        self.assertIn("group support Buy", html)
        self.assertIn("group support Sell", html)

    def test_raw_signal_count_is_distinguished_from_group_count(self):
        html = render(BUY_FIXTURE)
        self.assertIn("4 indicators fired", html)
        self.assertIn("2 independent opinions", html)

    # The disclaimer card deliberately uses these words to deny them
    # ("Signal agreement is not a probability of a price move"), so the
    # scan below is applied to everything above it.
    DISCLAIMER_MARKER = "what this does not mean"
    BANNED = ("confidence", "probability", "% chance", "likelihood", "accuracy")

    def _claims_section(self, fixture) -> str:
        html = render(fixture).lower()
        idx = html.find(self.DISCLAIMER_MARKER)
        self.assertNotEqual(idx, -1, "disclaimer card missing from the page")
        return html[:idx]

    def test_no_confidence_or_probability_wording_in_the_claims(self):
        """
        'Confidence' and 'probability' imply a likelihood of a price move,
        which this number is not. The page must not assert them.
        """
        for fixture in (BUY_FIXTURE, HOLD_FIXTURE, NEGATIVE_CHANGE):
            claims = self._claims_section(fixture)
            for banned in self.BANNED:
                self.assertNotIn(banned, claims,
                                 f"banned term {banned!r} in {fixture['symbol']}")

    def test_falsifying_control_banned_term_scan_can_fail(self):
        """
        The scan must be able to detect a banned term in the region it
        actually inspects, not merely pass because it looks nowhere.
        """
        claims = self._claims_section(_variant(signal_strength="High confidence"))
        self.assertIn("confidence", claims)

    def test_falsifying_control_disclaimer_is_genuinely_excluded(self):
        """
        Proves the exclusion is doing work: the full page does contain
        'probability', and only the trimmed claims section does not.
        """
        full = render(BUY_FIXTURE).lower()
        self.assertIn("probability", full)
        self.assertNotIn("probability", self._claims_section(BUY_FIXTURE))


@unittest.skipIf(NODE is None, "node not available")
class TestInternalDisagreement(unittest.TestCase):

    def test_mixed_group_is_flagged_and_explained(self):
        html = render(MIXED_GROUP_FIXTURE)
        self.assertIn("g-mixed", html)
        self.assertIn("MIXED", html)
        self.assertIn("influence is halved", html)

    def test_mixed_group_shows_its_conflicting_members(self):
        html = render(MIXED_GROUP_FIXTURE)
        self.assertIn("MA crossover (20/50)", html)
        self.assertIn("Golden/death cross (50/200)", html)

    def test_agreeing_group_is_not_flagged_as_mixed(self):
        html = render(BUY_FIXTURE)
        self.assertNotIn("influence is halved", html)


@unittest.skipIf(NODE is None, "node not available")
class TestSilentGroup(unittest.TestCase):

    def test_silent_group_is_shown_and_marked_uncounted(self):
        html = render(BUY_FIXTURE)
        self.assertIn("Mean Reversion", html)
        self.assertIn("NO SIGNAL", html)
        self.assertIn("Not counted", html)

    def test_no_signal_is_distinguished_from_neutral(self):
        html = render(BUY_FIXTURE)
        self.assertIn("not the same as neutral", html)


@unittest.skipIf(NODE is None, "node not available")
class TestMacdDisplay(unittest.TestCase):

    def test_macd_shows_line_signal_and_histogram_separately(self):
        html = render(BUY_FIXTURE)
        for label in ("MACD line", "Signal line", "Histogram", "Relation"):
            self.assertIn(label, html)
        self.assertIn("2.99", html)
        self.assertIn("2.34", html)

    def test_positive_macd_line_can_show_a_bearish_crossover(self):
        html = render(POSITIVE_MACD_BEARISH)
        self.assertIn("2.08", html)
        self.assertIn("4.28", html)
        self.assertIn("MACD &lt; Signal", html)
        self.assertIn("Bearish crossover", html)

    def test_signal_line_definition_is_stated(self):
        html = render(BUY_FIXTURE)
        self.assertIn("9-period EMA", html)


@unittest.skipIf(NODE is None, "node not available")
class TestHonestIndicatorReadings(unittest.TestCase):

    def test_rsi_reports_the_engine_vote_not_an_invented_one(self):
        html = render(BUY_FIXTURE)
        self.assertIn("Engine vote", html)
        self.assertIn("NONE", html)
        self.assertIn("below 30 or above 70", html)

    def test_bollinger_says_near_band_is_not_a_signal(self):
        html = render(BUY_FIXTURE)
        self.assertIn("not itself a signal", html)


@unittest.skipIf(NODE is None, "node not available")
class TestPriceFormatting(unittest.TestCase):

    def test_negative_change_keeps_its_minus_sign(self):
        """
        The production modal rendered -$9.00 as "$9.00": it applied
        Math.abs() under a sign prefix that was empty for negatives.
        """
        html = render(NEGATIVE_CHANGE)
        self.assertIn("-$1.40", html)
        self.assertNotIn(">+$1.40", html)

    def test_negative_percent_keeps_its_minus_sign(self):
        html = render(NEGATIVE_CHANGE)
        self.assertIn("-0.75%", html)

    def test_positive_change_keeps_its_plus_sign(self):
        html = render(BUY_FIXTURE)
        self.assertIn("+$3.39", html)


@unittest.skipIf(NODE is None, "node not available")
class TestDataQuality(unittest.TestCase):

    def test_fresh_data_shows_source_and_age_without_a_warning_banner(self):
        html = render(BUY_FIXTURE)
        self.assertIn("FRESH", html)
        self.assertIn("alpaca", html)
        self.assertNotIn("STALE DATA", html)

    def test_stale_data_raises_a_visible_warning(self):
        html = render(STALE)
        self.assertIn("STALE DATA", html)
        self.assertIn("may not reflect current", html)

    def test_delayed_feed_is_disclosed(self):
        html = render(BUY_FIXTURE)
        self.assertIn("Delayed data", html)


@unittest.skipIf(NODE is None, "node not available")
class TestMarketContext(unittest.TestCase):

    def test_regime_and_posture_are_shown(self):
        html = render(BUY_FIXTURE)
        self.assertIn("MIXED", html)
        self.assertIn("CAUTIOUS", html)

    def test_regime_relevance_is_explained(self):
        html = render(BUY_FIXTURE)
        self.assertIn("does not move in isolation", html)


@unittest.skipIf(NODE is None, "node not available")
class TestAccessibility(unittest.TestCase):

    # Every element that encodes direction in its colour, and the glyph
    # that must accompany it. Asserting only that "a glyph exists
    # somewhere" is a guard that cannot fail: removing the glyph from
    # one element still leaves glyphs elsewhere on the page.
    COLOUR_CODED_ELEMENTS = (
        ("callbox verdict", r'<div class="call">(.*?)</div>'),
        ("group badge", r'<span class="badge"[^>]*>(.*?)</span>\s*$'),
        ("tally row", r'<div class="tally-row">(.*?)</div>'),
        ("group member", r'<span class="mv"[^>]*>(.*?)</span>'),
        ("price delta", r'<span class="delta"[^>]*>(.*?)</span>\s*$'),
    )
    GLYPHS = ("▲", "▼", "■", "—")

    def test_direction_is_never_conveyed_by_colour_alone(self):
        """
        Every element that uses colour to signal direction must also carry
        a glyph, so the state survives greyscale and colour blindness.
        """
        import re
        html = render(HOLD_FIXTURE)
        for name, pattern in self.COLOUR_CODED_ELEMENTS:
            chunks = re.findall(pattern, html, re.S | re.M)
            self.assertTrue(chunks, f"no {name} found to check")
            for chunk in chunks:
                self.assertTrue(
                    any(g in chunk for g in self.GLYPHS),
                    f"{name} conveys direction by colour alone: {chunk[:120]!r}",
                )

    def test_falsifying_control_glyph_check_inspects_each_element(self):
        """
        Proves the check above is per-element: a badge stripped of its
        glyph must be caught even though other glyphs remain on the page.
        """
        import re
        html = render(HOLD_FIXTURE)
        stripped = re.sub(
            r'(<span class="badge"[^>]*>)\s*<span class="glyph"[^>]*>.*?</span>',
            r"\1", html, flags=re.S,
        )
        self.assertNotEqual(stripped, html, "badge glyph pattern did not match")
        self.assertIn("▲", stripped, "other glyphs should still be present")
        name, pattern = self.COLOUR_CODED_ELEMENTS[1]
        offenders = [c for c in re.findall(pattern, stripped, re.S | re.M)
                     if not any(g in c for g in self.GLYPHS)]
        self.assertTrue(offenders, "per-element check failed to catch the strip")

    def test_agreement_meter_has_a_text_alternative(self):
        html = render(BUY_FIXTURE)
        self.assertIn('role="img"', html)
        self.assertIn("aria-label=\"Signal agreement 0.72", html)

    def test_glyphs_are_hidden_from_screen_readers_where_text_repeats_them(self):
        html = render(BUY_FIXTURE)
        self.assertIn('aria-hidden="true"', html)


@unittest.skipIf(NODE is None, "node not available")
class TestNoMisleadingClaims(unittest.TestCase):

    def test_page_states_what_the_analysis_does_not_mean(self):
        html = render(BUY_FIXTURE)
        self.assertIn("not predict future prices", html)
        self.assertIn("not advice", html)

    def test_page_states_no_order_can_be_placed(self):
        html = render(BUY_FIXTURE)
        self.assertIn("No order can be placed", html)


@unittest.skipIf(NODE is None, "node not available")
class TestNoProseParsing(unittest.TestCase):
    """
    Every quantitative field must come from the structured contract. If the
    renderer ever fell back to scraping AI prose, a fixture with no prose at
    all would lose its numbers.
    """

    def test_numbers_render_from_structured_fields_with_no_prose_present(self):
        html = render(BUY_FIXTURE)
        self.assertNotIn("analysis_text", html)
        self.assertIn("230.60", html)   # price
        self.assertIn("2.99", html)     # macd line
        self.assertIn("65.9", html)     # rsi


if __name__ == "__main__":
    unittest.main()
