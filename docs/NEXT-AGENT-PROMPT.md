You are taking over a risk-governed paper-trading agent project.

Repo: /Users/Brian 1/Documents/GitHub/stock-trading-chatbot   Branch: feature/trading-agent-v1
AWS: account 899383035514, ALWAYS `export AWS_PROFILE=mypodops` (default profile is another account).

1. Read `docs/AGENT-HANDOFF.md` fully. Machine state: `docs/agent-handoff-state.json`.
2. Verify: `git rev-list -n1 agent-handoff-2026-10-01` equals HEAD (or HEAD descends from it); working tree clean; run the commands in handoff section 20; tests should be 1738 / 0 failing / 1 skipped.
3. Do NOT redo completed work (milestones 7–19A; Company Intelligence modules, provider, API listed as DONE).
4. The autonomous paper session runs independently every 5 minutes. Do not redeploy the cycle or scanner Lambdas, change strategy/risk config, or retune after a zero-trade day. A fix to the execution path is allowed only for a proven blocker and starts a new evidence cohort (record it).
5. Resume at handoff section 18: verify session health, collect the end-of-day report, raise the delayed-quote evidence issue with the user, then Company Intelligence in the listed order (peer-comparison budget → live validation → dashboard → chat → HOLDING-STRATEGY-INPUTS doc → remaining falsifying controls → dev-only deploy).
6. Keep going automatically between milestones. STOP only if: an external account/credential action is required, a destructive/irreversible operation is needed, there is a genuine blocker, or the next step would enable real-money execution.
7. Never: enable real money; deploy to production (`stock-chatbot-*`); add co-author/AI attribution to commits; force-push; read or probe the `stock-agent/alpaca-paper` secret; scrape or automate Fidelity; weaken the Risk Governor.
8. Every new guard needs a falsifying control: ask "WHAT PROVES THIS GUARD EXECUTES?" Test with real provider types, not floats.
