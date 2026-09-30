# SECURITY_ACTION_REQUIRED

**Status: PARTIALLY CLOSED — one step remains, and it needs the account owner.**
Opened 2026-09-30. Rotated 2026-09-30. This file stays until the last item is done.

---

## Alpha Vantage API key

### What happened

The live Alpha Vantage API key was committed in plaintext to three Markdown
files and pushed to GitHub. It is present in public git history in commits
`9a075ed`, `96ade16` and `4586d28`. Anything pushed to a public remote should
be assumed captured, and rewriting history would not undo that.

### DONE — production rotated off the exposed key

A replacement key was supplied by the account owner and installed:

- `stock-chatbot/alphavantage-api-key` now holds the new key
  (verified by fingerprint, `AWSCURRENT`)
- the previous value is retained as `AWSPREVIOUS` for rollback
- production verified end to end after rotation: a stock query through
  API Gateway returned a live quote with the ML layer running
- nothing this project runs uses the exposed key any more

### STILL OPEN — the exposed key is still active

**Verified on 2026-09-30: the old key continues to return data.** Rotation
moved us off it; it did not disable it.

Alpha Vantage provides no self-service way to revoke a key. Their guidance is
to tell them:

> If you suspect that your API key has been compromised at any point, you
> should let Alpha Vantage know and they will take actions accordingly.
> — <https://www.alphavantage.co/support/>

**Remaining step (account owner):** contact Alpha Vantage support, say the key
was exposed in a public repository, and ask them to revoke it. One message.
**Do not paste the key into the support form or anywhere else** — describe it
as "a free-tier key issued to this account, exposed publicly, please revoke"
and let them identify it from the account.

### How much this actually matters now

Low, and bounded. The exposed key grants a free-tier market data account: 25
requests/day, no funds, no personal data, no write access anywhere. Nothing
depends on it, so the realistic harm is a stranger consuming a quota we no
longer use.

The reason to finish the job anyway is that the key is registered to the
owner's Alpha Vantage account, so its usage is attributable to them.

---

## Credentials shared in chat transcripts

Two credentials were pasted into a session transcript during this work: the
Alpaca account password and the replacement Alpha Vantage key.

- **The Alpaca password should be changed**, and urgently if it is reused on
  any other service. The paper account holds no real money, so reuse
  elsewhere is the real exposure, not the account itself.
- The replacement Alpha Vantage key is free-tier and bounded as described
  above, but it is now in a transcript. Treat it as public. If it is ever
  swapped again, prefer a channel that is not a chat log.

Neither value is written anywhere in this repository.

---

## Rules that stay in force

- No credential is ever written to a file in this repository.
- Every key is read at runtime from AWS Secrets Manager.
- Alpaca paper keys live at `stock-agent/alpaca-paper`.
- The OpenAI key was checked and is **not** present in the repository or its
  history.
- No secret value appears in this file, and none should be added to it.
