"""Bounded offline-first market-data feasibility probes for F01 Research."""

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from trading_system.data.market_sessions import trading_sessions_between

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
NY = ZoneInfo("America/New_York")
MAX_BODY = 128 * 1024
STATUS = "HISTORICAL_MARKET_FEASIBILITY_ASSESSED"
SOURCES = {
    "f01_historical_security_50_issuer_preflight_audit.md": (
        "e757ba1619698c97476d11dbedfe25c91b90e5bf3ff78f4d0f06d099001fc1f5"
    ),
    "f01_historical_50_preflight.json": (
        "2c47e3da2bd21c03cc262232e85d775c0fde7190d49149656dfe72d8c3fc9752"
    ),
    "f01_historical_50_pilot.csv": (
        "75627e9032cf0dd1410d9ad93663a3248a22df1998b394d7aff892df6fbe0955"
    ),
    "f_2022_massive_identity_pilot_v1_observations.json": (
        "deaf0541803eb01be95e636de1b7a3c654d4a269c3335725db9983b2653aeaf7"
    ),
    "f_2022_massive_identity_pilot_v1_summary.json": (
        "b5221c7e8070ee0f73859e9e4fa90b1bf8ff7cbfc7832803a6d974e54df2c821"
    ),
    "f_forward_sip_shadow_contract_summary.json": (
        "8acff6d2aa0125a4a5228ba757c3ecba7ed29f5e1fdf723d55d554268d72a1d6"
    ),
}
DOCS = {
    "alpaca_basic": "https://docs.alpaca.markets/us/v1.1/docs/about-market-data-api",
    "alpaca_sip": "https://docs.alpaca.markets/us/v1.1/docs/market-data-faq",
    "alpaca_bars": "https://docs.alpaca.markets/us/v1.4.2/reference/stockbarsingle-1",
    "alpaca_actions": "https://docs.alpaca.markets/us/reference/corporateactions-1",
    "alpaca_assets": "https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id",
    "massive_basic": (
        "https://massive.com/knowledge-base/article/how-much-historical-stock-data-does-massive-have"
    ),
    "massive_reference": (
        "https://massive.com/knowledge-base/article/does-massive-have-a-downloadable-security-master"
    ),
    "massive_all_tickers": "https://massive.com/docs/rest/stocks/tickers/all-tickers",
    "massive_adjustment": (
        "https://massive.com/knowledge-base/article/is-massives-stock-data-adjusted-for-splits-or-dividends"
    ),
    "massive_splits": "https://massive.com/docs/rest/stocks/corporate-actions/splits",
    "massive_events": "https://massive.com/docs/rest/stocks/overview",
}
ORIGINS = {
    "alpaca_data": "https://data.alpaca.markets",
    "alpaca_paper": "https://paper-api.alpaca.markets",
    "massive": "https://api.massive.com",
}
ACCOUNT_KEYS = {
    "alpaca": ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
    "massive": ("MASSIVE_API_KEY",),
}
REFERENCE_FIELDS = set(
    [
        "ticker",
        "symbol",
        "id",
        "cik",
        "share_class_figi",
        "composite_figi",
        "type",
        "active",
        "status",
        "market",
        "locale",
        "primary_exchange",
        "list_date",
        "delisted_utc",
        "last_updated_utc",
        "class",
        "exchange",
        "tradable",
    ]
)
ACTION_FIELDS = set(
    [
        "id",
        "symbol",
        "ticker",
        "type",
        "date",
        "process_date",
        "effective_date",
        "execution_date",
        "ex_date",
        "payable_date",
        "old_symbol",
        "new_symbol",
        "old_cusip",
        "new_cusip",
        "cusip",
        "rate",
        "split_from",
        "split_to",
        "old_rate",
        "new_rate",
        "cash_rate",
        "cash_amount",
        "cash_per_share",
    ]
)
ACTION_GROUPS = set(
    [
        "forward_split",
        "forward_splits",
        "reverse_split",
        "reverse_splits",
        "cash_merger",
        "cash_mergers",
        "stock_and_cash_merger",
        "stock_and_cash_mergers",
        "name_change",
        "name_changes",
    ]
)


@dataclass(frozen=True)
class Probe:
    origin: str
    kind: str
    symbol: str
    path: str
    query: dict

    @property
    def provider(self):
        return self.origin.split("_")[0]


def probe_plan():
    scopes = (
        ("SPY", "2016-01-04", "2016-01-08"),
        ("SPY", "2018-10-22", "2018-10-26"),
        ("SPY", "2019-12-24", "2019-12-31"),
        ("AAPL", "2020-08-27", "2020-09-02"),
        ("TWTR", "2022-10-25", "2022-11-02"),
        ("SPY", "2025-06-02", "2025-06-06"),
        ("ACN", "2026-11-16", "2026-11-20"),
    )
    probes = []

    def alpaca_bars(symbol, start, end, feed="sip", adjustment="raw", asof="-"):
        return Probe(
            "alpaca_data",
            "bars",
            symbol,
            f"/v2/stocks/{symbol}/bars",
            dict(
                start=datetime.combine(date.fromisoformat(start), time.min, NY).isoformat(),
                end=datetime.combine(date.fromisoformat(end), time(23, 59, 59), NY).isoformat(),
                timeframe="1Day",
                feed=feed,
                adjustment=adjustment,
                asof=asof,
                limit=16,
                sort="asc",
            ),
        )

    for window in scopes:
        probes.extend(alpaca_bars(*window, feed=feed) for feed in ("sip", "iex"))
    probes.extend(alpaca_bars(*scopes[3], adjustment=adjustment) for adjustment in ("split", "all"))
    for symbol in ("JATT", "ZURA"):
        probes.append(alpaca_bars(symbol, "2023-03-16", "2023-03-24"))
    probes.append(alpaca_bars("ZURA", "2023-03-16", "2023-03-24", asof="2023-03-22"))
    for symbol, start, end, types in (
        ("AAPL", "2020-08-20", "2020-09-08", "forward_split"),
        ("TWTR", "2022-10-25", "2022-11-10", "cash_merger,stock_and_cash_merger"),
        ("JATT,ZURA", "2023-03-15", "2023-03-31", "name_change"),
    ):
        probes.append(
            Probe(
                "alpaca_data",
                "actions",
                symbol,
                "/v1/corporate-actions",
                dict(
                    symbols=symbol, start=start, end=end, types=types, limit=10, data_quality="all"
                ),
            )
        )
    for symbol in ("ACN", "TWTR", "JATT"):
        probes.append(Probe("alpaca_paper", "asset_snapshot", symbol, f"/v2/assets/{symbol}", {}))
    for symbol, start, end, flags in (
        ("SPY", "2018-10-22", "2018-10-26", (False,)),
        ("TWTR", "2022-10-25", "2022-11-02", (False,)),
        ("SPY", "2025-06-02", "2025-06-06", (False,)),
        ("ACN", "2026-11-16", "2026-11-20", (False, True)),
    ):
        for adjusted in flags:
            probes.append(
                Probe(
                    "massive",
                    "bars",
                    symbol,
                    f"/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}",
                    dict(adjusted=str(adjusted).lower(), limit=32, sort="asc"),
                )
            )
    for symbol, point in (
        ("ACN", "2018-10-23"),
        ("ACN", "2026-11-19"),
        ("TWTR", "2022-10-27"),
        ("JATT", "2023-03-17"),
        ("JATT", "2026-11-19"),
        ("ZURA", "2026-11-19"),
    ):
        probes.append(
            Probe("massive", "details", symbol, f"/v3/reference/tickers/{symbol}", {"date": point})
        )
    probes.append(
        Probe(
            "massive",
            "inactive",
            "TWTR",
            "/v3/reference/tickers",
            dict(ticker="TWTR", market="stocks", active="false", limit=10),
        )
    )
    probes.append(
        Probe(
            "massive",
            "events",
            "JATT",
            "/vX/reference/tickers/JATT/events",
            {"types": "ticker_change"},
        )
    )
    probes.append(
        Probe(
            "massive",
            "splits",
            "AAPL",
            "/stocks/v1/splits",
            {
                "ticker": "AAPL",
                "execution_date.gte": "2020-08-20",
                "execution_date.lte": "2020-09-08",
                "limit": 10,
            },
        )
    )
    return probes


def encode(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def plain_fields(row, fields):
    if not isinstance(row, dict):
        raise ValueError("UNEXPECTED_OBJECT")
    return {
        key: row[key]
        for key in sorted(fields & row.keys())
        if type(row[key]) in (str, int, bool, Decimal) and len(str(row[key])) <= 100
    }


def sessions(start, end):
    return trading_sessions_between(date.fromisoformat(start), date.fromisoformat(end))


def bar_result(probe, payload):
    alpaca = probe.provider == "alpaca"
    if alpaca:
        start, end = (probe.query[field][:10] for field in ("start", "end"))
        limit = probe.query["limit"]
        if payload.get("symbol") != probe.symbol:
            raise ValueError("SYMBOL_MISMATCH")
    else:
        start, end = probe.path.split("/")[-2:]
        limit = 16
        if payload.get("ticker") != probe.symbol:
            raise ValueError("SYMBOL_MISMATCH")
        if payload.get("adjusted") is not (probe.query["adjusted"] == "true"):
            raise ValueError("ADJUSTMENT_MISMATCH")
    bars = payload.get("bars" if alpaca else "results")
    empty_massive = not alpaca and payload.get("resultsCount") == 0
    if (alpaca and "bars" in payload and bars is None) or (bars is None and empty_massive):
        bars = []
    if not isinstance(bars, list) or len(bars) > limit:
        raise ValueError("UNEXPECTED_BAR_COUNT")
    expected = {day.isoformat() for day in sessions(start, end)}
    rows = {}
    invalid = 0
    for bar in bars:
        try:
            if alpaca:
                stamp = datetime.fromisoformat(bar["t"])
                if stamp.utcoffset() is None:
                    raise ValueError
            else:
                epoch = bar["t"]
                if type(epoch) is not int:
                    raise ValueError
                stamp = datetime.fromtimestamp(epoch / 1000, UTC)
            local = stamp.astimezone(NY)
            if local.time() != time.min:
                raise ValueError
            session = local.date().isoformat()
            opening, high, low, close, volume = (Decimal(str(bar[field])) for field in "ohlcv")
            if (
                not all(n.is_finite() for n in (opening, high, low, close, volume))
                or not (0 < low <= min(opening, close) <= max(opening, close) <= high)
                or volume <= 0
                or session not in expected
                or session in rows
            ):
                raise ValueError
            rows[session] = [
                stamp.isoformat(),
                *(str(n) for n in (opening, high, low, close, volume)),
            ]
        except (
            KeyError,
            TypeError,
            ValueError,
            AttributeError,
            InvalidOperation,
            OSError,
            OverflowError,
        ):
            invalid += 1
    normalized = [[probe.symbol, day, *row] for day, row in sorted(rows.items())]
    normalized_bytes = b"".join(encode(row) + b"\n" for row in normalized)
    omitted = sorted(expected - rows.keys())
    return {
        "classification": "INVALID_OR_DUPLICATE_BAR"
        if invalid
        else "SAMPLE_COMPLETE"
        if rows and not omitted
        else "PARTIAL_SAMPLE"
        if rows
        else "NO_PROVIDER_OBSERVATION",
        "expected_sessions": sorted(expected),
        "missing_sessions": omitted,
        "invalid_rows": invalid,
        "rows_returned": len(bars),
        "rows_usable": len(rows),
        "last_observed_session": max(rows, default=None),
        "bars": normalized,
        "normalized_jsonl_bytes": len(normalized_bytes),
        "normalized_gzip_bytes": len(gzip.compress(normalized_bytes, mtime=0)),
        "feed_evidence": "EXPLICIT_REQUEST_PARAMETER_NOT_ECHOED"
        if alpaca
        else "MASSIVE_AGGREGATES_NOT_ALPACA_SIP_OR_IEX",
    }


def reference_result(probe, payload):
    match probe.kind:
        case "asset_snapshot" | "details":
            record = payload if probe.kind == "asset_snapshot" else payload["results"]
            values = [plain_fields(record, REFERENCE_FIELDS)]
        case "events":
            events = payload["results"]["events"]
            if not isinstance(events, list) or len(events) > 12:
                raise ValueError("UNEXPECTED_EVENT_COUNT")
            values = [
                plain_fields(event, ACTION_FIELDS)
                | {"ticker_change": plain_fields(event.get("ticker_change", {}), {"ticker"})}
                for event in events
            ]
        case "actions":
            groups = payload["corporate_actions"]
            if not isinstance(groups, dict) or set(groups) - ACTION_GROUPS:
                raise ValueError("UNEXPECTED_CORPORATE_ACTION_GROUP")
            values = [
                plain_fields(row, ACTION_FIELDS) | {"action_group": group}
                for group, rows in sorted(groups.items())
                for row in rows
            ]
        case "inactive" | "splits":
            records = payload.get("results", [])
            fields = REFERENCE_FIELDS if probe.kind == "inactive" else ACTION_FIELDS
            if not isinstance(records, list):
                raise ValueError("UNEXPECTED_RECORDS")
            values = [plain_fields(record, fields) for record in records]
        case _:
            raise ValueError("UNEXPECTED_PROBE")
    if len(values) > 12 or any(not value for value in values):
        raise ValueError("UNEXPECTED_RECORD_COUNT_OR_FIELDS")
    if probe.kind in {"asset_snapshot", "details", "inactive"} and any(
        row.get("ticker", row.get("symbol")) != probe.symbol for row in values
    ):
        raise ValueError("SYMBOL_MISMATCH")
    return {
        "classification": "POINT_OBSERVATION" if values else "NO_PROVIDER_OBSERVATION",
        "records": values,
    }


def analyze(probe, status, raw):
    result = {"http_status": status, "decoded_body_bytes": len(raw), "body_sha256": sha(raw)}
    if status != 200:
        result["classification"] = {
            401: "AUTH_FAILED",
            402: "PLAN_OR_PAYMENT_REQUIRED",
            403: "ACCESS_DENIED",
            404: "NO_PROVIDER_OBSERVATION",
            429: "RATE_LIMITED",
        }.get(status, "HTTP_ERROR")
        return result
    try:
        body = json.loads(raw, parse_float=Decimal)
        if not isinstance(body, dict):
            raise ValueError
        if body.get("next_page_token") or body.get("next_url"):
            return result | {"classification": "PAGINATION_REQUIRED_NOT_FOLLOWED"}
        if probe.provider == "massive" and body.get("status") not in {"OK", "DELAYED"}:
            raise ValueError
        return result | (
            bar_result(probe, body) if probe.kind == "bars" else reference_result(probe, body)
        )
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return result | {"classification": "UNEXPECTED_RESPONSE"}


def credentials(provider, env_file=None):
    if env_file:
        from dotenv import dotenv_values

        try:
            saved = dotenv_values(env_file, interpolate=False)
        except (OSError, UnicodeError):
            return {}, []
    else:
        saved = {}
    names = ACCOUNT_KEYS[provider]
    secrets = [os.environ.get(name) or saved.get(name) for name in names]
    if not all(
        secret and secret.isascii() and not any(c.isspace() for c in secret) for secret in secrets
    ):
        return {}, []
    headers = (
        {"Authorization": "Bearer " + secrets[0]}
        if provider == "massive"
        else dict(zip(("APCA-API-KEY-ID", "APCA-API-SECRET-KEY"), secrets, strict=True))
    )
    return headers, secrets


def run_probes(provider, probes, env_file):
    import requests

    from trading_system.data.massive_rate_limit import MassiveReferenceError
    from trading_system.data.massive_reference import _REQUEST_LIMITER

    headers, secrets = credentials(provider, env_file)
    if not headers:
        return [
            {"probe": asdict(probe), "classification": "CREDENTIALS_UNAVAILABLE"}
            for probe in probes
        ]
    results = []
    stopped = False
    with requests.Session() as session:
        session.trust_env = False
        session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))

        def fetch(probe):
            deadline = monotonic() + 25
            with session.get(
                ORIGINS[probe.origin] + probe.path,
                params=probe.query,
                headers=headers,
                timeout=(5, 10),
                allow_redirects=False,
                stream=True,
            ) as response:
                raw = bytearray()
                for chunk in response.iter_content(8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY or monotonic() > deadline:
                        return {
                            "http_status": response.status_code,
                            "classification": "RESPONSE_BUDGET_EXCEEDED",
                        }
                return analyze(probe, response.status_code, bytes(raw))

        for probe in probes:
            if stopped:
                results.append({"probe": asdict(probe), "classification": "PROBE_STOPPED"})
                continue
            observed = datetime.now(UTC).isoformat()
            try:
                if provider == "massive":
                    item = _REQUEST_LIMITER.transmit(lambda probe=probe: fetch(probe), limit=5)
                else:
                    item = fetch(probe)
            except MassiveReferenceError:
                item = {"classification": "SHARED_RATE_LIMITER_UNAVAILABLE"}
                stopped = True
            except requests.RequestException:
                item = {"classification": "NETWORK_ERROR"}
            stopped = (
                stopped
                or item.get("http_status") == 429
                or (item.get("http_status") == 401 and probe.origin != "alpaca_paper")
            )
            results.append({"probe": asdict(probe), "observed_at_utc": observed, **item})

    # Secrets never belong in provider evidence, even if a response echoes input.
    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, Decimal):
            value = str(value)
        return (
            "[REDACTED]" if isinstance(value, str) and any(s in value for s in secrets) else value
        )

    return redact(results)


def capacity(results):
    grouped = defaultdict(list)
    for item in results:
        probe = item["probe"]
        if (
            probe["kind"] != "bars"
            or item["classification"] != "SAMPLE_COMPLETE"
            or probe["query"].get("asof", "-") != "-"
        ):
            continue
        query = probe["query"]
        variant = (
            probe["origin"],
            query.get("feed", "massive_aggregates"),
            query.get("adjustment", query.get("adjusted")),
        )
        grouped[variant].append(item)
    bars = 51 * (300 + len(sessions("2016-01-01", "2026-11-20")))
    scenarios = []
    for (provider, feed, adjustment), samples in sorted(grouped.items()):
        count = sum(item["rows_usable"] for item in samples)
        if count < 10 or len({item["probe"]["symbol"] for item in samples}) < 2:
            continue
        projected = {
            key: (bars * sum(item[key] for item in samples) + count - 1) // count
            for key in ("decoded_body_bytes", "normalized_jsonl_bytes", "normalized_gzip_bytes")
            if all(key in item for item in samples)
        }
        scenarios.append(
            {
                "origin": provider,
                "feed": feed,
                "adjustment": adjustment,
                "sample_bars": count,
                "sample_windows": len(samples),
                "projected_rows": bars,
                "projected_bytes": projected,
                "compact_plus_one_same_volume_copy_bytes": 2 * projected["normalized_gzip_bytes"],
            }
        )
    return {
        "state": "SAMPLE_EXTRAPOLATION_ONLY" if scenarios else "PENDING_MEASURED_BAR_SAMPLE",
        "target_symbols_including_spy": 51,
        "target_daily_row_slots_with_warmup": bars,
        "by_feed_and_adjustment": scenarios,
        "excluded": [
            "additional_share_classes",
            "security_and_action_sources",
            "manifests",
            "workspace_reserve",
            "future_database_indexes_and_journals",
        ],
    }


def stored_sip_measurement():
    """Size a few existing provider observations without retaining or changing source rows."""
    import sqlite3
    from contextlib import closing

    from f01_historical_50_preflight_run import path_state

    from trading_system.data.canonical_paths import pinned_files

    path = ROOT / "data/trading_system.sqlite3"
    scopes = (("2025-08-04", "2025-08-08"), ("2026-11-02", "2026-11-06"))
    samples = []
    with pinned_files({"research": path}) as handles:
        before = path_state(path)
        if (
            not os.path.samestat(os.fstat(handles["research"].fileno()), path.stat())
            or before.get(path.name + "-wal", (0, 0, 0))[2] != 0
            or path.name + "-journal" in before
        ):
            raise ValueError("STORED_SAMPLE_REQUIRES_STABLE_CHECKPOINT")
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            for symbol in ("SPY", "AAPL", "ACN"):
                for start, end in scopes:
                    probe = Probe(
                        "alpaca_data",
                        "bars",
                        symbol,
                        f"/v2/stocks/{symbol}/bars",
                        dict(
                            start=start, end=end, feed="sip", adjustment="all", asof="-", limit=16
                        ),
                    )
                    rows = db.execute(
                        """SELECT timestamp,open,high,low,close,volume,sync_reference,retrieved_at
                           FROM forward_market_observations
                           WHERE symbol=? AND session BETWEEN ? AND ? AND timeframe='1d'
                           AND provider='alpaca' AND feed='sip' AND adjustment='all'
                           ORDER BY session LIMIT 17""",
                        (symbol, start, end),
                    ).fetchall()
                    if any(not ref or not stamp for *_, ref, stamp in rows):
                        raise ValueError("STORED_SAMPLE_MISSING_PROVENANCE")
                    body = {
                        "symbol": symbol,
                        "bars": [dict(zip("tohlcv", row[:6], strict=True)) for row in rows],
                    }
                    measurements = bar_result(probe, body)
                    del measurements["bars"]
                    samples.append(
                        {
                            "probe": asdict(probe),
                            "stored_rows_digest": sha(encode(rows)),
                            **measurements,
                        }
                    )
        if before != path_state(path):
            raise ValueError("STORED_SAMPLE_SOURCE_DRIFT")
    return {
        "basis": "PRIOR_PERSISTED_PROVIDER_OBSERVATIONS_NOT_CURRENT_ACCOUNT_PROBE",
        "source": str(path),
        "file_state": before,
        "samples": samples,
        "capacity": capacity(samples),
    }


def split_comparison(results):
    variants = {}
    for item in results:
        query = item["probe"]["query"]
        if (
            item["probe"]["symbol"] == "AAPL"
            and query.get("feed") == "sip"
            and item["classification"] == "SAMPLE_COMPLETE"
        ):
            variants[query["adjustment"]] = {row[1]: row for row in item["bars"]}
    raw = variants.get("raw", {})
    comparisons = []
    for variant in ("split", "all"):
        adjusted = variants.get(variant, {})
        for day in sorted(raw.keys() & adjusted.keys()):
            comparisons.append(
                {
                    "date": day,
                    "variant": variant,
                    "raw_to_adjusted_close_ratio": str(
                        Decimal(raw[day][6]) / Decimal(adjusted[day][6])
                    ),
                    "adjusted_to_raw_volume_ratio": str(
                        Decimal(adjusted[day][7]) / Decimal(raw[day][7])
                    ),
                }
            )
    return comparisons


def local_report():
    data = {}
    for name, expected in SOURCES.items():
        raw = (REPORTS / name).read_bytes()
        if sha(raw) != expected:
            raise ValueError(f"LOCAL_SOURCE_BINDING_MISMATCH: {name}")
        data[name] = raw
    preflight = json.loads(data["f01_historical_50_preflight.json"])
    pilot = list(csv.DictReader(io.StringIO(data["f01_historical_50_pilot.csv"].decode())))
    if [(r["symbol_label"], r["planning_cik"]) for r in pilot] != [
        (r["symbol"], r["research_cik"]) for r in preflight["candidates"]
    ] or len(pilot) != 50:
        raise ValueError("LOCAL_PILOT_BINDING_MISMATCH")
    old = json.loads(data["f_2022_massive_identity_pilot_v1_summary.json"])
    sip = json.loads(data["f_forward_sip_shadow_contract_summary.json"])
    legacy = preflight["local_books"]["legacy_symbol_store"]
    plan = probe_plan()
    return {
        "status": STATUS,
        "contract": "F01_HISTORICAL_MARKET_FEASIBILITY_V1",
        "decision": "C",
        "decision_scope": "HISTORICAL_TRADING_QUALIFICATION_2016_2026",
        "documentation_reviewed": "2026-11-23",
        "documentation": DOCS,
        "source_sha256": SOURCES,
        "helper_sha256": sha(Path(__file__).read_bytes()),
        "documented": {
            "alpaca": {
                "tier": "Basic",
                "history_since": 2016,
                "calls_per_min": 200,
                "live_feed": "iex",
                "historical_sip_end_delay_minutes": 15,
                "actions_publication_time_guaranteed": False,
            },
            "massive": {
                "tier": "Stocks Basic",
                "calls_per_min": 5,
                "bars_rolling_years": 2,
                "dated_reference_rolling_years": 2,
                "splits_rolling_years": 2,
                "flat_files": False,
                "experimental_ticker_events": True,
            },
        },
        "prior_observations": {
            "massive_2022_reference_run": {
                key: old[key]
                for key in (
                    "pilot_symbols",
                    "period",
                    "massive_requests",
                    "successful_responses",
                    "not_found_responses",
                )
            },
            "massive_normalized_reference_archive_bytes": len(
                data["f_2022_massive_identity_pilot_v1_observations.json"]
            ),
            "alpaca_persisted_observations": {
                "separate_forward_contract": sip["market_data_contract_id"],
                "forward_groups": legacy["forward_market_observations"],
                "spy": legacy["forward_alpaca_sip_all_daily"]["SPY"],
            },
            "prior_account_tier_verified_for_these_observations": False,
            "legacy_spy": legacy["daily"]["SPY"],
            "historically_trading_qualified_issuers": preflight["historical_trading_qualified"],
        },
        "security_blockers": {
            "SPY": ["300_CONTIGUOUS_PRIOR_SESSIONS", "2014_2015_BOUNDARY_FOR_2016_START"],
            "AAPL": ["SPLIT_EFFECTIVE_DATE_AND_FACTOR", "PIT_PRICE_EPS_SHARES_BASIS"],
            "ACN": ["CLASS_A_SECURITY_ID_AND_EPOCHS", "ECONOMIC_ISSUER_SHARES_SCOPE"],
            "TWTR": [
                "HISTORICAL_SECURITY_ID_AND_FINAL_SESSION",
                "EXIT_TERMS_EFFECTIVE_DATE_AND_VALUE",
            ],
            "JATT": [
                "PRE_AND_POST_CHANGE_SECURITY_ID",
                "TICKER_REUSE_EPOCH",
                "CORPORATE_ACTION_EFFECTIVE_DATE",
            ],
            "universe": [
                "DATE_BOUND_LISTING_AND_DELISTING_INTERVALS",
                "NO_CURRENT_UNIVERSE_BACKFILL",
                "NO_CROSS_FEED_STITCHING",
            ],
        },
        "batch_plan": {
            "symbols_per_batch": 5,
            "time_slice_months": 12,
            "variants_separate": True,
            "commit": "validated immutable per-symbol shards and atomic completion manifest",
            "resume_key": [
                "provider",
                "feed",
                "adjustment",
                "security_epoch",
                "symbol",
                "asof",
                "query_interval",
                "schema",
                "source_hash",
            ],
            "precondition": "review_account_security_epochs_and_exits_before_sec_expansion",
        },
        "probe_plan": [asdict(probe) for probe in plan],
        "account_probe": "NOT_RUN",
        "observations": [],
        "aapl_split_comparison": [],
        "capacity": capacity([]),
        "local_storage_measurement": None,
        "release": {"pit": False, "trading": False, "production": False},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe", choices=ACCOUNT_KEYS)
    parser.add_argument("--authorize-provider-network", action="store_true")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--measure-local-sip", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.probe) != args.authorize_provider_network:
        parser.error("--probe and --authorize-provider-network are required together")
    if args.measure_local_sip and args.probe:
        parser.error("local measurement and provider probing are separate modes")
    if (args.credentials_file or args.probe) and not (args.probe and args.output):
        parser.error("provider mode requires a new --output file")
    if args.output and (
        args.output.suffix.lower() != ".json"
        or os.path.lexists(args.output)
        or not args.output.parent.is_dir()
    ):
        parser.error("--output must be a new JSON file in an existing directory")
    git = ["git", "-c", f"safe.directory={ROOT.as_posix()}"]
    branch = subprocess.check_output(
        [*git, "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    if branch != "SEC-integration":
        parser.error("SEC-integration required")
    report = local_report()
    report["head"] = subprocess.check_output(
        [*git, "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if args.measure_local_sip:
        report["local_storage_measurement"] = stored_sip_measurement()
    if args.probe:
        if args.credentials_file and not args.credentials_file.is_file():
            parser.error("credentials file must exist")
        # Silence request-library and dotenv diagnostics, including parse errors in secret files.
        logging.disable(logging.CRITICAL)
        plan = [probe for probe in probe_plan() if probe.provider == args.probe]
        observations = run_probes(args.probe, plan, args.credentials_file)
        report.update(
            account_probe={
                "provider": args.probe,
                "tier_attestation": "UNVERIFIED",
                "http_responses": sum("http_status" in item for item in observations),
            },
            observations=observations,
            capacity=capacity(observations),
            aapl_split_comparison=split_comparison(observations),
        )
    output = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
