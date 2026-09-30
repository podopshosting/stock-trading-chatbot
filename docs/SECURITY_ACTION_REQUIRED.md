# SECURITY_ACTION_REQUIRED

**Status: OPEN — awaiting a replacement credential from the account owner.**
Opened 2026-09-30. This file stays until the item is closed.

---

## Alpha Vantage API key must be rotated

**What happened.** The live Alpha Vantage API key was committed in plaintext
to three Markdown files and pushed to GitHub. It is present in public git
history in commits `9a075ed`, `96ade16` and `4586d28`.

**Why redaction was not enough.** The working tree was redacted in commit
`4ae017a`, but the key remains reachable in history, and anything pushed to a
public remote should be assumed captured. Rewriting history would not undo
that either.

**Therefore the key is compromised and must be replaced.**

## Why this is not blocking development

The key still works and remains in AWS Secrets Manager at
`stock-chatbot/alphavantage-api-key`. Production and Phase 1 development both
continue. The exposure is a real but bounded risk: the key grants access to a
free-tier market data account with a 25 request/day quota. It carries no
funds, no personal data and no write access to anything.

The practical damage is quota theft — someone else spending the daily budget,
which would surface as the application being rate limited.

## What the account owner needs to do

1. Sign in to the Alpha Vantage account and issue a new API key.
2. Provide it through a private channel — **never** in a commit, an issue, a
   document or a chat transcript.

## What happens then (no owner action needed)

3. Update the secret:
   ```bash
   AWS_PROFILE=mypodops aws secretsmanager put-secret-value \
     --secret-id stock-chatbot/alphavantage-api-key \
     --secret-string '<new key>' --region us-east-2
   ```
4. Verify production still answers a stock query end to end.
5. Revoke or abandon the old key if Alpha Vantage supports it.
6. Delete this file and note the rotation in `PRODUCTION-STATE.md`.

## Rules that stay in force regardless

- No credential is ever written to a file in this repository.
- Both keys are read at runtime from AWS Secrets Manager.
- The OpenAI key was checked and is **not** present in the repository or its
  history.
- No secret value appears in this file, and none should be added to it.
