"""
CompanyService: assembles company intelligence from providers, persists
it, and serves it. Entirely off the intraday path - nothing in the cycle
or scanner imports this package (enforced by a test).

Everything is lazily fetched on first request and cached in the store:
immutable history never refetched needlessly, derived snapshots reused
while fresh. Provider failures degrade the one section that needed them
and say so; they never fabricate a value.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

from . import crosscheck, dividends as div, earnings as earn
from . import fundamentals as fund, holding_context as hc, peers as pe
from . import compare as cmp_, splits as spl
from .classification import sector_for_sic
from .models import (ActionType, CompanyProfile, CorporateAction,
                     DividendStatus, Provenance)
from .providers.alpaca_corporate_actions import (to_dividend_events,
                                                 to_split_events)
from .store import CompanyStore, HistoryConflict
from .universe import SEED_UNIVERSE

ACTIONS_TTL = 24 * 3600
FUND_TTL = 24 * 3600
HISTORY_YEARS = 10
# How far ahead to look for already-declared corporate actions.
UPCOMING_WINDOW_DAYS = 120
# Bump when normalisation logic changes: cached derived snapshots from an
# older method are then ignored instead of served for another 24 hours.
FUND_METHOD = "fundamentals-v1.3"
CANDIDATE_BUDGET = 25          # new candidate profiles per request
# One company's XBRL facts are ~4.5 MB and ~2,400 observations. Both
# profile() (for the share count) and fundamentals() need them, so the
# normalised result is held per container and the download happens once.
# Bounded so a warm container serving many symbols cannot grow without
# limit.
FACTS_CACHE_MAX = 12
# How many SEC fetches may be in flight. SEC asks for <=10 requests/second
# and these are large, download-bound responses: the limit exists to keep
# the fan-out polite, not to saturate a link.
FETCH_CONCURRENCY = 9


def _now_iso(clock=time.time) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(
        timespec="seconds")


def _action_identity(a: CorporateAction) -> str:
    return "|".join([str(a.type), a.ex_date or a.effective_date or "?",
                     a.event_id or "", str(a.amount), str(a.ratio_new),
                     str(a.ratio_old), str(a.related_symbol)])


class CompanyService:
    def __init__(self, store: CompanyStore, actions, facts, submissions: Callable,
                 price: Callable, bars: Optional[Callable] = None,
                 cik_for: Optional[Callable] = None,
                 today: Callable[[], date] = date.today,
                 clock: Callable[[], float] = time.time,
                 universe: Sequence[str] = SEED_UNIVERSE):
        self.store, self.actions, self.facts = store, actions, facts
        self._submissions, self._price, self._bars = submissions, price, bars
        self._cik_for = cik_for
        self._today, self._clock, self.universe = today, clock, universe
        self._facts_cache: Dict[str, Dict] = {}

    def _company_facts(self, symbol: str) -> Dict:
        """Normalised XBRL facts for one symbol, downloaded at most once
        per container.

        Without this, a peer comparison downloaded each company's 4.5 MB
        payload twice - once for the share count behind market cap and
        again for the fundamentals.
        """
        cached = self._facts_cache.get(symbol)
        if cached is not None:
            return cached
        facts = self.facts.fetch(symbol)
        if len(self._facts_cache) >= FACTS_CACHE_MAX:
            self._facts_cache.pop(next(iter(self._facts_cache)))
        self._facts_cache[symbol] = facts
        return facts

    def warm_facts(self, symbols: Sequence[str]) -> Dict[str, str]:
        """Fetch several companies' facts concurrently into the cache.

        Peer comparison needs the subject plus every peer. Fetching them
        one at a time cost about 4.7 seconds each - 42 seconds for nine
        companies, most of it waiting on the network. Failures are
        returned per symbol rather than raised: one unavailable peer must
        not fail the comparison.
        """
        from concurrent.futures import ThreadPoolExecutor
        todo = [s for s in dict.fromkeys(symbols)
                if s not in self._facts_cache]
        errors: Dict[str, str] = {}
        if not todo:
            return errors

        def one(sym):
            return sym, self.facts.fetch(sym)
        with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
            for future in [pool.submit(one, s) for s in todo]:
                try:
                    sym, facts = future.result()
                except Exception as exc:                  # noqa: BLE001
                    errors[getattr(exc, "symbol", "?")] = \
                        f"{type(exc).__name__}: {exc}"[:160]
                    continue
                if len(self._facts_cache) >= FACTS_CACHE_MAX:
                    self._facts_cache.pop(next(iter(self._facts_cache)))
                self._facts_cache[sym] = facts
        return errors

    # -- freshness of stored snapshots --------------------------------------
    def _fresh(self, symbol, kind, ttl):
        snap = self.store.latest(symbol, kind)
        if not snap:
            return None
        try:
            t = datetime.fromisoformat(snap["stamp"].replace("Z", "+00:00"))
        except ValueError:
            return None
        if self._clock() - t.timestamp() <= ttl:
            return snap["payload"]
        return None

    def _stamp(self):
        # Microseconds: two snapshots of one symbol in the same second
        # (lite then full profile) must not collide on the immutable key.
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat(
            timespec="microseconds")

    def _save(self, symbol, kind, payload):
        self.store.put_snapshot(symbol, kind, payload, self._stamp())

    # -- profile -------------------------------------------------------------
    def _fetch_profile_inputs(self, symbol: str,
                              with_market_cap: bool) -> Dict:
        """Every network read a profile needs, and nothing else.

        Separated from building the profile so a peer fan-out can do the
        waiting concurrently while the store stays written from one
        thread. Failures are carried in the result rather than raised:
        one unavailable company must not fail a peer set.
        """
        out: Dict = {"symbol": symbol, "errors": {}}
        try:
            out["submissions"] = self._submissions(symbol) or {}
        except Exception as exc:                          # noqa: BLE001
            out["submissions"] = {}
            out["errors"]["submissions"] = f"{type(exc).__name__}: {exc}"[:160]
        if with_market_cap:
            try:
                # One small concept call (~10 KB), unless the full facts
                # happen to be cached from a fundamentals request. The
                # whole taxonomy is ~4.5 MB and holds nothing else a
                # market cap needs.
                cached = self._facts_cache.get(symbol)
                if cached is not None:
                    out["shares"] = fund.latest(
                        cached.get("shares_outstanding", []), "INSTANT")
                else:
                    out["shares"] = self.facts.shares_outstanding(symbol)
            except Exception as exc:                      # noqa: BLE001
                out["errors"]["shares"] = f"{type(exc).__name__}: {exc}"[:160]
            try:
                out["price"], out["price_asof"] = self._price(symbol)
            except Exception as exc:                      # noqa: BLE001
                out["errors"]["price"] = f"{type(exc).__name__}: {exc}"[:160]
        return out

    def _build_profile(self, symbol: str, inputs: Dict,
                       with_market_cap: bool) -> CompanyProfile:
        retrieved = _now_iso(self._clock)
        sub = inputs.get("submissions") or {}
        sic = str(sub["sic"]) if sub.get("sic") else None
        p = CompanyProfile(
            symbol=symbol, name=sub.get("name"), cik=sub.get("cik"),
            sic=sic, sic_description=sub.get("sicDescription"),
            sector=sector_for_sic(sic), industry=sub.get("sicDescription"),
            exchange=(sub.get("exchanges") or [None])[0],
            active=True if sub else None,
            provenance=[Provenance("sec", "submissions", retrieved)])
        if with_market_cap:
            sh, price = inputs.get("shares"), inputs.get("price")
            asof = inputs.get("price_asof")
            if sh and price:
                p.shares_outstanding, p.price, p.price_asof = \
                    sh.value, price, asof
                p.market_cap = sh.value * price
                p.market_cap_basis = (
                    f"{sh.value:,.0f} shares (cover page, {sh.period_end}) "
                    f"x price {price} as of {asof}")
                p.provenance.append(Provenance(
                    "sec_companyfacts",
                    "dei:EntityCommonStockSharesOutstanding",
                    retrieved, period=sh.period_end))
            else:
                why = "; ".join(f"{k}: {v}" for k, v in
                                (inputs.get("errors") or {}).items()) \
                    or "no share count reported"
                p.provenance.append(Provenance(
                    "market_cap", f"unavailable: {why}"[:200], retrieved))
        self._save(symbol, "PROFILE", p.as_dict())
        return p

    def profile(self, symbol: str, with_market_cap: bool = True) -> CompanyProfile:
        symbol = symbol.upper()
        cached = self._fresh(symbol, "PROFILE", ACTIONS_TTL)
        if cached and (cached.get("market_cap") is not None
                       or not with_market_cap):
            return _profile_from(cached)
        return self._build_profile(
            symbol, self._fetch_profile_inputs(symbol, with_market_cap),
            with_market_cap)

    def ingest_actions(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        today = self._today()
        start = date(today.year - HISTORY_YEARS, today.month, min(today.day, 28))
        # The window runs PAST today: an already-declared ex-date in the
        # near future is exactly what "next ex-date" means, and a window
        # ending today could never contain one, so the field was always
        # None however many dividends a company had announced.
        end = today + timedelta(days=UPCOMING_WINDOW_DAYS)
        acts = self.actions.fetch(symbol, start.isoformat(), end.isoformat())
        written, conflicts = 0, []
        for a in acts:
            try:
                if self.store.put_fact(symbol, "ACTION", _action_identity(a),
                                       a.as_dict()):
                    written += 1
            except HistoryConflict as e:
                conflicts.append(str(e))
        return {"fetched": len(acts), "written": written,
                "conflicts": conflicts, "window": [start.isoformat(),
                                                   end.isoformat()],
                "retrieved_at": _now_iso(self._clock)}

    def corporate_actions(self, symbol: str, refresh: bool = False) -> Dict:
        symbol = symbol.upper()
        meta = self._fresh(symbol, "ACTIONSYNC", ACTIONS_TTL)
        stored = self.store.facts(symbol, "ACTION")
        err = None
        if refresh or meta is None:
            try:
                meta = self.ingest_actions(symbol)
                self._save(symbol, "ACTIONSYNC", meta)
                stored = self.store.facts(symbol, "ACTION")
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:200]
        return {"symbol": symbol, "actions": stored, "ingest": meta,
                "error": err, "count": len(stored),
                "window_years": HISTORY_YEARS}

    def _action_objects(self, symbol) -> List[CorporateAction]:
        from .models import ActionType as T
        out = []
        for d in self.corporate_actions(symbol)["actions"]:
            prov = d.get("provenance")
            out.append(CorporateAction(
                type=T(d["type"]), symbol=d["symbol"], event_id=d.get("event_id"),
                announcement_date=d.get("announcement_date"),
                ex_date=d.get("ex_date"), record_date=d.get("record_date"),
                payable_date=d.get("payable_date"),
                effective_date=d.get("effective_date"), amount=d.get("amount"),
                currency=d.get("currency"), ratio_new=d.get("ratio_new"),
                ratio_old=d.get("ratio_old"), related_symbol=d.get("related_symbol"),
                special=bool(d.get("special")), detail=d.get("detail") or {},
                provenance=Provenance(**prov) if prov else None))
        return out

    # -- dividends / splits --------------------------------------------------
    def dividends(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        ca = self.corporate_actions(symbol)
        if ca["error"] and not ca["actions"]:
            return {"symbol": symbol, "status": "UNKNOWN", "pays_dividend": None,
                    "reason": "corporate-actions provider unavailable",
                    "error": ca["error"]}
        acts = self._action_objects(symbol)
        events = to_dividend_events(acts)
        today = self._today()
        prof = div.classify(events, today, history_complete=True)
        if prof.status is DividendStatus.NO_DIVIDEND:
            prof.reason = (f"no cash dividends in the {HISTORY_YEARS}-year "
                           "window retrieved from the provider")
        price, asof = (None, None)
        try:
            price, asof = self._price(symbol)
        except Exception:
            pass
        prof = div.with_metrics(prof, events, today, price, asof)
        events_sorted = sorted(events, key=lambda e: e.ex_date, reverse=True)
        years = {e.ex_date[:4] for e in events if e.kind == "REGULAR"}
        prof.years_paid = len(years) or None
        out = prof.as_dict()
        out.update({
            "symbol": symbol,
            "dividend_stock": ("YES" if prof.pays_dividend else
                               "NO" if prof.pays_dividend is False else "UNKNOWN"),
            "history": [{"ex_date": e.ex_date, "amount": e.amount,
                         "kind": e.kind, "pay_date": e.pay_date,
                         "record_date": e.record_date} for e in events_sorted[:40]],
            "special_dividends": [e.ex_date for e in events if e.kind == "SPECIAL"],
            "frequency_days": _median_gap([e for e in events if e.kind == "REGULAR"]),
            "window_years": HISTORY_YEARS,
        })
        self._save(symbol, "DIVPROFILE", out)
        return out

    def splits(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        acts = self._action_objects(symbol)
        events = to_split_events(acts)
        s = spl.summarise(events, self._today())
        s["symbol"] = symbol
        s["timeline"] = [{
            "ex_date": e.ex_date, "type": str(e.type),
            "label": _split_label(e)} for e in
            sorted(events, key=lambda e: e.ex_date, reverse=True)]
        return s

    # -- fundamentals / earnings --------------------------------------------
    def _persist_cited_periods(self, symbol: str, summary: Dict) -> int:
        """Store the periods the summary actually cites, and only those.

        Persisting every XBRL observation meant 19,748 writes for a
        nine-company comparison - about 2,400 per company, none of which
        anything ever read. SEC's XBRL is reproducible from the source at
        any time with full provenance; what needs to be immutable here is
        the period behind a number this system REPORTED.
        """
        cited: List[Dict] = []
        for section in ("income", "balance"):
            for value in (summary.get(section) or {}).values():
                if isinstance(value, dict):
                    cited.append(value)
        for key in ("operating_cash_flow", "capex"):
            value = (summary.get("cash_flow") or {}).get(key)
            if isinstance(value, dict):
                cited.append(value)
        for key in ("ttm_revenue", "ttm_net_income"):
            cited.extend((summary.get(key) or {}).get("contributing") or [])

        written = 0
        for row in cited:
            if not row.get("period_end"):
                continue
            ident = "|".join([str(row.get("concept") or "?"),
                              str(row.get("period_start") or ""),
                              str(row["period_end"]),
                              str(row.get("filed") or ""),
                              str(row.get("accession") or "")])
            try:
                if self.store.put_fact(symbol, "FINPERIOD", ident, row):
                    written += 1
            except HistoryConflict:
                pass
        return written

    def fundamentals(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        cached = self._fresh(symbol, "FUNDSUMMARY", FUND_TTL)
        if cached and cached.get("method") == FUND_METHOD:
            return cached
        try:
            facts = self._company_facts(symbol)
        except Exception as e:
            return {"symbol": symbol, "status": "UNKNOWN",
                    "error": f"{type(e).__name__}: {e}"[:200]}
        s = fund.summarise(facts, self._today())
        s.update({"symbol": symbol, "method": FUND_METHOD,
                  "source": "SEC Company Facts (XBRL)",
                  "retrieved_at": _now_iso(self._clock)})
        s["periods_persisted"] = self._persist_cited_periods(symbol, s)
        self._save(symbol, "FUNDSUMMARY", s)
        return s

    def earnings(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        try:
            facts = self._company_facts(symbol)
        except Exception as e:
            return {"symbol": symbol, "status": "UNKNOWN",
                    "error": f"{type(e).__name__}: {e}"[:200]}
        recs = earn.from_sec(facts)
        t = earn.trends(recs)
        return {"symbol": symbol,
                "source": "SEC XBRL reported values (REPORTED_VALUE)",
                "estimates": "UNAVAILABLE - no consensus-estimate source is "
                             "configured; estimates are never inferred",
                "next_earnings_date": None,
                "next_earnings_note": "no earnings-calendar source configured",
                "records": [{
                    "period_end": r.period_end, "fiscal_period": r.fiscal_period,
                    "eps_actual": r.eps_actual, "eps_estimate": r.eps_estimate,
                    "eps_surprise": r.eps_surprise,
                    "revenue_actual": r.revenue_actual,
                    "revenue_estimate": r.revenue_estimate,
                    "report_date": r.report_date} for r in recs[-12:]],
                "trends": t}

    # -- peers ---------------------------------------------------------------
    def peers(self, symbol: str, refresh: bool = False) -> Dict:
        symbol = symbol.upper()
        subject = self.profile(symbol)
        cands, skipped, todo = [], [], []
        for sym in self.universe:
            if sym == symbol:
                continue
            cached = self._fresh(sym, "PROFILE", 7 * ACTIONS_TTL)
            if cached:
                cands.append(_profile_from(cached))
            elif len(todo) < CANDIDATE_BUDGET:
                todo.append(sym)
            else:
                skipped.append(sym)

        # Candidate profiling was 58 sequential HTTP round trips - 33 of
        # the 88 seconds a cold comparison took, on only 7 MB. The waiting
        # happens concurrently; every store write still comes from this
        # thread.
        if todo:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
                lite_inputs = list(pool.map(
                    lambda sym: self._fetch_profile_inputs(sym, False), todo))
            lites = [self._build_profile(sym, inputs, False)
                     for sym, inputs in zip(todo, lite_inputs)]
            # Only companies in the subject's SIC major group can become
            # peers, so only those need a market cap.
            major = [p for p in lites
                     if p.sic and subject.sic and p.sic[:2] == subject.sic[:2]]
            with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as pool:
                full_inputs = list(pool.map(
                    lambda p: self._fetch_profile_inputs(p.symbol, True),
                    major))
            full = {p.symbol: self._build_profile(p.symbol, inputs, True)
                    for p, inputs in zip(major, full_inputs)}
            cands.extend(full.get(p.symbol, p) for p in lites)
        ps = pe.select(subject, cands)
        out = ps.as_dict()
        out.update({"symbol": symbol,
                    "subject": subject.as_dict(),
                    "candidates_considered": len(cands),
                    "candidates_not_yet_profiled": skipped,
                    "complete": not skipped})
        self._save(symbol, "PEERSET", ps.as_dict())
        return out

    def peer_comparison(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        ps = self.peers(symbol)
        syms = [p["symbol"] for p in ps["peers"]]
        # One concurrent pass for every company's facts, then the
        # summaries are computed from cache. Sequentially this was ~4.7s
        # per company of pure download wait.
        fetch_errors = self.warm_facts([symbol] + syms)
        fs = {s: self.fundamentals(s) for s in [symbol] + syms}

        def metric(sym, name):
            for d in fs[sym].get("derived", []):
                if d.get("metric") == name and isinstance(d.get("value"), (int, float)):
                    return {"value": d["value"], "period_end":
                            d.get("period_end") or d.get("current_period_end")}
            return None
        rows = []
        for name in ("revenue_growth", "gross_margin", "operating_margin",
                     "net_margin", "debt_to_equity", "current_ratio",
                     "return_on_equity"):
            rows.append(cmp_.compare_metric(
                name, metric(symbol, name), {s: metric(s, name) for s in syms}))
        out = {"symbol": symbol, "peers": ps["peers"], "metrics": rows,
               "fetch_errors": fetch_errors,
               "note": "relative facts; periods aligned or excluded"}
        self._save(symbol, "PEERCOMPARE", out)
        return out

    # -- holding context -----------------------------------------------------
    def holding_context(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        d = self.dividends(symbol)
        prof = div.classify(to_dividend_events(self._action_objects(symbol)),
                            self._today(), history_complete=True)
        prof.days_until_ex = d.get("days_until_ex")
        f = self.fundamentals(symbol)
        e = self.earnings(symbol)
        s = self.splits(symbol)
        ctx = hc.holding_context(
            prof if d.get("status") != "UNKNOWN" else None,
            f if "derived" in f else None, e.get("trends"), s, None,
            f.get("freshness", "UNKNOWN"))
        ctx["symbol"] = symbol
        self._save(symbol, "HOLDING", ctx)
        return ctx

    def overview(self, symbol: str) -> Dict:
        symbol = symbol.upper()
        p = self.profile(symbol)
        d = self.dividends(symbol)
        return {"symbol": symbol, "profile": p.as_dict(),
                "dividend_stock": d.get("dividend_stock", "UNKNOWN"),
                "dividend_status": d.get("status"),
                "last_split": self.splits(symbol)["last"],
                "note": "company context is separate from the intraday signal"}


def _profile_from(d: Dict) -> CompanyProfile:
    p = CompanyProfile(**{k: v for k, v in d.items()
                          if k in CompanyProfile.__dataclass_fields__
                          and k != "provenance"})
    p.provenance = [Provenance(**x) for x in d.get("provenance", [])]
    return p


def _median_gap(events) -> Optional[int]:
    ds = sorted(date.fromisoformat(e.ex_date[:10]) for e in events)
    gaps = sorted((b - a).days for a, b in zip(ds[-9:], ds[-8:]))
    return gaps[len(gaps) // 2] if gaps else None


def _split_label(e) -> str:
    """'10-for-1 forward split' / '1-for-10 reverse split'. Direction is in
    words, from the ratio."""
    new, old = e.new_rate, e.old_rate
    def n(x): return f"{x:g}"
    if e.ratio >= 1:
        return f"{n(new)}-for-{n(old)} forward split"
    return f"{n(new)}-for-{n(old)} reverse split"
