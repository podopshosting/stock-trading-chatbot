"""Simulation and research API for the development workbench.

WHY THIS IS A SEPARATE LAMBDA

`stock-agent-dev-api` is guaranteed not to be able to trade, and the
guarantee is an ABSENCE: nothing that can reach a broker is imported, so
no configuration change can make it submit an order. A test scans its
source for broker imports and call sites.

`agent.replay.engine` imports `..broker.paper`. Importing replay into
the read API would therefore put broker code in that Lambda while the
literal source scan kept passing - the guard reads the handler's own
text, not its transitive imports. That is the same trap already found in
the prediction package, and the fix is the same: keep the coupling out
rather than keep the scan happy.

So simulation lives here. This function CAN construct a replay broker,
which is a paper-only simulator that never opens a socket, and it still
cannot reach a venue: no live adapter is imported and no credentials are
read for order submission.

EVERYTHING HERE IS RESEARCH. No endpoint submits an order.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RESEARCH_ONLY = "RESEARCH_ONLY"
SIM_NOTE = ("Simulation and research only. No endpoint in this Lambda "
            "submits a broker order.")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _response(status: int, body: Dict) -> Dict:
    body.setdefault("simulation_only", True)
    body.setdefault("note", SIM_NOTE)
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            # Mirrors the read API: CORS is answered in code rather than
            # by the Function URL config, so there is exactly one place
            # that decides it and no chance of duplicate headers.
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body, default=str),
    }


def _query(event) -> Dict[str, str]:
    return dict(event.get("queryStringParameters") or {})


def _body(event) -> Dict:
    raw = event.get("body")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------------------
# Input validation
#
# Every selectable value is checked against an allow-list built from the
# source of truth, never from a string the caller supplied. A replay
# configuration is executable surface: an unchecked config name is an
# invitation to run something nobody reviewed.
# --------------------------------------------------------------------
def _normalise_symbol(raw) -> Optional[str]:
    import re
    s = str(raw or "").strip().upper()
    return s if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z]{1,2})?", s) else None


ALLOWED_STOP_SOURCES = ("FIXED", "DECISION")
ALLOWED_REGIME_SOURCES = ("NONE", "SYNTHETIC_PERMISSIVE")


def handle_scenarios(event) -> Dict:
    """The canonical scenario library, read from source.

    The UI must not carry its own copy: a duplicated list drifts, and a
    scenario whose name no longer matches its behaviour is worse than
    one that is missing. `ALL` maps a name to a BUILDER, so each entry
    is built to read its own declared expectation.
    """
    from agent.replay import scenarios as sc

    out: List[Dict] = []
    for name in sorted(sc.ALL):
        try:
            s = sc.build(name)
        except Exception as exc:                              # noqa: BLE001
            # A scenario that cannot be built is reported, not skipped.
            # Silently omitting it would make a broken generator look
            # like a scenario nobody wrote.
            out.append({"id": name, "build_error":
                        f"{type(exc).__name__}: {exc}"})
            continue
        guarantees = [g[0] for g in (sc.GUARANTEES.get(name) or [])]
        out.append({
            "id": name,
            "name": getattr(s, "name", name),
            # `question` is what the scenario is FOR; `expectation` is
            # the invariant it asserts. Both are shown because a reader
            # needs the question to judge whether the result answers it.
            "question": getattr(s, "question", None),
            "expectation": getattr(s, "expectation", None),
            "note": getattr(s, "note", None),
            "trade_expectation": getattr(s, "trade", None),
            "regime": getattr(s, "regime", None),
            "spread_pct": getattr(s, "spread_pct", None),
            "may_exceed_1r": getattr(s, "may_exceed_1r", None),
            "expected_failure": getattr(s, "expected_failure", None),
            "config_overrides": getattr(s, "config_overrides", None) or {},
            "bar_count": len(getattr(s, "bars", []) or []),
            # The properties the generated bars are PROVEN to have.
            "guaranteed_properties": guarantees,
        })
    return _response(200, {
        "scenarios": out,
        "count": len(out),
        "source": "agent/replay/scenarios.py",
        "not_evidence": getattr(sc, "SCENARIO_NOT_EVIDENCE", None),
        "guarantee_note": (
            "A guaranteed property is something the generated bars are "
            "verified to have, not a prediction of the outcome. A "
            "scenario PASSES when its intended invariant holds, which is "
            "not the same as making money: gap_through_stop is expected "
            "to lose, and loses correctly."),
    })


def handle_configs(event) -> Dict:
    """Replay configurations, with deployability stated."""
    from agent.replay import configs as cf
    out = []
    for name in sorted(cf.ALL):
        spec = cf.ALL[name]
        deployable = bool(getattr(spec, "deployable", False))
        out.append({
            "id": name,
            "purpose": getattr(spec, "purpose", None),
            # Which guard this configuration exists to make reachable.
            "exposes": getattr(spec, "exposes", None),
            "deployable": deployable,
            "scenario": getattr(spec, "scenario", None),
            "limit_overrides": getattr(spec, "limit_overrides", None) or {},
            "engine_overrides": getattr(spec, "engine_overrides", None) or {},
            "note": getattr(spec, "note", None),
            "label": None if deployable else cf.TEST_ONLY_LABEL,
        })
    return _response(200, {
        "configs": out,
        "count": len(out),
        "deployable": cf.deployable_names(),
        "note": ("Only a deployable config describes the strategy as it "
                 "would actually run. A " + cf.TEST_ONLY_LABEL + " config "
                 "exists to make one guard reachable and must never be "
                 "read as a result about the live strategy."),
    })


def handle_hypothetical(event) -> Dict:
    """A hypothetical trade: arithmetic plus a Risk Governor opinion.

    This is a calculator. It does not consult the strategy, does not
    create a hypothesis, and cannot place an order. It answers "what
    would have happened to $N here under shock X", which the normal
    pipeline cannot answer because the normal pipeline refuses to trade
    most things.

    The Risk Governor section is clearly separated from the arithmetic:
    whether the governor WOULD have approved a setup is a different
    question from what the setup pays out.
    """
    body = _body(event)
    symbol = _normalise_symbol(body.get("symbol"))
    if not symbol:
        return _response(400, {"error": "symbol must be a ticker, 1-5 letters"})

    def num(key, default=None, lo=None, hi=None):
        v = body.get(key, default)
        if v is None:
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        if lo is not None and v < lo:
            raise ValueError(f"{key} must be >= {lo}")
        if hi is not None and v > hi:
            raise ValueError(f"{key} must be <= {hi}")
        return v

    try:
        entry = num("entry_price", lo=0.0001, hi=1e7)
        notional = num("notional", 25.0, lo=0.01, hi=100000.0)
        stop_pct = num("stop_pct", 3.0, lo=0.01, hi=90.0)
        target_pct = num("target_pct", None, lo=0.01, hi=900.0)
        shock_pct = num("shock_pct", 0.0, lo=-99.0, hi=900.0)
        slippage_bps = num("slippage_bps", 5.0, lo=0.0, hi=1000.0)
    except ValueError as exc:
        return _response(400, {"error": str(exc)})

    if entry is None:
        return _response(400, {
            "error": "entry_price is required",
            "why": ("a hypothetical with no entry price has no answer, and "
                    "guessing one from a live quote would silently change "
                    "the question being asked"),
        })
    if target_pct is None:
        # Live couples the target to the stop at 2x. Mirrored, and
        # labelled, so a caller who did not choose a target gets the
        # policy rather than a made-up number.
        target_pct = stop_pct * 2.0
        target_source = "DERIVED_2X_STOP"
    else:
        target_source = "CALLER_SUPPLIED"

    # First executable price: the fill, shocked and slipped. A gap is
    # applied BEFORE slippage because a gap moves the market and
    # slippage is paid on top of wherever the market then is.
    gapped = entry * (1.0 + shock_pct / 100.0)
    fill = gapped * (1.0 + slippage_bps / 10000.0)
    quantity = notional / fill if fill > 0 else 0.0

    stop_price = entry * (1.0 - stop_pct / 100.0)
    target_price = entry * (1.0 + target_pct / 100.0)
    planned_risk = notional * (stop_pct / 100.0)

    # What actually happens. If the shock has already carried price
    # through the stop, the stop cannot fill at the stop - it fills at
    # the first executable price, and the difference is the breach.
    stop_breached = gapped <= stop_price
    target_reached = gapped >= target_price
    if stop_breached:
        exit_price, outcome = fill, "STOP_GAPPED_THROUGH"
    elif target_reached:
        exit_price, outcome = fill, "TARGET_GAPPED_THROUGH"
    else:
        exit_price, outcome = None, "NEITHER_LEVEL_REACHED"

    if exit_price is None:
        realised = None
        realised_r = None
    else:
        # Quantity bought at `fill`, valued at `exit_price`. With a pure
        # gap and no later movement those are the same price, so the
        # loss is measured against the PLANNED entry, which is what the
        # trader thought they were getting.
        realised = (exit_price - entry) * quantity
        realised_r = (realised / planned_risk) if planned_risk > 0 else None

    # Risk Governor opinion on the SETUP, not the outcome. Evaluated
    # against the live limits so the answer is about the real policy.
    from agent.risk import RiskLimits
    limits = RiskLimits()
    governor: Dict[str, Any] = {
        "evaluated_against": "live RiskLimits defaults",
        "limits_version": getattr(limits, "version", "unknown"),
    }
    checks = []
    max_trade_risk = getattr(limits, "max_trade_risk", None)
    if max_trade_risk is not None:
        checks.append(("planned risk within max_trade_risk",
                       planned_risk <= max_trade_risk,
                       f"planned ${planned_risk:.2f} vs limit "
                       f"${max_trade_risk:.2f}"))
    daily = getattr(limits, "daily_capital_limit", None)
    if daily is not None:
        checks.append(("notional within daily capital limit",
                       notional <= daily,
                       f"${notional:.2f} vs ${daily:.2f}"))
    min_pos = getattr(limits, "min_position_value", None)
    if min_pos is not None:
        checks.append(("notional at least min_position_value",
                       notional >= min_pos,
                       f"${notional:.2f} vs ${min_pos:.2f}"))
    max_pct = getattr(limits, "max_position_pct_of_daily", None)
    if max_pct is not None and daily:
        checks.append(("notional within max_position_pct_of_daily",
                       notional <= daily * max_pct,
                       f"${notional:.2f} vs ${daily * max_pct:.2f}"))
    governor["checks"] = [
        {"check": c[0], "passed": bool(c[1]), "detail": c[2]} for c in checks
    ]
    governor["would_the_setup_be_sizeable"] = all(c[1] for c in checks)
    governor["caveat"] = (
        "This is the Risk Governor's view of the SETUP's size and risk "
        "only. It is NOT the strategy's decision: the strategy also "
        "requires an actionable hypothesis, a permitting regime, fresh "
        "data, a tight spread and sufficient liquidity, none of which a "
        "hypothetical supplies.")

    return _response(200, {
        "mode": "HYPOTHETICAL_TRADE",
        "symbol": symbol,
        "plan": {
            "entry_price": round(entry, 6),
            "notional": round(notional, 2),
            "estimated_quantity": round(quantity, 8),
            "stop_price": round(stop_price, 6),
            "target_price": round(target_price, 6),
            "stop_pct": stop_pct,
            "target_pct": target_pct,
            "target_source": target_source,
            "planned_risk_dollars": round(planned_risk, 4),
            "planned_r": 1.0,
        },
        "scenario": {
            "shock_pct": shock_pct,
            "slippage_bps": slippage_bps,
            "gapped_price": round(gapped, 6),
            "first_executable_price": round(fill, 6),
            "slippage_dollars": round((fill - gapped) * quantity, 4),
        },
        "result": {
            "outcome": outcome,
            "exit_price": None if exit_price is None else round(exit_price, 6),
            "realised_dollars": None if realised is None else round(realised, 4),
            "realised_r": None if realised_r is None else round(realised_r, 4),
            "stop_held": (None if exit_price is None
                          else bool(realised_r is not None
                                    and realised_r >= -1.0)),
            "target_hit": bool(target_reached),
            "position_remaining": exit_price is None,
            "capital_consumed": round(notional, 2),
            "unresolved_note": (
                None if exit_price is None else
                "Measured at the first executable price. A level the shock "
                "carried price through cannot fill at that level, and the "
                "difference is the breach."),
        },
        "risk_governor_analysis": governor,
        "strategy_decision": {
            "consulted": False,
            "why": ("A hypothetical does not run the strategy. Use "
                    "/agent/pipeline?symbol= for the actual decision."),
        },
    })



# --------------------------------------------------------------------
# Replay persistence
#
# Stored in the existing journal table under a REPLAYRUN# partition,
# the same pattern as EXTORDERS#. No new infrastructure, and the role
# already has access.
#
# A run is persisted BEFORE its performance is returned, and the
# assumptions are stored in the same item as the numbers. The whole
# reason this exists is that a reported figure could not be re-derived:
# ReplayResult carried its own config, nothing wrote it down, and the
# shell that produced it exited.
# --------------------------------------------------------------------
RUN_INDEX_PK = "REPLAYRUN#INDEX"


def _table():
    import boto3
    name = os.environ.get("JOURNAL_TABLE", "stock-agent-dev-journal")
    return boto3.resource("dynamodb", region_name=os.environ.get(
        "AWS_REGION", "us-east-2")).Table(name)


def _code_sha() -> str:
    """The deployed code identity, or UNKNOWN.

    Set at deploy time. UNKNOWN rather than a guess: a run attributed to
    the wrong commit is worse than one attributed to nothing, because it
    looks like provenance.
    """
    return os.environ.get("CODE_SHA") or "UNKNOWN"


def _decimalise(obj):
    """Floats are not storable in DynamoDB; store them as strings.

    Strings rather than Decimal because a round-trip through Decimal
    silently re-ranks values that differ below its precision, and these
    are research numbers that get compared.
    """
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, dict):
        return {k: _decimalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_decimalise(v) for v in obj]
    return obj


def handle_replay(event) -> Dict:
    """Run a replay and persist it. Returns the run_id.

    Two modes. SCENARIO uses the canonical synthetic library. HISTORICAL
    fetches real bars for one symbol. Everything selectable is checked
    against an allow-list built from source, because a replay
    configuration is executable surface.
    """
    import uuid
    body = _body(event)
    mode = str(body.get("mode") or "SCENARIO").upper()
    if mode not in ("SCENARIO", "HISTORICAL"):
        return _response(400, {"error": "mode must be SCENARIO or HISTORICAL"})

    stop_source = str(body.get("stop_source") or "FIXED").upper()
    if stop_source not in ALLOWED_STOP_SOURCES:
        return _response(400, {"error": "stop_source must be one of "
                                        + str(list(ALLOWED_STOP_SOURCES))})

    from agent.replay import scenarios as sc
    from agent.replay import configs as cf
    from agent.replay.engine import ReplayConfig, run as replay_run

    config_name = str(body.get("config") or cf.DEFAULT_NAME)
    if config_name not in cf.ALL:
        return _response(400, {"error": "unknown config",
                               "allowed": sorted(cf.ALL)})
    spec = cf.ALL[config_name]

    kwargs: Dict[str, Any] = {"stop_source": stop_source}
    regime_for = None
    regime_source = "NONE"
    regime_detail = None
    dataset_id = None
    dataset_checksum = None
    bars: Dict[str, Any] = {}
    scenario_name = None
    symbol = None

    if mode == "SCENARIO":
        scenario_name = str(body.get("scenario") or "")
        if scenario_name not in sc.ALL:
            return _response(400, {"error": "unknown scenario",
                                   "allowed": sorted(sc.ALL)})
        scenario = sc.build(scenario_name)
        # Already a {symbol: [Bar]} mapping. Wrapping it again produced
        # a dict whose "bars" were strings, which the engine read as
        # timestamps - a confusing AttributeError for a shape error.
        bars = scenario.bars
        # The scenario declares its own regime; using anything else
        # would test a different scenario than the one named.
        regime_for = sc.regime_for(scenario)
        regime_source = "SCENARIO_DECLARED"
        regime_detail = f"regime declared by scenario {scenario_name}"
        if scenario.spread_pct is not None:
            kwargs["spread_pct_override"] = scenario.spread_pct
        dataset_id = f"scenario:{scenario_name}"
        dataset_checksum = "N/A_SYNTHETIC"
    else:
        symbol = _normalise_symbol(body.get("symbol"))
        if not symbol:
            return _response(400, {"error": "symbol must be a ticker"})
        try:
            days = int(body.get("days", 250))
        except (TypeError, ValueError):
            return _response(400, {"error": "days must be an integer"})
        if not 20 <= days <= 2000:
            return _response(400, {"error": "days must be between 20 and 2000"})
        timeframe = str(body.get("timeframe") or "1day").lower()
        intervals = {"1day": 86400.0, "1hour": 3600.0, "15min": 900.0,
                     "5min": 300.0, "1min": 60.0}
        if timeframe not in intervals:
            return _response(400, {"error": "unknown timeframe",
                                   "allowed": sorted(intervals)})
        kwargs["bar_interval_seconds"] = intervals[timeframe]

        from agent.providers import AlpacaProvider
        from agent.replay.data import Bar
        try:
            # Credentials from Secrets Manager, the same way the live
            # agent and the read API read them. Never from environment
            # variables and never logged: an error message that carries
            # a key is worse than no error message.
            import boto3
            from agent.config import AgentConfig
            cfg = AgentConfig()
            sm = boto3.client("secretsmanager",
                              region_name=cfg.storage.region)
            creds = json.loads(sm.get_secret_value(
                SecretId=cfg.storage.alpaca_secret_id)["SecretString"])
            provider = AlpacaProvider(
                api_key_id=creds["api_key_id"],
                api_secret_key=creds["api_secret_key"],
                quote_feed=os.environ.get("ALPACA_QUOTE_FEED", "sip"))
            raw = provider.get_bars(symbol, timeframe=timeframe, limit=days)
        except Exception as exc:                              # noqa: BLE001
            # The DATA failed, not the engine. Reported as such so the
            # reader does not go looking for a replay fault.
            return _response(502, {
                "error": f"could not load history for {symbol}: "
                         f"{type(exc).__name__}: {exc}",
                "distinction": ("the replay engine is fine; the market "
                                "data could not be fetched"),
            })
        # get_bars returns a BarSet, whose own Bar type is NOT the
        # replay Bar - same five fields, different class, and the
        # provider's carries provenance the engine has no use for.
        # Unwrapped via .bars and converted explicitly, so a change to
        # either shape fails HERE rather than somewhere downstream. The
        # first version iterated the BarSet directly and got
        # "'BarSet' object is not iterable".
        source = getattr(raw, "bars", None)
        if source is None:
            return _response(502, {
                "error": f"the provider returned {type(raw).__name__}, "
                         f"which has no .bars"})
        provenance = getattr(raw, "provenance", None)
        series = []
        for b in source:
            try:
                series.append(Bar(
                    timestamp=str(b.timestamp),
                    open=float(b.open), high=float(b.high),
                    low=float(b.low), close=float(b.close),
                    volume=float(b.volume or 0.0)))
            except (AttributeError, TypeError, ValueError) as exc:
                return _response(502, {
                    "error": f"a bar could not be converted: {exc}"})
        if len(series) < 30:
            return _response(422, {
                "error": f"only {len(series)} bars available for {symbol}",
                "why": ("a replay needs warmup before any decision means "
                        "anything; a short series would produce decisions "
                        "taken on indicators computed from nothing")})
        bars = {symbol: series}

        # Real bars, but the strategy is regime-gated and refuses
        # everything without a regime. The caller chooses, and the
        # choice is recorded on the run, because a permissive regime
        # DISABLES the market filter and every number downstream is
        # conditioned on that.
        regime_source = str(body.get("regime_source") or "NONE").upper()
        if regime_source not in ALLOWED_REGIME_SOURCES:
            return _response(400, {
                "error": "regime_source must be one of "
                         + str(list(ALLOWED_REGIME_SOURCES))})
        if regime_source == "SYNTHETIC_PERMISSIVE":
            def regime_for(_sym, _clock):                     # noqa: ARG001
                return {"regime": "BULLISH", "regime_confidence": 0.8,
                        "risk_posture": "NORMAL", "market_session": "OPEN"}
            regime_detail = (
                "regime forced to BULLISH/0.8, posture NORMAL, session OPEN "
                "on EVERY bar - the long-only regime gate is DISABLED, so "
                "this run describes the strategy without its market filter")
        dataset_id = f"alpaca:{symbol}:{timeframe}:{len(series)}"
        provider_provenance = str(provenance) if provenance else None
        import hashlib
        h = hashlib.sha256()
        for b in series:
            h.update(f"{b.timestamp}|{b.open}|{b.high}|{b.low}|{b.close}|"
                     f"{b.volume}".encode())
        dataset_checksum = h.hexdigest()[:32]

    # Build the config: spec overrides first, then the request's.
    limits = None
    if getattr(spec, "limit_overrides", None):
        from agent.risk import RiskLimits
        limits = RiskLimits(**spec.limit_overrides)
    engine_over = dict(getattr(spec, "engine_overrides", None) or {})
    spread_override = kwargs.pop("spread_pct_override", None)
    cfg_kwargs = dict(engine_over)
    cfg_kwargs.update(kwargs)
    if limits is not None:
        cfg_kwargs["risk_limits"] = limits
    try:
        config = ReplayConfig(**cfg_kwargs)
    except TypeError as exc:
        return _response(400, {"error": f"config rejected: {exc}"})

    # A scenario may require specific engine settings to make its point.
    # Applied by setattr, matching scripts/replay.py, and REFUSED if the
    # field does not exist: silently ignoring an override would run a
    # different scenario than the one named.
    for key, value in (getattr(scenario, "config_overrides", None) or {}
                       ).items() if mode == "SCENARIO" else []:
        if not hasattr(config, key):
            return _response(500, {
                "error": f"scenario {scenario_name} overrides unknown "
                         f"config field {key!r}"})
        setattr(config, key, value)

    run_kwargs: Dict[str, Any] = {
        "dataset_id": dataset_id,
        "dataset_checksum": dataset_checksum,
        "config_name": config_name,
        "config_deployable": bool(getattr(spec, "deployable", False)),
        "regime_source": regime_source,
        "regime_detail": regime_detail,
    }
    if regime_for is not None:
        run_kwargs["regime_for"] = regime_for
    if spread_override is not None:
        run_kwargs["spread_pct"] = spread_override

    result = replay_run(bars, config, **run_kwargs)

    run_id = "rpl_" + uuid.uuid4().hex[:16]
    created = _now()
    d = result.as_dict()
    perf = d.get("performance") or {}
    stop_integrity = perf.get("stop_integrity") or {}

    # Assumptions first, in the record itself, so a stored number can
    # never be read without the conditions that produced it.
    assumptions = {
        "run_id": run_id,
        "created_at": created,
        "mode": mode,
        "symbol": symbol,
        "scenario": scenario_name,
        "dataset_id": dataset_id,
        "dataset_checksum": dataset_checksum,
        "provider_provenance": locals().get("provider_provenance"),
        "code_sha": _code_sha(),
        "regime_source": regime_source,
        "regime_is_synthetic": d.get("regime_is_synthetic", True),
        "regime_detail": regime_detail,
        "stop_model": stop_source,
        "config_name": config_name,
        "config_deployable": bool(getattr(spec, "deployable", False)),
        "config_label": (None if getattr(spec, "deployable", False)
                         else cf.TEST_ONLY_LABEL),
        "execution_model": ("decision on bar N fills at bar N+1 open; "
                            f"slippage {config.slippage_bps} bps"),
        "valid": d.get("valid"),
        "void_detail": d.get("lookahead_detail"),
    }
    summary = {
        "trades": perf.get("trades_counted"),
        "trades_excluded_unknown": perf.get("trades_excluded_unknown"),
        "net_pnl": perf.get("total_net_pnl"),
        "verdict": perf.get("verdict"),
        "entries_filled": d.get("entries_filled"),
        "exits_filled": d.get("exits_filled"),
        "stop_breaches": stop_integrity.get("stop_breaches"),
        "breach_rate": stop_integrity.get("breach_rate"),
        "worst_r": stop_integrity.get("worst_r"),
        "breached_symbols": stop_integrity.get("breached_symbols"),
        "contested_bars": d.get("contested_bars"),
        "rejections": d.get("rejections"),
    }

    item = {"PK": f"REPLAYRUN#{run_id}", "SK": "RESULT",
            "assumptions": assumptions, "summary": summary,
            "result": d}
    index = {"PK": RUN_INDEX_PK, "SK": f"{created}#{run_id}",
             "assumptions": assumptions, "summary": summary}
    persisted, persist_error = False, None
    try:
        tbl = _table()
        tbl.put_item(Item=_decimalise(item))
        tbl.put_item(Item=_decimalise(index))
        persisted = True
    except Exception as exc:                                  # noqa: BLE001
        persist_error = f"{type(exc).__name__}: {exc}"

    return _response(200, {
        "run_id": run_id,
        "persisted": persisted,
        "persist_error": persist_error,
        # If it could not be stored it is not history, and saying so
        # beside the number is the whole point of this endpoint.
        "persistence_warning": (None if persisted else
            "NOT PERSISTED - this result cannot be re-derived later and "
            "must not be quoted as a recorded run"),
        "assumptions": assumptions,
        "summary": summary,
        "allocations": d.get("allocations"),
        "warnings": d.get("warnings"),
    })


def handle_runs(event) -> Dict:
    """Persisted runs, newest first."""
    params = _query(event)
    try:
        limit = max(1, min(100, int(params.get("limit", 25))))
    except (TypeError, ValueError):
        limit = 25
    try:
        tbl = _table()
        resp = tbl.query(
            KeyConditionExpression=(
                __import__("boto3").dynamodb.conditions.Key("PK").eq(
                    RUN_INDEX_PK)),
            ScanIndexForward=False, Limit=limit)
        rows = resp.get("Items") or []
    except Exception as exc:                                  # noqa: BLE001
        # A read failure is not an empty list. An empty list would say
        # "no runs exist", which is a different and unverified claim.
        return _response(200, {"runs": [], "count": None,
                               "read_error": f"{type(exc).__name__}: {exc}",
                               "warning": ("runs could not be read; this is "
                                           "NOT evidence that none exist")})
    out = [{"assumptions": r.get("assumptions"), "summary": r.get("summary")}
           for r in rows]
    return _response(200, {"runs": out, "count": len(out), "read_error": None})


def handle_run_detail(event) -> Dict:
    run_id = _query(event).get("run_id") or ""
    if not run_id.startswith("rpl_"):
        return _response(400, {"error": "run_id must look like rpl_..."})
    try:
        resp = _table().get_item(Key={"PK": f"REPLAYRUN#{run_id}",
                                      "SK": "RESULT"})
    except Exception as exc:                                  # noqa: BLE001
        return _response(200, {"found": None, "run_id": run_id,
                               "read_error": f"{type(exc).__name__}: {exc}"})
    item = resp.get("Item")
    if not item:
        return _response(404, {"found": False, "run_id": run_id})
    return _response(200, {"found": True, "run_id": run_id,
                           "assumptions": item.get("assumptions"),
                           "summary": item.get("summary"),
                           "result": item.get("result")})



# --------------------------------------------------------------------
# Research predictions
#
# RESEARCH_ONLY throughout. The prediction package may not import the
# broker, risk, hypothesis, position, scanner or orchestration modules
# and a test reads its source to prove it, so nothing reachable from
# here can place an order.
# --------------------------------------------------------------------
PREDICTION_PARTITION = "PREDICTION"


def _load_bars_for(symbol: str, days: int, timeframe: str):
    """Real bars, or (None, reason). Never raises.

    A provider failure is DATA failing, not the predictor failing, and
    the caller needs to be told which.
    """
    intervals = {"1day": 86400.0, "1hour": 3600.0, "15min": 900.0,
                 "5min": 300.0, "1min": 60.0}
    if timeframe not in intervals:
        return None, f"unknown timeframe {timeframe!r}", None
    try:
        import boto3
        from agent.config import AgentConfig
        from agent.providers import AlpacaProvider
        from agent.replay.data import Bar
        cfg = AgentConfig()
        sm = boto3.client("secretsmanager", region_name=cfg.storage.region)
        creds = json.loads(sm.get_secret_value(
            SecretId=cfg.storage.alpaca_secret_id)["SecretString"])
        provider = AlpacaProvider(
            api_key_id=creds["api_key_id"],
            api_secret_key=creds["api_secret_key"],
            quote_feed=os.environ.get("ALPACA_QUOTE_FEED", "sip"))
        raw = provider.get_bars(symbol, timeframe=timeframe, limit=days)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}", None
    source = getattr(raw, "bars", None)
    if source is None:
        return None, f"provider returned {type(raw).__name__}", None
    bars = []
    for b in source:
        try:
            bars.append(Bar(timestamp=str(b.timestamp), open=float(b.open),
                            high=float(b.high), low=float(b.low),
                            close=float(b.close),
                            volume=float(b.volume or 0.0)))
        except (AttributeError, TypeError, ValueError) as exc:
            return None, f"a bar could not be converted: {exc}", None
    if not bars:
        return None, "the provider returned no bars", None
    import hashlib
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.timestamp}|{b.open}|{b.high}|{b.low}|{b.close}|"
                 f"{b.volume}".encode())
    return bars, None, h.hexdigest()[:32]


def handle_predict(event) -> Dict:
    """Generate research predictions for one symbol, and persist them.

    Predictions are made at the LAST bar, which is the only point a
    caller can act on - and because its horizon has not elapsed, every
    one of them is PENDING. That is the honest state for a forecast
    about the future, and it is reported as such rather than scored.

    Backfilled predictions over earlier bars are generated too, so the
    card can show whether the models have ever been right. Those mature
    immediately because their horizons are in the past.
    """
    body = _body(event)
    symbol = _normalise_symbol(body.get("symbol"))
    if not symbol:
        return _response(400, {"error": "symbol must be a ticker"})
    try:
        days = int(body.get("days", 250))
    except (TypeError, ValueError):
        return _response(400, {"error": "days must be an integer"})
    if not 60 <= days <= 2000:
        return _response(400, {
            "error": "days must be between 60 and 2000",
            "why": ("features need history before they mean anything; "
                    "a shorter series produces numbers rather than "
                    "features")})
    timeframe = str(body.get("timeframe") or "1day").lower()

    bars, why, checksum = _load_bars_for(symbol, days, timeframe)
    if bars is None:
        return _response(502, {
            "error": f"could not load history for {symbol}: {why}",
            "distinction": ("the predictor is fine; the market data "
                            "could not be fetched")})

    from agent.prediction import features as PF
    from agent.prediction import maturation as PM
    from agent.prediction import metrics as PMET
    from agent.prediction import models as PMOD

    interval = {"1day": 86400.0, "1hour": 3600.0, "15min": 900.0,
                "5min": 300.0, "1min": 60.0}[timeframe]
    # Horizons expressed in MINUTES, as the targets module requires, so
    # on daily bars one bar ahead is 1440 minutes. Stating the bar
    # multiple too, because "a 1440-minute horizon" on daily data is
    # easy to misread as intraday.
    horizons = [int(round(interval / 60.0 * n)) for n in (1, 2, 5)]
    dataset_id = f"alpaca:{symbol}:{timeframe}:{len(bars)}"

    live: List[Dict] = []
    backfill: List[Any] = []
    warmup = 60

    # The live prediction, at the last bar.
    record = PF.build(bars, len(bars) - 1, symbol, dataset_id=dataset_id,
                      dataset_checksum=checksum)
    if record is None:
        return _response(422, {
            "error": "no feature record could be built at the last bar"})
    for p in PMOD.predict_all_baselines(record, generated_at=_now(),
                                        horizons=horizons):
        d = p.to_dict()
        d["status"] = PMOD.PENDING
        d["horizon_bars"] = round(p.horizon_minutes * 60.0 / interval, 2)
        live.append(d)

    # Backfill, so the card can show a track record rather than only a
    # fresh unanswerable claim.
    for i in range(warmup, len(bars) - 6):
        r = PF.build(bars, i, symbol, dataset_id=dataset_id,
                     dataset_checksum=checksum)
        if r is None:
            continue
        backfill.extend(PMOD.predict_all_baselines(
            r, generated_at=_now(), horizons=horizons))
    outcomes = PM.mature_all(backfill, {symbol: bars},
                             evaluated_at=_now(),
                             bar_interval_seconds=interval,
                             dataset_checksum=checksum)
    report = PMET.report(backfill, outcomes)

    persisted, persist_error = False, None
    pred_id = "predrun_" + (checksum or "unknown")[:16]
    try:
        tbl = _table()
        tbl.put_item(Item=_decimalise({
            "PK": f"{PREDICTION_PARTITION}#{symbol}",
            "SK": f"{_now()}#{pred_id}",
            "mode": RESEARCH_ONLY,
            "symbol": symbol,
            "dataset_id": dataset_id,
            "dataset_checksum": checksum,
            "code_sha": _code_sha(),
            "feature_schema_version": PF.FEATURE_SCHEMA_VERSION,
            "model_version": PMOD.MODEL_SCHEMA_VERSION,
            "live_predictions": live,
            "backfill_report": report,
        }))
        persisted = True
    except Exception as exc:                                  # noqa: BLE001
        persist_error = f"{type(exc).__name__}: {exc}"

    return _response(200, {
        "mode": RESEARCH_ONLY,
        "execution_influence": (
            "NONE - predictions do not affect scanner selection, "
            "hypotheses, the Risk Governor, sizing, exits or any order"),
        "symbol": symbol,
        "generated_at": _now(),
        "dataset_id": dataset_id,
        "dataset_checksum": checksum,
        "code_sha": _code_sha(),
        "feature_schema_version": PF.FEATURE_SCHEMA_VERSION,
        "model_version": PMOD.MODEL_SCHEMA_VERSION,
        "bar_interval_seconds": interval,
        "horizons_minutes": horizons,
        "feature_record": record.to_dict(),
        "live_predictions": live,
        "live_note": (
            "Every live prediction is PENDING: its horizon has not "
            "elapsed. A forecast about the future has no outcome yet, "
            "and scoring one would require knowing the answer."),
        "backfill": {
            "predictions": len(backfill),
            "report": report,
            "note": ("Backfilled over earlier bars, which mature "
                     "immediately because their horizons are in the "
                     "past. This is the track record; the live "
                     "prediction above is not evidence of anything."),
        },
        "persisted": persisted,
        "persist_error": persist_error,
    })


def handle_prediction_runs(event) -> Dict:
    """Persisted prediction runs for one symbol."""
    symbol = _normalise_symbol(_query(event).get("symbol"))
    if not symbol:
        return _response(400, {"error": "symbol must be a ticker"})
    try:
        import boto3
        resp = _table().query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(
                f"{PREDICTION_PARTITION}#{symbol}"),
            ScanIndexForward=False, Limit=10)
        rows = resp.get("Items") or []
    except Exception as exc:                                  # noqa: BLE001
        # A read failure is not an empty list: an empty list would claim
        # no run exists, which is a different and unverified statement.
        return _response(200, {"runs": [], "count": None,
                               "read_error": f"{type(exc).__name__}: {exc}",
                               "warning": ("runs could not be read; this "
                                           "is NOT evidence that none "
                                           "exist")})
    return _response(200, {"runs": rows, "count": len(rows),
                           "read_error": None, "mode": RESEARCH_ONLY})


ROUTES = {
    ("GET", "/sim/scenarios"): handle_scenarios,
    ("GET", "/sim/configs"): handle_configs,
    ("POST", "/sim/hypothetical"): handle_hypothetical,
    ("POST", "/sim/replay"): handle_replay,
    ("GET", "/sim/runs"): handle_runs,
    ("GET", "/sim/run"): handle_run_detail,
    ("POST", "/sim/predict"): handle_predict,
    ("GET", "/sim/predictions"): handle_prediction_runs,
}


def handler(event, context=None):           # noqa: ARG001
    method = ((event.get("requestContext") or {}).get("http") or {}).get(
        "method") or event.get("httpMethod") or "GET"
    path = (((event.get("requestContext") or {}).get("http") or {}).get("path")
            or event.get("rawPath") or event.get("path") or "/")
    path = path.rstrip("/") or "/"

    if method == "OPTIONS":
        return _response(200, {"ok": True})
    if path in ("/", "/health"):
        return _response(200, {
            "service": "stock-agent-dev-sim",
            "prediction_mode": RESEARCH_ONLY,
            "routes": sorted(f"{m} {p}" for m, p in ROUTES),
            "generated_at": _now(),
        })

    fn = ROUTES.get((method, path))
    if fn is None:
        return _response(404, {"error": "no such route",
                               "method": method, "path": path,
                               "routes": sorted(f"{m} {p}" for m, p in ROUTES)})
    try:
        return fn(event)
    except Exception as exc:                                  # noqa: BLE001
        # The type and message, not a traceback: a simulation failure is
        # a research problem and the caller needs to know WHICH input
        # could not be honoured, not where in the stack it surfaced.
        return _response(500, {"error": f"{type(exc).__name__}: {exc}",
                               "path": path})
