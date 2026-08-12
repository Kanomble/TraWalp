"""SIC peer grouping and industry-relative metric helpers."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from trading_system.models.screening import PeerDebug

PEER_METRICS = (
    "pe",
    "ev_to_ebitda",
    "ev_to_ebit",
    "operating_margin",
    "roic",
    "revenue_growth",
)


def normalize_sic(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text.zfill(4) if text.isdigit() and len(text) <= 4 else None


def assign_peer_groups(frame: pd.DataFrame, min_peer_count: int = 8) -> pd.DataFrame:
    """Choose the narrowest sufficiently populated 4/3/2-digit SIC group per company."""

    if "sic" not in frame.columns:
        raise ValueError("Peer input requires a sic column")
    output = frame.copy()
    output["sic_normalized"] = output["sic"].map(normalize_sic)
    valid_sics = output["sic_normalized"].dropna()
    counts = {width: valid_sics.str[:width].value_counts().to_dict() for width in (4, 3, 2)}

    def group_for(sic: str | None) -> str | None:
        if sic is None:
            return None
        for width in (4, 3, 2):
            prefix = sic[:width]
            if counts[width].get(prefix, 0) >= min_peer_count:
                return f"sic{width}:{prefix}"
        return None

    output["peer_group"] = output["sic_normalized"].map(group_for)
    for metric in PEER_METRICS:
        if metric in output.columns:
            numeric = pd.to_numeric(output[metric], errors="coerce")
            if metric in {"pe", "ev_to_ebitda", "ev_to_ebit"}:
                numeric = numeric.where(numeric > 0)
            grouped = numeric.groupby(output["peer_group"])
            output[f"industry_median_{metric}"] = grouped.transform("median").where(
                grouped.transform("count") >= min_peer_count
            )
    return output


def peer_median(values: Iterable[float | None]) -> float | None:
    series = pd.Series(list(values), dtype=float).dropna()
    return float(series.median()) if not series.empty else None


def relative_multiple(company: float | None, industry_median: float | None) -> float | None:
    """Negative/zero multiples are economically invalid and remain unavailable."""

    if company is None or industry_median is None or company <= 0 or industry_median <= 0:
        return None
    return company / industry_median


def peer_diagnostics(
    frame: pd.DataFrame, symbol: str, sic: str | None, min_peer_count: int
) -> PeerDebug:
    normalized = normalize_sic(sic)
    sic_series = (
        frame["sic_normalized"]
        if "sic_normalized" in frame.columns
        else frame.get("sic", pd.Series(dtype=object)).map(normalize_sic)
    )

    def subset(width: int) -> pd.DataFrame:
        if normalized is None:
            return frame.iloc[0:0]
        return frame.loc[sic_series.str[:width] == normalized[:width]]

    groups = {width: subset(width) for width in (4, 3, 2)}
    selected_width = next(
        (width for width in (4, 3, 2) if len(groups[width]) >= min_peer_count), None
    )
    selected = groups[selected_width] if selected_width else frame.iloc[0:0]
    diagnostic_group = selected if selected_width else groups[2]

    def valid(metric: str) -> pd.Series:
        if metric not in diagnostic_group.columns:
            return pd.Series(dtype=float)
        values = pd.to_numeric(diagnostic_group[metric], errors="coerce")
        return values.loc[values > 0].dropna()

    pe = valid("pe")
    ev_ebitda = valid("ev_to_ebitda")
    ev_ebit = valid("ev_to_ebit")
    return PeerDebug(
        symbol=symbol,
        sic=normalized,
        exact_peer_count=len(groups[4]),
        three_digit_peer_count=len(groups[3]),
        two_digit_peer_count=len(groups[2]),
        selected_group=f"sic{selected_width}:{normalized[:selected_width]}"
        if selected_width and normalized
        else None,
        selected_peer_count=len(selected),
        valid_pe_count=len(pe),
        valid_ev_ebitda_count=len(ev_ebitda),
        valid_ev_ebit_count=len(ev_ebit),
        median_pe=float(pe.median()) if len(pe) >= min_peer_count else None,
        median_ev_ebitda=(float(ev_ebitda.median()) if len(ev_ebitda) >= min_peer_count else None),
        median_ev_ebit=float(ev_ebit.median()) if len(ev_ebit) >= min_peer_count else None,
        minimum_peer_count=min_peer_count,
    )
