# Broker Integration Options for Programmatic Order Entry

**Research date:** 2026-09-30
**Context:** Serverless (AWS Lambda) trading system, US retail individual account holder.
**Scope:** Officially supported APIs that permit *placing orders* (not just reading data).

---

## TL;DR — the Fidelity answer

**No. Fidelity does not offer an official, supported API that lets an individual retail customer place trades programmatically. As of this research date there is no public Fidelity retail trading API, no sandbox, no developer portal for it, and no announced pilot.**

- **Fidelity Access** — the thing people find when they search for "Fidelity API" — is **read-only account data aggregation**, delivered to third-party aggregators (Akoya, Plaid, etc.). Fidelity's own page states it "allows read-only access to account info that you authorize." It **cannot place orders**, and it is not something you as an individual integrate against directly; you *grant* it to an approved aggregator.
- **Fidelity's FIX connectivity and Integration Xchange / Wealthscape APIs are institutional.** They are gated behind institutional account status, million-dollar-scale minimums, contractual onboarding, and technical certification. They are for RIAs, broker-dealers and clearing/custody clients — **not available to a retail individual**.
- The `fidelity-api` PyPI package and similar GitHub projects are **unofficial browser automation** (Playwright driving the real fidelity.com UI) or **reverse-engineered** private endpoints. **Do not use them.** Reasons documented in detail below.

**Practical consequence for this project:** you cannot trade your Fidelity account from AWS Lambda by supported means. If the goal is programmatic trading, it requires a second brokerage account at a broker that offers a real API. Your Fidelity account can remain your long-term/custody account; the algorithmic sleeve lives elsewhere.

---

## Comparison table

| | **Fidelity** | **Alpaca** | **Interactive Brokers** |
|---|---|---|---|
| **Official retail order-entry API** | ❌ **No** | ✅ Yes — it is the core product | ✅ Yes |
| **Paper / sandbox** | ❌ None | ✅ Free, open to anyone, email signup only | ✅ Free paper account (~$1M simulated), but **requires a funded, open IBKR Pro live account** for API access |
| **Fractional shares via API** | n/a | ✅ Yes, from $1, 2,000+ US equities (**market orders only, `day` TIF only, RTH only**) | ✅ Yes, via cash-quantity (`cashQty`) orders; permissions must be enabled on the account |
| **Account minimum** | n/a | **$0** to open/fund; $2,000 for margin/short (Reg T) | **$0** minimum to open; practical minimums apply for margin/market data |
| **Auth model** | n/a | API key + secret (own account); OAuth 2.0 (third-party apps, requires Alpaca approval) | OAuth 2.0 `private_key_jwt` (institutional / registered clients) **or** local Client Portal Gateway (the retail path); TWS API needs local TWS/IB Gateway |
| **Rate limits** | n/a | **200 req/min per API key** | **50 req/s** per authenticated Web API session; **10 req/s** via CP Gateway; TWS API has its own pacing rules (~50 msg/s) |
| **Streaming** | n/a | WebSocket: market data + trade updates. Free tier = IEX only, 1 connection, 30 symbols | WebSocket (Web API) and event-driven socket (TWS API); market data requires paid subscriptions |
| **Long-running process required?** | n/a | ❌ **No — pure stateless REST** | ⚠️ **Yes, for retail** — CP Gateway or TWS/IB Gateway must be running and re-authenticated |
| **Lambda-friendly** | n/a | ✅ **Excellent** | ❌ Poor without an always-on EC2/ECS sidecar |
| **Algorithmic trading allowed** | n/a | ✅ Explicitly the intended use | ✅ Explicitly supported |

---

## Fidelity — detailed findings

### 1. Official retail trading API: does not exist

Every current source converges on the same answer. TradersPost's broker-API survey (updated **2026-08-06**) states plainly: *"Fidelity does not currently offer a public API for retail traders"*, and adds that *"Fidelity has not announced pilot programs for a public retail API."*
→ https://blog.traderspost.io/article/does-fidelity-have-an-api

There is no `developer.fidelity.com` retail trading portal, no API key issuance flow in the retail account UI, and no sandbox.

### 2. Fidelity Access — read-only, and not for you to call

Fidelity Access is Fidelity's answer to screen scraping. It is a **data-sharing** mechanism, not a trading interface.

- Fidelity's own security page: Fidelity Access *"allows read-only access to account info that you authorize"* and *"you never have to provide your Fidelity username and password to a third party."*
  → https://www.fidelity.com/security/fidelity-access-data-security
- The consuming side is an **approved aggregator** — Akoya (spun out of Fidelity in 2020, still co-owned by Fidelity and eleven major US banks), Plaid, Yodlee, MX, etc. You authorize *them*; you do not get an API key.
  → https://www.mx.com/blog/a-list-of-financial-data-aggregators-in-the-united-states/
- Effective **October 1, 2023**, Fidelity began *prohibiting third parties from accessing customer data through screen scraping* and required all third parties to move to Fidelity Access / standardized APIs.
  → https://newsroom.fidelity.com/pressreleases/fidelity-takes-steps-to-address-screen-scraping/s/2f33bc18-f16d-4b66-9868-626ada9ba32b
  → https://riabiz.com/a/2023/10/19/fidelity-just-dropped-the-hammer-on-screen-scrapers-to-cheers-but-some-firms-like-plaid-are-holdouts-and-the-cfpb-may-wield-the-final-gavel

**Verdict: aggregation only. Balances, positions, transactions. No order entry, no order status, no cancel/replace.** Nothing in the Akoya product line (Balances/Investments, Transactions) is a trading endpoint.

### 3. Fidelity Institutional / Wealthscape / Integration Xchange / FIX — not available to individuals

Fidelity *does* run substantial API and FIX infrastructure — for institutions.

- **Integration Xchange** (launched October 2018) is Fidelity Institutional's open-architecture integration catalog for wealth-management firms. It exposes *"seven direct integration types … including APIs, SSO integrations, server-to-server alerts, inbound file processing, outbound transmissions, Financial Information eXchange (FIX®) trading connectivity and network connectivity integrations."*
  → https://www.businesswire.com/news/home/20200916005108/en/
  → https://integrationxchange.wealthscape.com/
- **WealthCentral** (2007/2008) was merged with StreetScape in 2016 into **Wealthscape**, the current RIA/broker-dealer platform. It is a *platform for advisory firms*, accessed under an institutional clearing-and-custody relationship.
  → https://www.wealthmanagement.com/financial-technology/fidelity-updates-wealthscape-platform-integration-xchange
- **FIX connectivity** is described as gated by *"institutional account status, minimum balances in millions, and technical certification."*
  → https://blog.traderspost.io/article/does-fidelity-have-an-api

To use any of this you would need to *be* a registered investment adviser or broker-dealer custodying with Fidelity Institutional, execute a contract, and pass onboarding/certification. Opening a personal Fidelity brokerage account does not grant access, and there is no self-service path.

### 4. Unofficial approaches — documented, and explicitly rejected

Two families of unofficial tooling exist. **We will not use either.**

**(a) Browser automation** — e.g. `fidelity-api` on PyPI and `kennyboy106/fidelity-api` on GitHub. Playwright drives a headless Chromium through the real fidelity.com login (including 2FA), scrapes positions, and clicks through the trade ticket to place orders.
→ https://pypi.org/project/fidelity-api/
→ https://github.com/kennyboy106/fidelity-api
The README's own framing: *"I am not a financial advisor and not affiliated with Fidelity in any way. Use this tool at your own risk."*

**(b) Reverse-engineered private endpoints** — e.g. "Fidelity Trader API", built from `mitmproxy` captures of the Fidelity Trader+ desktop application's network traffic. Self-described as *"an unofficial, community-driven project not affiliated with, endorsed by, or supported by Fidelity Investments."*
→ https://brownjosiah.github.io/fidelity-trader-api/

**Why these are disqualifying:**

1. **Terms-of-service exposure.** Fidelity publicly moved to shut down automated non-sanctioned access in 2023 and restricts account access to individual, authorized use. Automated credential-driven access of this kind runs directly against the posture Fidelity has taken. The realistic downside is not a lawsuit — it is **account lockout or termination of your brokerage relationship**, which is a catastrophic outcome for the account holder.
2. **Credential exposure.** These tools require your **live Fidelity username, password and 2FA seed/handling** to be present in the runtime. Putting full-control credentials for a brokerage account into an automated cloud process is an unacceptable risk profile regardless of ToS. There is no scoped token, no revocable grant, no read-only mode, no per-action authorization.
3. **Silent breakage on a money path.** A UI change, an A/B test, a new interstitial, a 2FA prompt variant — any of these can change behavior without notice. The dangerous failure mode is not "crash"; it is **a mis-parsed page producing a wrong order** (wrong symbol field, wrong quantity, wrong side) or a submit that silently doesn't submit while the system believes it did.
4. **No recourse.** There is no support channel, no status page, no rate-limit contract, no error taxonomy, no idempotency guarantees. When an order goes wrong, you own it entirely.
5. **Bot-detection arms race.** Fidelity actively fingerprints automation. Headless browsers get challenged, throttled, or flagged as account compromise — which can itself trigger a security lock.

**Standing decision: Fidelity is a read-only / manual-execution account for this system. No unofficial automation, in any environment, including paper-equivalent testing.**

---

## Alpaca — detailed findings

**Official order-entry API:** Yes. Programmatic trading *is* the product, not a side channel.
→ https://docs.alpaca.markets/us/docs/trading-api

### Paper / sandbox
Free and open. *"Paper trading is free and available to all Alpaca users"* — a real-time simulation environment. **Anyone globally can create a paper-only account with just an email address**, no KYC, no funding. Switch by pointing at `paper-api.alpaca.markets` with paper keys; the API surface is identical to live.
→ https://docs.alpaca.markets/us/docs/paper-trading

This is a genuine advantage: you can build and run the full system end-to-end before opening any funded account.

### Fractional shares
Supported — as little as **$1** across **2,000+ US equities**. Important constraints:

- **Market orders only** for fractional quantities.
- **`day` time-in-force only** — `gtc`, `ioc`, `fok`, `opg`, `cls` are all rejected on fractional orders.
- **Regular trading hours only** — no fractional in extended hours.

→ https://docs.alpaca.markets/docs/orders-at-alpaca

### Account requirements and minimums
- **US residents:** 18+, valid **SSN** (not an ITIN), legal US residential address in the 50 states or Puerto Rico, and US citizen / permanent resident / qualifying visa holder (E-1, E-2, E-3, F-1, H1-B, TN-1, J-1, L-1, B-1, B-2, O-1).
- **Non-US residents** can also open accounts via international KYC.
- **Minimum deposit: $0.** Margin or short selling requires a **$2,000** minimum equity balance (Reg T).

→ https://alpaca.markets/support/requirements-alpaca-brokerage-account
→ https://alpaca.markets/support/alpaca-minimum-deposit

### Authentication
Two models:

1. **API key + secret** (`APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers) — for trading **your own** account. This is what this project needs. Stateless: every request carries its own credentials. Store in AWS Secrets Manager / SSM Parameter Store, never in env vars baked into the deployment package.
2. **OAuth 2.0** — for building an app that trades on behalf of *other* users. `client_id`/`client_secret`, standard authorize/redirect flow, `env=paper|live` scoping. **Requires Alpaca approval before an OAuth app can execute live trades.** Not needed unless this becomes a multi-tenant product.

→ https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api

### Rate limits
**200 requests per minute per API key.** Exceeding returns HTTP 429. For a Lambda-based system this is generous but real — a fan-out across many symbols must be batched and throttled centrally rather than left to concurrent Lambda invocations, which can collectively blow the budget.

### Order types
`market`, `limit`, `stop`, `stop_limit`, `trailing_stop`, plus advanced classes: **bracket**, **OCO**, **OTO**.

Time-in-force: `day`, `gtc` (auto-cancels after 90 days), `opg`, `cls`, `ioc`, `fok`.

Extended hours (4:00–9:30am / 4:00–8:00pm ET): **limit orders only**, `day` or `gtc` only.

### Streaming
WebSocket for both **market data** and **trade updates** (fills, cancels, rejects). Free market-data tier: **IEX only, 1 concurrent connection, 30 symbols** for trades/quotes (minute bars unlimited). Paid tiers give full CTA/UTP SIP consolidated feeds.
→ https://docs.alpaca.markets/us/docs/streaming-market-data
→ https://alpaca.markets/data

⚠️ **Lambda note:** WebSockets do not belong in Lambda. If you need the trade-updates stream, run it on ECS Fargate / a small EC2 instance and push events into SQS/EventBridge, **or** poll the REST orders endpoint from a scheduled Lambda. Polling is the simpler architecture and is well within the 200/min budget.

### Restrictions relevant to automation
- Algorithmic/automated trading is the intended use — no prohibition.
- PDT (pattern day trader) rules apply: under $25,000 equity, 4+ day trades in 5 business days triggers restriction. **This is a real constraint on a bot that intraday-trades a small account.**
- Fractional orders are restricted as noted above.
- Commission-free US equities.

### Serverless suitability
✅ **Excellent.** Pure stateless HTTPS REST with per-request auth. No gateway, no session, no keepalive, no daily re-login, no 2FA prompt. A Lambda cold-starts, reads a secret, signs nothing, POSTs an order, and exits. This is the shape the architecture wants.

---

## Interactive Brokers — detailed findings

**Official order-entry API:** Yes, and it is far more capable than Alpaca's (global markets, options, futures, FX, bonds, complex order types, IBKR algos). The problem is not capability — it is **session architecture**.
→ https://www.interactivebrokers.com/en/trading/ib-api.php

IBKR offers three relevant surfaces:

1. **Web API** (formerly Client Portal Web API) — REST/WebSocket. IBKR is consolidating Client Portal Web API, Digital Account Management and the Flex Web Service into a single **IBKR Web API** unified under **OAuth 2.0**.
   → https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/
2. **TWS API** — socket protocol against a running Trader Workstation or IB Gateway desktop app. C++, C#, Java, Python.
   → https://interactivebrokers.github.io/tws-api/
3. **FIX** — institutional only.

### ⚠️ The gateway problem — the decisive issue for this project

IBKR's documentation is explicit about the retail path:

> *"For retail and individual clients, Authentication to our WebAPI is managed using the **Client Portal Gateway**, a small java program used to route local web requests with appropriate authentication."*

> *"In order to use Client Portal API, a lightweight API gateway, or in the case of **institutional clients**, OAuth or a dedicated connection, is required."*

→ https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/

So: **OAuth 2.0 (`private_key_jwt`, RFC 7521/7523) — the one path that would be stateless and Lambda-compatible — is the institutional path.** The retail path requires a **Java process you host and keep alive**.

And that process is not "set and forget":

- **Brokerage session times out after 5 minutes of inactivity.** You must call `/tickle` roughly **every minute** to keep it alive.
- The session is **tied to two-factor authentication**, **must be restarted at least once per day**, and **once a week IBKR expires it entirely and demands a full re-login**.
- The **IBKR Key 2FA push must be approved on a phone within ~2 minutes**.

→ https://www.interactivebrokers.com/docs/web-api/authentication/faq
→ https://quietalphalab.com/ibkr-2fa-24-7-automated-trading/

There is a well-known community workaround (`Voyz/ibeam`, a Docker container that automates gateway login and keepalive), but it is unofficial, it stores your IBKR credentials, and its issue tracker is full of "Gateway session active but not authenticated" loops. It reintroduces exactly the credential-in-automation risk we rejected for Fidelity.
→ https://github.com/Voyz/ibeam

**The TWS API is worse for this purpose** — it needs a full desktop TWS or IB Gateway GUI app running, with its own daily restart and 2FA cycle.

### Paper trading
Free, and good: all clients get a paper account seeded with **~$1,000,000** simulated equity that behaves against real market conditions. Same API, different port (TWS API) or account.

**Catch:** *"Whether accessing a live account or its associated simulated paper account, the live account must be fully open and funded. The live account must also be of the 'IBKR Pro' type."* You cannot evaluate IBKR's API without first opening and funding a real IBKR Pro account.
→ https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/

Paper does not support every order type — **VWAP, Auction, RFQ and Pegged-to-Market are unavailable** in simulation.

### Fractional shares
Supported via **cash-quantity orders** (`cashQty` in TWS API) — specify a dollar amount and IBKR buys/sells the fractional remainder. Use `reqContractDetails` to read `MinSize` / `SizeIncrement` to construct valid quantities.

**Fractional permissions must be explicitly enabled on the account** (Account Settings → Trading Permissions), and community reports indicate fractional handling is more reliable through IB Gateway/TWS API than through the Web API.
→ https://www.interactivebrokers.com/en/trading/fractional-trading.php
→ https://groups.io/g/twsapi/topic/fractional_shares_via_tws_api/69203949

### Account requirements and minimums
- **No minimum to open** an IBKR Pro account for US residents.
- **API access requires an open, funded, IBKR Pro-type live account** (IBKR Lite does not qualify for the paper-account API path described above).
- Market data is **à la carte paid subscriptions** — budget for this; there is no free consolidated feed.

### Rate limits
- **Direct Web API: 50 requests/second** per authenticated username/session.
- **Via CP Gateway: 10 requests/second.**
- Exceeding → **HTTP 429**; offending IPs go into a **10-minute penalty box**; repeat offenders can be **permanently blocked** pending resolution.
- TWS API has separate pacing rules (broadly ~50 messages/second, plus strict historical-data pacing).

→ https://www.interactivebrokers.com/docs/web-api/trading/usage-and-availability/pacing-limitations
→ https://interactivebrokers.github.io/tws-api/order_limitations.html

⚠️ Note that an IP-level penalty box interacts badly with Lambda: NAT-gateway egress means a runaway function can get the **whole VPC's egress IP** boxed.

### Order types
The broadest of the three by a wide margin: market, limit, stop, stop-limit, trailing, bracket, OCA/OCO, conditional, MOC/LOC, relative/pegged, plus IBKR's algo suite (Adaptive, TWAP, VWAP, Accumulate/Distribute, etc.), across equities, options, futures, FX, bonds and global exchanges.

### Streaming
WebSocket on the Web API; event-driven callbacks on the TWS API. Both assume a persistent connection — same architectural problem as the gateway.

### Restrictions relevant to automation
- Automated trading is explicitly supported and common.
- PDT rules apply identically.
- Market-data subscriptions and their redistribution terms are enforced.
- Mandatory daily session restart and weekly full re-login are hard operational constraints on 24/7 automation.

### Serverless suitability
❌ **Poor for retail.** A Lambda cannot host a persistent authenticated gateway. Making IBKR work from Lambda means standing up an **always-on ECS Fargate or EC2 sidecar** running CP Gateway (or TWS/IB Gateway), with automated 2FA handling and health monitoring, and having Lambdas call *that* over a private network path. That is a whole second system — the very thing a serverless architecture exists to avoid — and it puts a stateful, daily-expiring, 2FA-gated single point of failure directly in the order path.

If IBKR granted OAuth 2.0 to individual accounts, this assessment would flip. Confirm current eligibility with IBKR API Integration support before assuming it is closed — the Web API consolidation is actively in progress and this is the one fact most likely to have moved.

---

## Recommendation

### Primary: **Alpaca**

Build against Alpaca. It is the only one of the three that is both (a) officially open to a US retail individual for order entry and (b) architecturally compatible with AWS Lambda.

The decisive property is **statelessness**. Alpaca is plain REST with per-request API-key auth — no gateway process, no session to keep alive, no daily re-login, no 2FA prompt in the order path. A Lambda can place an order in one cold start and exit. Nothing else on this list can say that.

Supporting reasons:

- **Free, unrestricted paper environment with no funded account required.** You can build and validate the entire system, including failure paths, before any money exists. IBKR cannot offer this; Fidelity offers nothing.
- **$0 minimum**, commission-free US equities, fractional shares from $1 — appropriate for the account sizes this system will realistically run.
- **200 req/min** is ample for a rebalancing/signal bot and forces healthy centralized throttling.
- Clean upgrade path to OAuth 2.0 if this ever serves users other than you.

Design constraints to carry into implementation:

1. **Fractional = market + `day` + RTH only.** If the strategy needs limit orders, it needs whole shares.
2. **Do not run WebSockets in Lambda.** Poll `GET /v2/orders` from a scheduled Lambda for fill confirmation, or put the trade-updates stream on Fargate and fan out through EventBridge.
3. **Keep API keys in Secrets Manager**, fetched at invocation with a short in-memory cache — never in Lambda environment variables.
4. **Watch PDT** if account equity is under $25,000.
5. **Free market data is IEX-only.** If the strategy is sensitive to full-market pricing, budget for a paid data tier — or source data independently of the broker.

### Not recommended now: **Interactive Brokers**

Technically the strongest API of the three and the right answer if the system ever needs options, futures, or non-US markets. But for a Lambda-native architecture the retail gateway requirement is disqualifying: it forces an always-on, 2FA-gated, daily-expiring Java process into the order path. That is a materially worse reliability posture than the strategy itself.

**Revisit if:** IBKR opens OAuth 2.0 `private_key_jwt` to individual account holders (verify with IBKR API Integration — this is actively changing), *or* the instrument set outgrows US equities and justifies operating the sidecar.

### Fidelity: **manual execution only**

There is no supported programmatic path. Treat the Fidelity account as read-only within this system.

If you want Fidelity data in the app, the supported route is **Fidelity Access via an approved aggregator** (Plaid/Akoya) — positions and balances, read-only, no order entry. That is a legitimate integration and worth doing if consolidated portfolio view matters.

For execution, the honest options are:
1. Open an Alpaca account for the algorithmic sleeve and leave Fidelity as the long-term/custody account. **Recommended.**
2. Have the system generate signals and *notify* you, and place Fidelity orders by hand. Viable if trade frequency is low.
3. Transfer assets to a broker with an API. Only if programmatic execution of the *whole* portfolio is genuinely required.

**Do not** use `fidelity-api` or any browser-automation package, in production or in testing. The exposure — live credentials in an automated process, silent breakage on a money path, and account termination risk — is not proportionate to any convenience gained.

---

## Sources

- [Does Fidelity Have an API? — TradersPost blog (updated 2026-08-06)](https://blog.traderspost.io/article/does-fidelity-have-an-api)
- [Fidelity Access and Data Security — fidelity.com](https://www.fidelity.com/security/fidelity-access-data-security)
- [Fidelity Takes Steps to Address Screen Scraping — Fidelity Newsroom (Oct 2023)](https://newsroom.fidelity.com/pressreleases/fidelity-takes-steps-to-address-screen-scraping/s/2f33bc18-f16d-4b66-9868-626ada9ba32b)
- [Fidelity drops the hammer on screen scrapers — RIABiz, 2023-10-19](https://riabiz.com/a/2023/10/19/fidelity-just-dropped-the-hammer-on-screen-scrapers-to-cheers-but-some-firms-like-plaid-are-holdouts-and-the-cfpb-may-wield-the-final-gavel)
- [Fidelity Integration Xchange — Wealthscape](https://integrationxchange.wealthscape.com/)
- [Fidelity Integration Xchange update incl. FIX connectivity — Business Wire, 2020-09-16](https://www.businesswire.com/news/home/20200916005108/en/)
- [Fidelity Updates Wealthscape Platform, Integration Xchange — WealthManagement.com](https://www.wealthmanagement.com/financial-technology/fidelity-updates-wealthscape-platform-integration-xchange)
- [Financial data aggregators incl. Akoya — MX](https://www.mx.com/blog/a-list-of-financial-data-aggregators-in-the-united-states/)
- [fidelity-api — PyPI (UNOFFICIAL)](https://pypi.org/project/fidelity-api/)
- [kennyboy106/fidelity-api — GitHub (UNOFFICIAL, Playwright)](https://github.com/kennyboy106/fidelity-api)
- [Fidelity Trader API — reverse-engineered (UNOFFICIAL)](https://brownjosiah.github.io/fidelity-trader-api/)
- [Alpaca — About Trading API](https://docs.alpaca.markets/us/docs/trading-api)
- [Alpaca — Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)
- [Alpaca — Orders at Alpaca (order types, TIF, fractional restrictions)](https://docs.alpaca.markets/docs/orders-at-alpaca)
- [Alpaca — Using OAuth2 and Trading API](https://docs.alpaca.markets/us/docs/using-oauth2-and-trading-api)
- [Alpaca — WebSocket Stream](https://docs.alpaca.markets/us/docs/streaming-market-data)
- [Alpaca — Market Data plans](https://alpaca.markets/data)
- [Alpaca — Who can apply for a brokerage account?](https://alpaca.markets/support/requirements-alpaca-brokerage-account)
- [Alpaca — Does Alpaca require a minimum deposit?](https://alpaca.markets/support/alpaca-minimum-deposit)
- [IBKR — Web API Documentation (gateway vs OAuth)](https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/)
- [IBKR — Web API Authentication FAQ (5-min timeout, /tickle, daily restart)](https://www.interactivebrokers.com/docs/web-api/authentication/faq)
- [IBKR — Client Portal Web API docs](https://interactivebrokers.github.io/cpwebapi/)
- [IBKR — Pacing Limitations (50 req/s, 10 req/s gateway, 429 penalty box)](https://www.interactivebrokers.com/docs/web-api/trading/usage-and-availability/pacing-limitations)
- [IBKR — TWS API Order Limitations](https://interactivebrokers.github.io/tws-api/order_limitations.html)
- [IBKR — Requesting a Paper Trading Account (funded IBKR Pro required)](https://www.interactivebrokers.com/campus/trading-lessons/request-paper-trading-account/)
- [IBKR — Fractional Trading](https://www.interactivebrokers.com/en/trading/fractional-trading.php)
- [IBKR — Trading API Solutions](https://www.interactivebrokers.com/en/trading/ib-api.php)
- [twsapi group — fractional shares via TWS API (cashQty, SizeIncrement)](https://groups.io/g/twsapi/topic/fractional_shares_via_tws_api/69203949)
- [Voyz/ibeam — unofficial IBKR gateway automation](https://github.com/Voyz/ibeam)
- [IBKR 2FA and 24/7 automated trading — QuietAlpha Lab](https://quietalphalab.com/ibkr-2fa-24-7-automated-trading/)
