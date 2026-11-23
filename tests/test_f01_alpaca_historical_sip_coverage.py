"""Focused offline tests for the eight-control SIP qualification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reports"))
import f01_alpaca_historical_sip_coverage as audit  # noqa: E402


def test_calendar_gaps_are_deterministic_and_omit_weekend_and_holiday():
    dates = [
        d.isoformat()
        for d in audit.trading_sessions_between(audit.date(2019, 12, 24), audit.date(2019, 12, 31))
    ]
    first, last = dates[0], dates[-1]
    observed = {first, last}
    result, missing = audit.coverage(dates, observed, observed, 0, "SECURITY")
    assert missing == [["2019-12-26", "2019-12-27", "2019-12-30"]]
    assert result["largest_consecutive_gap"] == result["internal_missing_sessions"] == 3
    assert result["qualification"] == "PARTIAL"
    valid = ["2019-12-24T05:00:00+00:00", 3, 4, 2, 3, 4]
    seen, good, invalid, _ = audit.populate([valid, valid])
    assert seen == {"2019-12-24"} and not good and invalid["2019-12-24"] == 1


def test_feed_adjustment_and_unknown_legacy_are_never_substituted():
    stores = {
        ("probe", "SPY", "sip", "raw"): (set(), {"2016-01-04"}, {}, "raw"),
        ("probe", "SPY", "iex", "raw"): (set(), {"2016-01-05"}, {}, "iex"),
        ("forward", "SPY", "sip", "all"): (set(), {"2016-01-06"}, {}, "all"),
        ("legacy", "SPY", "unknown", "unknown"): (set(), {"2016-01-07"}, {}, "legacy"),
    }
    assert audit.retained_for_plan(stores, "SPY", ("sip", "raw")) == {"2016-01-04"}
    assert audit.retained_for_plan(stores, "SPY", ("sip", "all")) == {"2016-01-06"}
    assert not audit.retained_for_plan(stores, "SPY", ("sip", "split"))


def test_verified_epoch_clips_window_and_reused_labels_are_not_joined():
    dates = ["2023-03-20", "2023-03-21", "2026-11-19"]
    old = {"start": "2023-03-17", "end": "2023-03-21", "verified": True, "security_id": "OLD"}
    new = {"start": "2026-04-17", "end": "2026-11-20", "verified": True, "security_id": "NEW"}
    assert audit.apply_epoch(dates, [old]) == (["2023-03-20"], "OLD")
    assert audit.apply_epoch(dates, [old, new]) == (dates, None)
    assert audit.apply_epoch(dates, [old | {"verified": False}]) == (dates, None)


def test_warmup_requires_300_and_complete_symbol_coverage_cannot_qualify_unknown_epoch():
    warmup = audit.calendar()["WARMUP"]
    assert len(warmup) == 300 and warmup[0] == "2014-10-23" and warmup[-1] == "2015-12-31"
    one_gap = set(warmup[1:])
    result, missing = audit.coverage(warmup, one_gap, one_gap, 0, None)
    assert result["usable_sessions"] == 299 and result["qualification"] == "BLOCKED"
    assert missing == [["2014-10-23"]]
    complete, _ = audit.coverage(warmup, set(warmup), set(warmup), 0, None)
    assert complete["technical_coverage"] == "COMPLETE_IN_REFERENCE_CALENDAR"
    assert complete["qualification"] == "BLOCKED"
