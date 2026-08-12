import pandas as pd

from trading_system.fundamentals.peers import (
    assign_peer_groups,
    peer_diagnostics,
    peer_median,
    relative_multiple,
)


def test_peer_group_falls_back_from_four_to_three_digit_sic() -> None:
    frame = pd.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(8)],
            "sic": ["3571"] * 4 + ["3572"] * 4,
            "pe": [10, 12, 14, 16, 18, 20, 22, 24],
        }
    )
    peers = assign_peer_groups(frame, min_peer_count=8)
    assert set(peers["peer_group"]) == {"sic3:357"}
    assert peers["industry_median_pe"].iloc[0] == 17


def test_empty_or_too_small_peer_group_remains_unavailable() -> None:
    frame = pd.DataFrame({"symbol": ["A"], "sic": [None], "pe": [10]})
    result = assign_peer_groups(frame, min_peer_count=2)
    assert result["peer_group"].isna().all()
    assert result["industry_median_pe"].isna().all()
    assert peer_median([]) is None


def test_relative_multiple_rejects_non_positive_values() -> None:
    assert relative_multiple(18, 24) == 0.75
    assert relative_multiple(-5, 24) is None
    assert relative_multiple(18, 0) is None


def test_peer_diagnostics_reports_fallback_and_insufficient_group() -> None:
    frame = pd.DataFrame(
        {
            "symbol": [f"S{i}" for i in range(8)],
            "sic": ["7372"] * 3 + ["7373"] * 5,
            "pe": [10, 12, None, 14, 16, 18, 20, 22],
            "ev_to_ebitda": [8, 9, None, 10, 11, 12, 13, 14],
        }
    )
    grouped = assign_peer_groups(frame, min_peer_count=8)
    debug = peer_diagnostics(grouped, "MSFT", "7372", 8)
    assert debug.exact_peer_count == 3
    assert debug.three_digit_peer_count == 8
    assert debug.selected_group == "sic3:737"
    assert debug.valid_pe_count == 7
    assert debug.median_pe is None  # valid observations remain below configured minimum

    insufficient = peer_diagnostics(grouped.iloc[:3], "MSFT", "7372", 8)
    assert insufficient.selected_group is None
    assert insufficient.selected_peer_count == 0
