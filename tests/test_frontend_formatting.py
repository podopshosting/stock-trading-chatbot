"""
Regression test for the stock-details modal sign bug.

The modal rendered a -$9.00 change as "$9.00" — it applied Math.abs() while
its sign prefix was '+' or '' (empty for negatives), so the minus vanished
from a dollar figure while the percentage stayed negative.

The formatter is extracted from the shipped index.html and executed, so this
tests the code that actually runs rather than a copy of it. Requires node;
skips cleanly when node is unavailable.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(REPO_ROOT, "web", "static", "index.html")

NODE = shutil.which("node")


def extract_formatter() -> str:
    """Pull formatSignedCurrency out of index.html."""
    with open(INDEX_HTML, encoding="utf-8") as fh:
        html = fh.read()
    match = re.search(
        r"(function formatSignedCurrency\(value\)\s*\{.*?\n        \})",
        html, re.S,
    )
    if match is None:
        raise AssertionError(
            "formatSignedCurrency not found in web/static/index.html - "
            "the modal sign fix may have been reverted"
        )
    return match.group(1)


class TestSignedCurrencyFormatting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if NODE is None:
            raise unittest.SkipTest("node not available; skipping JS formatter test")
        cls.formatter = extract_formatter()

    def run_cases(self, cases):
        script = (
            self.formatter
            + "\nconst cases = " + json.dumps(cases) + ";"
            + "\nconsole.log(JSON.stringify(cases.map(formatSignedCurrency)));"
        )
        out = subprocess.run(
            [NODE, "-e", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip())

    def test_negative_change_keeps_its_minus_sign(self):
        """The actual bug: -9.0 must not render as $9.00."""
        self.assertEqual(self.run_cases([-9.0]), ["-$9.00"])

    def test_signs_and_edge_values(self):
        cases = [-9.0, "-9.0000", 1.5, "1.5", 0, "0.0000", 0.005, -0.004]
        expected = [
            "-$9.00",   # negative float
            "-$9.00",   # negative string, as the API returns it
            "+$1.50",   # positive gets an explicit plus
            "+$1.50",
            "$0.00",    # zero gets no sign
            "$0.00",
            "+$0.01",   # rounds, keeps sign
            "-$0.00",   # rounds to zero but was negative: sign retained
        ]
        self.assertEqual(self.run_cases(cases), expected)

    def test_missing_values_are_not_reported_as_zero(self):
        """A missing change must read N/A, never '$0.00', which would assert
        that the stock was flat."""
        self.assertEqual(
            self.run_cases([None, "", "N/A", "abc"]),
            ["N/A", "N/A", "N/A", "N/A"],
        )


class TestModalUsesTheFormatter(unittest.TestCase):
    def test_modal_no_longer_uses_bare_math_abs_for_change(self):
        with open(INDEX_HTML, encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotRegex(
            html,
            r"\$\$\{Math\.abs\(stockData\.change",
            "modal must format the change through formatSignedCurrency",
        )
        self.assertIn("formatSignedCurrency(stockData.change)", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
