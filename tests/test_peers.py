import pandas as pd
import pytest

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


@pytest.mark.parametrize("sic", [None, float("nan"), pd.NA, "", "nan", "bad", "35x1", "12345"])
def test_missing_or_malformed_sic_remains_unavailable(sic) -> None:
    frame = pd.DataFrame({"symbol": ["A"], "sic": [sic], "pe": [10]})
    result = assign_peer_groups(frame, min_peer_count=1)
    assert result["sic_normalized"].isna().all()
    assert result["peer_group"].isna().all()
    assert result["industry_median_pe"].isna().all()
    pd.testing.assert_frame_equal(frame, result[frame.columns])


@pytest.mark.parametrize(
    ("sics", "group"),
    [
        (["3571"] * 4, "sic4:3571"),
        (["7372"] * 2 + ["7373"] * 2, "sic3:737"),
        (["2811"] * 2 + ["2834"] * 2, "sic2:28"),
    ],
)
def test_valid_sic_grouping_unchanged_in_mixed_frame(sics, group) -> None:
    frame = pd.DataFrame({"sic": sics, "pe": [10, 12, 14, 16]})
    baseline = assign_peer_groups(frame, min_peer_count=4)
    missing = pd.DataFrame({"sic": [None, float("nan"), pd.NA, "bad"], "pe": [100] * 4})
    mixed = pd.concat([frame, missing], ignore_index=True)
    result = assign_peer_groups(mixed, min_peer_count=4)
    assert baseline["peer_group"].tolist() == [group] * 4
    assert baseline["industry_median_pe"].tolist() == [13] * 4
    pd.testing.assert_frame_equal(result.iloc[:4], baseline)
    assert result.iloc[4:]["sic_normalized"].isna().all()
    assert result.iloc[4:]["peer_group"].isna().all()
    assert result.iloc[4:]["industry_median_pe"].isna().all()
    pd.testing.assert_frame_equal(mixed, result[mixed.columns])


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
