"""
No credential may reach a tracked file.

The project has already leaked one key: an Alpha Vantage key was
committed in plaintext to three Markdown files and pushed to a public
remote, where it must be assumed captured
(`docs/SECURITY_ACTION_REQUIRED.md`). This suite is the standing check
that it does not happen again, including for the Alpaca paper key pair
that arrived on 2026-10-01.

The scanner is exercised against a planted sample as well as the real
repository, because a scanner that has never been seen to fire is not
evidence that the repository is clean.
"""
import os
import re
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Vendored third-party code carries AWS documentation examples and
# library text that match these shapes without being credentials.
VENDORED = ("lambda-layer/", "lambda-micro/chatbot-router/package/",
            "lambda-packages/", "node_modules/", "dist/")

PATTERNS = {
    "alpaca_key_id": r"\bPK[A-Z0-9]{16,24}\b",
    # An Alpaca secret is 40 random base64 characters. A plain
    # 40-character pattern also matches English prose and long
    # identifiers ("TestStrategyGatesRequireRealTimeEvidence"), so the
    # candidate is additionally required to look random - see
    # `_looks_random`. Pattern alone was 5 false positives, 0 real.
    "alpaca_secret": r"\b[A-Za-z0-9/+]{40}\b",
    "aws_access_key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "openai_key": r"\bsk-[A-Za-z0-9_-]{20,}\b",
    "github_token": r"\bghp_[A-Za-z0-9]{30,}\b",
    "slack_token": r"\bxox[bap]-[A-Za-z0-9-]{10,}\b",
    "private_key_block": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "assignment": (r"(?i)(api[_-]?key|apikey|secret[_-]?key|api[_-]?secret|"
                   r"password)\s*[:=]\s*['\"][A-Za-z0-9/+_-]{16,}['\"]"),
}


# Patterns whose matches must also look random to count.
ENTROPY_CHECKED = {"alpaca_secret"}


def _looks_random(token):
    """Three character classes and high per-character entropy.

    A secret is drawn at random; a word or an identifier is not. Both
    tests are needed: "TestStrategyGatesRequireRealTimeEvidence" has two
    classes, and a 40-character run of prose has low entropy.
    """
    import math
    from collections import Counter
    classes = sum(bool(re.search(cls, token))
                  for cls in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    if classes < 3:
        return False
    counts = Counter(token)
    entropy = -sum((n / len(token)) * math.log2(n / len(token))
                   for n in counts.values())
    return entropy >= 4.2


def scan(text):
    """Pattern names that fire on `text`. Never returns the value."""
    hits = []
    for name, pattern in PATTERNS.items():
        matches = re.findall(pattern, text)
        if not matches:
            continue
        if name in ENTROPY_CHECKED and not any(
                _looks_random(m if isinstance(m, str) else m[0])
                for m in matches):
            continue
        hits.append(name)
    return sorted(hits)


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return [f for f in out.stdout.splitlines()
            if f and not f.startswith(VENDORED)]


class TestTheScannerActuallyFires(unittest.TestCase):
    """The falsifying control. Without these, a clean result below could
    mean the patterns match nothing at all."""

    def test_it_detects_a_planted_alpaca_key_id(self):
        self.assertIn("alpaca_key_id", scan("key = PKABCDEFGHIJKLMNOPQRST"))

    def test_it_detects_a_planted_alpaca_secret(self):
        # A realistic 40-character random secret, not a repeated run.
        secret = "aZ3kQ9vB2nM7xL1pR8tY6wC4uE0gH5jD/sF+bN2q"
        self.assertEqual(len(secret), 40)
        self.assertIn("alpaca_secret", scan(f"secret: {secret}"))

    def test_prose_and_identifiers_are_not_mistaken_for_secrets(self):
        """Five false positives before the randomness check: ordinary
        words and long test-class names are 40 characters too."""
        for benign in ("TestStrategyGatesRequireRealTimeEvidenceNow",
                       "blockchainblockchainblockchainblockcha",
                       "a" * 40, "ABCDEFGHIJKLMNOPQRSTUVWXYZABCDEFGHIJKLMN"):
            with self.subTest(text=benign[:20]):
                self.assertNotIn("alpaca_secret", scan(benign))

    def test_it_detects_an_aws_key_and_an_openai_key(self):
        self.assertIn("aws_access_key", scan("AKIA" + "Z" * 16))
        self.assertIn("openai_key", scan("sk-" + "a" * 32))

    def test_it_detects_a_credential_assignment(self):
        self.assertIn("assignment", scan('api_key = "abcdefghij0123456789"'))

    def test_it_detects_a_private_key_block(self):
        self.assertIn("private_key_block",
                      scan("-----BEGIN RSA PRIVATE KEY-----"))

    def test_ordinary_source_does_not_fire(self):
        self.assertEqual(scan('api_key = os.environ["ALPACA_KEY"]'), [])
        self.assertEqual(scan("retrieved_at: float  # epoch seconds"), [])


class TestNoCredentialsInTrackedFiles(unittest.TestCase):

    def test_tracked_files_carry_no_credentials(self):
        offenders = {}
        for path in tracked_files():
            full = os.path.join(REPO, path)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, "r", errors="ignore") as handle:
                    text = handle.read(400_000)
            except OSError:
                continue
            hits = scan(text)
            if hits:
                offenders[path] = hits
        self.assertEqual(offenders, {},
                         f"credential-shaped content in tracked files: "
                         f"{sorted(offenders)}")

    def test_the_local_credential_file_is_not_tracked(self):
        tracked = set(tracked_files())
        for name in tracked:
            self.assertNotIn("Alpaca Paper Trading Key", name)

    def test_stray_credential_filenames_are_ignored(self):
        """A key dropped into the tree must not be committable."""
        for candidate in ("Alpaca Paper Trading Key.txt", "my-key.txt",
                          "alpaca-secret.json", "aws-credentials.json"):
            with self.subTest(name=candidate):
                out = subprocess.run(
                    ["git", "check-ignore", "-q", candidate],
                    cwd=REPO, capture_output=True)
                self.assertEqual(out.returncode, 0,
                                 f"{candidate} is not git-ignored")


if __name__ == "__main__":
    unittest.main()
