"""F03: synthetic local SIP fixtures; no provider, backtest or journal execution."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from shadow_fixtures import official_history
from test_champion_consolidation import bar
from test_champion_consolidation import offline as offline
from test_shadow_execution import ready_states
from test_shadow_qualification import SESSION, candidate

from trading_system import cli
from trading_system.ai.export import NoAICandidatesError, build_ai_candidate_export
from trading_system.champion import champion_ranking, screen_champion
from trading_system.config import load_strategy_config
from trading_system.data.database import Database
from trading_system.data.forward_market import persist
from trading_system.data.observations import current_session_observation
from trading_system.models.screening import ScreenReport
from trading_system.shadow.admission import evaluate_inception_admission
from trading_system.shadow.contracts import digest, normalized
from trading_system.shadow.inputs import LocalInputs
from trading_system.shadow.modes import ECONOMIC, OBSERVATION_ONLY
from trading_system.shadow.readiness import evaluate_shadow_input_readiness
from trading_system.strategy.reporting import load_report
from trading_system.strategy.screener import Screener

NOW = datetime(2026, 10, 23, 22, tzinfo=UTC)


def seed(db, values, feed="sip", adjustment="all"):
    persist(
        db,
        values,
        provider="alpaca",
        feed=feed,
        adjustment=adjustment,
        sync_reference="synthetic-f03",
        retrieved_at=NOW,
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    original_screener = Screener.run
    db = Database(tmp_path / "market.sqlite3")
    db.initialize()
    config = load_strategy_config()
    config.storage.database_path = db.path
    config.storage.reports_path = tmp_path / "reports"
    days = official_history(SESSION, 301)
    rows = {}

    def screen(screener, requested, *, use_market_snapshots):
        assert isinstance(screener.database, LocalInputs)
        assert not use_market_snapshots
        records = []
        for symbol in ("BBB", "AAA"):
            history = screener.database.bars_available_as_of(symbol, SESSION, limit=300)
            current = screener.database.bars_on_session([symbol], SESSION).get(symbol)
            records.append(
                candidate(
                    symbol=symbol,
                    count=len(history),
                    observation=current_session_observation(current, SESSION),
                    reasons=("insufficient_market_history",) if len(history) < 300 else (),
                    quality=20
                    if rows.get("kind") == "deterministic_reject" and symbol == "AAA"
                    else 90,
                )
            )
        return ScreenReport(
            as_of=SESSION,
            requested_as_of=requested,
            effective_market_session=SESSION,
            generated_at="synthetic",
            analyzed_count=2,
            eligible_count=0,
            records=tuple(records),
        )

    monkeypatch.setattr(Screener, "run", screen)

    def prepare(kind):
        assert not rows, "prepare each isolated fixture once"
        rows["kind"] = kind
        values = [bar("AAA", day) for day in days[-300:]]
        if kind == "gap_300":
            del values[100]
            values.insert(0, bar("AAA", days[0]))
        elif kind == "zero_volume":
            values[100] = values[100].model_copy(update={"volume": 0})
        elif kind == "historical_gap":
            values = values[1:]
        elif kind in {"absent_history", "deterministic_reject"}:
            values = []
        elif kind == "current_only":
            values = values[-1:]
        elif kind in {"wrong_feed", "wrong_adjustment"}:
            db.upsert_bars(values)
            seed(
                db,
                values,
                feed="iex" if kind == "wrong_feed" else "sip",
                adjustment="raw" if kind == "wrong_adjustment" else "all",
            )
            values = values[-1:]
        seed(db, values + [bar("BBB", day) for day in days[-300:]] + [bar("SPY", SESSION)])
        if kind == "invalid_ohlc":
            with db.connect() as connection:
                connection.execute(
                    "UPDATE forward_market_observations SET high='1' "
                    "WHERE symbol='AAA' AND session=?",
                    (days[-300:][100].isoformat(),),
                )
        ready_states(db, NOW)
        return db, config, days

    prepare.original_screener = original_screener
    return prepare


def readiness(db, config):
    with LocalInputs(db.path).snapshot() as inputs:
        return evaluate_shadow_input_readiness(inputs, config, now=NOW, session=SESSION)


def test_f03_300_rows_do_not_prove_official_history_parity(setup):
    db, config, _ = setup("gap_300")
    shadow = readiness(db, config)
    assert shadow.raw_report.records[-1].market_history_count == 300
    assert shadow.qualification["incomplete_inputs"] == [
        {"symbol": "AAA", "reasons": ["MARKET_HISTORY_UNPROVEN"]}
    ]
    assert shadow.observation_ready and not shadow.ready
    standalone = screen_champion(db, config, SESSION)
    assert standalone.eligible_count == shadow.ranked_report.eligible_count == 0, (
        "F03 row-count fallback publishes ranking",
        standalone.eligible_count,
        shadow.qualification["unresolved_rank_relevant_count"],
    )


@pytest.mark.parametrize(
    "kind",
    [
        "full",
        "gap_300",
        "zero_volume",
        "invalid_ohlc",
        "historical_gap",
        "current_only",
        "absent_history",
        "deterministic_reject",
        "wrong_feed",
        "wrong_adjustment",
        "current_zero_volume",
        "missing_current",
    ],
)
def test_screen_ai_and_both_shadow_readiness_modes_share_coverage(setup, kind):
    # Two extra current-observation cases use the existing fixture variants below.
    prepared_kind = "full" if kind in {"current_zero_volume", "missing_current"} else kind
    db, config, days = setup(prepared_kind)
    if kind == "current_zero_volume":
        seed(db, [bar("AAA", SESSION).model_copy(update={"volume": 0})])
    elif kind == "missing_current":
        with db.connect() as c:
            c.execute(
                "DELETE FROM forward_market_observations WHERE symbol='AAA' AND session=?",
                (SESSION.isoformat(),),
            )
        seed(db, [bar("AAA", days[0])])
    before = db.path.read_bytes()
    shadow = readiness(db, config)
    standalone = screen_champion(db, config, SESSION)
    assert db.path.read_bytes() == before
    manifest = json.loads(Path(standalone.market_input_manifest_path).read_text())
    assert digest(manifest) == standalone.market_input_manifest_hash
    assert manifest["history_coverage"] == shadow.details["history_coverage"]
    assert manifest["feed"] == "sip" and manifest["adjustment"] == "all"
    assert standalone.records == shadow.ranked_report.records
    assert standalone.eligible_count == shadow.ranked_report.eligible_count
    complete = kind in {"full", "deterministic_reject"}
    assert shadow.ready is complete
    assert shadow.observation_ready  # Ranking gaps are the only blockers here.
    assert shadow.details["ranking_complete"] is complete
    assert shadow.qualification["unresolved_rank_relevant_count"] == (0 if complete else 1)
    if not complete:
        assert standalone.eligible_count == 0
        assert all(r.rank is None and not r.eligible for r in standalone.records)
        assert shadow.details["blocking_reasons"] == ["RANKING_UNRESOLVED"]
        assert manifest["history_coverage"]["AAA"]["status"] == "incomplete"
        with pytest.raises(NoAICandidatesError):
            build_ai_candidate_export(standalone)
    else:
        payload = build_ai_candidate_export(standalone, generated_at=NOW)
        assert [r.symbol for r in payload.candidates] == (
            ["BBB"] if kind == "deterministic_reject" else ["AAA", "BBB"]
        )
        assert [r.quant_score for r in payload.candidates] == [90] * payload.candidate_count
        assert standalone.records == champion_ranking(shadow.raw_report, config).records
        if kind == "full":
            assert all(
                e["status"] == "complete" and e["usable_sessions"] == 300
                for e in manifest["history_coverage"].values()
            )
        else:
            assert shadow.qualification["rejected"] == {"AAA": "quality_threshold"}
            assert manifest["history_coverage"]["AAA"]["status"] == "incomplete"
    if kind in {"full", "gap_300", "deterministic_reject"}:
        for mode in (ECONOMIC, OBSERVATION_ONLY):
            journal_path = db.path.with_name(mode + "-never-created.sqlite3")
            admission = evaluate_inception_admission(
                db.path, config, journal_path=journal_path, now=NOW, requested_mode=mode
            )
            assert admission.readiness.qualification == shadow.qualification
            assert admission.selected_mode_ready is (complete or mode == OBSERVATION_ONLY)
            assert not journal_path.exists()


def test_canonical_coverage_and_qualification_run_inside_same_snapshot(setup, monkeypatch):
    from trading_system.shadow import qualification

    db, config, _ = setup("full")
    observed = {}
    history = LocalInputs.history_coverage
    qualify = qualification.qualified_forward_screen

    def coverage(inputs, symbols, session, count):
        assert session == SESSION and count == 300 and set(symbols) == {"AAA", "BBB"}
        assert inputs._connection.in_transaction
        assert inputs._connection.execute("PRAGMA query_only").fetchone()[0] == 1
        observed["inputs"] = inputs
        observed["coverage"] = history(inputs, symbols, session, count)
        observed["calls"] = observed.get("calls", 0) + 1
        return observed["coverage"]

    def qualified(raw, session, config, *, history_coverage=None):
        assert history_coverage is observed["coverage"]
        assert observed["inputs"]._connection.in_transaction
        return qualify(raw, session, config, history_coverage=history_coverage)

    monkeypatch.setattr(LocalInputs, "history_coverage", coverage)
    monkeypatch.setattr(qualification, "qualified_forward_screen", qualified)
    report = screen_champion(db, config, SESSION)
    content = json.loads(Path(report.market_input_manifest_path).read_text())
    assert observed["calls"] == 1
    assert content["history_coverage"] == normalized(observed["coverage"])
    assert observed["inputs"]._connection is None


def test_requested_weekend_uses_effective_official_session_in_coverage_and_manifest(setup):
    db, config, _ = setup("full")
    report = screen_champion(db, config, SESSION + timedelta(days=1))
    content = json.loads(Path(report.market_input_manifest_path).read_text())
    assert report.eligible_count == 2 and report.as_of == SESSION
    assert content["session"] == str(SESSION)
    assert content["requested_as_of"] == str(SESSION + timedelta(days=1))
    assert all(
        e["expected_sessions"] == e["usable_sessions"] == 300
        for e in content["history_coverage"].values()
    )


def test_concurrent_fixture_revision_cannot_mix_screen_and_coverage_snapshots(setup, monkeypatch):
    db, config, days = setup("full")
    source = Screener.run
    revised = []

    def screen_then_revise(screener, *args, **kwargs):
        raw = source(screener, *args, **kwargs)
        if not revised:
            with db.connect() as c:
                c.execute(
                    "UPDATE forward_market_observations SET volume=0 "
                    "WHERE symbol='AAA' AND session=?",
                    (days[-300:][100].isoformat(),),
                )
            revised.append(True)
        return raw

    monkeypatch.setattr(Screener, "run", screen_then_revise)
    old = screen_champion(db, config, SESSION)
    old_content = json.loads(Path(old.market_input_manifest_path).read_text())
    assert old.eligible_count == 2
    assert old_content["history_coverage"]["AAA"]["status"] == "complete"
    new = screen_champion(db, config, SESSION)
    content = json.loads(Path(new.market_input_manifest_path).read_text())
    assert new.eligible_count == 0
    assert content["history_coverage"]["AAA"]["usable_sessions"] == 299
    assert old.market_input_manifest_hash != new.market_input_manifest_hash


def test_repair_changes_manifest_identity_without_rewriting_old_evidence(setup):
    db, config, days = setup("gap_300")
    before = screen_champion(db, config, SESSION)
    path = Path(before.market_input_manifest_path)
    original = path.read_bytes()
    seed(db, [bar("AAA", days[-300:][100])])
    after = screen_champion(db, config, SESSION)
    assert before.eligible_count == 0 and after.eligible_count == 2
    assert before.market_input_manifest_hash != after.market_input_manifest_hash
    assert path.read_bytes() == original
    content = json.loads(Path(after.market_input_manifest_path).read_text())
    assert digest(content) == after.market_input_manifest_hash
    assert content["history_coverage"]["AAA"]["status"] == "complete"


def test_manifest_tampering_is_not_silently_rewritten(setup):
    db, config, _ = setup("full")
    report = screen_champion(db, config, SESSION)
    path = Path(report.market_input_manifest_path)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="FORWARD_MANIFEST_HASH_COLLISION_OR_CORRUPTION"):
        screen_champion(db, config, SESSION)
    assert path.read_text() == "{}"


@pytest.mark.parametrize("kind", ["full", "gap_300", "deterministic_reject"])
def test_real_cli_champion_screen_and_ai_export_use_same_qualification(
    setup, kind, monkeypatch, tmp_path, capsys
):
    db, config, _ = setup(kind)
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(strategy=config))
    with db.read_only() as c:
        before = [
            tuple(r)
            for r in c.execute("SELECT * FROM forward_market_observations ORDER BY symbol,session")
        ]
    assert cli.main(["screen", "--champion", "--as-of", str(SESSION)]) == 0
    path = config.storage.reports_path / "champion" / f"screen_{SESSION}.json"
    report = load_report(path)
    evidence = json.loads(Path(report.market_input_manifest_path).read_text())
    assert evidence["history_coverage"] == readiness(db, config).details["history_coverage"]
    output = tmp_path / "ai.json"
    status = cli.main(["export-ai", "--champion", "--as-of", str(SESSION), "--output", str(output)])
    if kind == "gap_300":
        assert status == 1 and not output.exists() and report.eligible_count == 0
        assert "No eligible screened candidates" in capsys.readouterr().err
    else:
        assert status == 0
        payload = json.loads(output.read_text())
        assert [r["symbol"] for r in payload["candidates"]] == [
            r.symbol for r in report.records if r.eligible
        ]
        assert payload["strategy"]["name"] == "F/configured/C1"
    with db.read_only() as c:
        assert [
            tuple(r)
            for r in c.execute("SELECT * FROM forward_market_observations ORDER BY symbol,session")
        ] == before


def test_operational_admission_remains_distinct_from_screen_qualification(setup):
    db, config, _ = setup("full")
    db.set_sync_value(
        "dataset",
        "asset_universe",
        {"status": "failed", "errors": 1, "last_success_at": NOW.isoformat()},
    )
    standalone = screen_champion(db, config, SESSION)
    assert standalone.eligible_count == 2  # A local qualified screen is not mode admission.
    result = readiness(db, config)
    assert result.details["ranking_complete"]
    assert not result.ready and not result.observation_ready
    assert result.details["blocking_reasons"] == [
        "ASSET_UNIVERSE_ERRORS_PRESENT",
        "ASSET_UNIVERSE_STATE_FAILED",
    ]


def test_real_screener_and_champion_manifest_share_short_history_evidence(setup, monkeypatch):
    from trading_system.models.fundamentals import CompanyIdentity
    from trading_system.models.market_data import TradableAsset

    db, config, _ = setup("current_only")
    monkeypatch.setattr(Screener, "run", setup.original_screener)
    db.upsert_assets(
        [
            TradableAsset(
                symbol="AAA", name="AAA", tradable=True, fractionable=True, asset_class="us_equity"
            )
        ]
    )
    db.upsert_company(CompanyIdentity(cik="0000000001", symbol="AAA", name="AAA", sic="3571"))
    shadow = readiness(db, config)
    report = screen_champion(db, config, SESSION)
    assert len(report.records) == 1 and report.records[0].market_history_count == 1
    assert report.records[0].current_market_observation == "OBSERVATION_PRESENT"
    assert report.records == shadow.ranked_report.records
    assert not shadow.ready and shadow.observation_ready
    assert report.eligible_count == 0
    assert "MARKET_HISTORY_UNPROVEN" in shadow.qualification["incomplete_inputs"][0]["reasons"]
    manifest = json.loads(Path(report.market_input_manifest_path).read_text())
    assert manifest["history_coverage"] == shadow.details["history_coverage"]
    assert manifest["history_coverage"]["AAA"]["usable_sessions"] == 1
