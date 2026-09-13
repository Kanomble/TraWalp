"""Frozen pre-refactor scoring reference, used only in local regression/benchmark tests."""

from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from trading_system.fundamentals.peers import assign_peer_groups, peer_diagnostics
from trading_system.models.fundamentals import CompanyIdentity
from trading_system.models.screening import ScreenRecord, ScreenReport
from trading_system.strategy.screener import (
    INDUSTRY_METRICS,
    LOGGER,
    PEER_VALUE_METRICS,
    Screener,
    _optional_float,
    _optional_float_list,
    _optional_string,
    _PreparedCandidate,
)


class ReferenceScreener(Screener):
    def report_from_prepared(
        self,
        prepared: list[_PreparedCandidate],
        conflicted_companies: list[CompanyIdentity],
        *,
        requested_as_of: date,
        market_session: date,
    ) -> ScreenReport:
        """Score a prepared PIT cross-section using the canonical peer/ranking logic."""

        peer_table = self._peer_table(prepared)
        records = [self._score(candidate, peer_table) for candidate in prepared]
        records.extend(
            self._identity_conflict_record(company, market_session)
            for company in conflicted_companies
        )

        eligible = sorted(
            (record for record in records if record.eligible),
            key=lambda record: (
                -(record.scores.total or 0),
                -(record.scores.quality.score or 0),
                -(record.scores.valuation.score or 0),
                record.symbol,
            ),
        )
        ranks = {record.symbol: index for index, record in enumerate(eligible, start=1)}
        ranked = [
            record.model_copy(update={"rank": ranks.get(record.symbol)}) for record in records
        ]
        ranked.sort(
            key=lambda record: (
                record.rank is None,
                record.rank or 0,
                -(record.scores.total or -1),
                record.symbol,
            )
        )
        return ScreenReport(
            as_of=market_session,
            requested_as_of=requested_as_of,
            effective_market_session=market_session,
            generated_at=datetime.now(UTC).isoformat(),
            analyzed_count=len(ranked),
            eligible_count=len(eligible),
            identity_conflicts_excluded=len(conflicted_companies),
            identity_conflict_sample=tuple(
                sorted(company.symbol for company in conflicted_companies)[:10]
            ),
            records=tuple(ranked),
        )

    def _peer_table(self, prepared: list[_PreparedCandidate]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for candidate in prepared:
            if candidate.base_exclusions:
                continue
            row = {
                "symbol": candidate.company.symbol,
                "sic": candidate.company.sic,
                **candidate.fundamentals.model_dump(mode="python"),
            }
            rows.append(row)
        columns = list(dict.fromkeys(("symbol", "sic", *INDUSTRY_METRICS, *PEER_VALUE_METRICS)))
        frame = pd.DataFrame(rows)
        if frame.empty:
            return pd.DataFrame(columns=[*columns, "peer_group"])
        output = assign_peer_groups(frame, self.config.peers.min_peer_count)
        for sic in output["sic_normalized"].dropna().unique():
            diagnostic = peer_diagnostics(output, "*", str(sic), self.config.peers.min_peer_count)
            logged_group = diagnostic.selected_group or f"sic2:{str(sic)[:2]} (insufficient)"
            logged_count = (
                diagnostic.selected_peer_count
                if diagnostic.selected_group
                else diagnostic.two_digit_peer_count
            )
            LOGGER.debug(
                "Peer group sic=%s group=%s companies=%d valid_pe=%d "
                "valid_ev_ebitda=%d median_pe=%s median_ev_ebitda=%s",
                sic,
                logged_group,
                logged_count,
                diagnostic.valid_pe_count,
                diagnostic.valid_ev_ebitda_count,
                diagnostic.median_pe,
                diagnostic.median_ev_ebitda,
            )
        return output

    def _score(self, candidate: _PreparedCandidate, peer_table: pd.DataFrame) -> ScreenRecord:
        # Base-excluded candidates are intentionally absent from ``peer_table``.
        # Avoid an O(peer rows) DataFrame scan for thousands of already rejected
        # symbols on every historical session; their score inputs remain exactly
        # the same empty-peer context as before.
        peer_row = (
            peer_table.iloc[0:0]
            if candidate.base_exclusions
            else peer_table.loc[peer_table["symbol"] == candidate.company.symbol]
        )
        peer_group = None if peer_row.empty else _optional_string(peer_row.iloc[0]["peer_group"])
        group = (
            peer_table.loc[peer_table["peer_group"] == peer_group]
            if peer_group is not None
            else peer_table.iloc[0:0]
        )
        peer_values: dict[str, list[float | None]] = {}
        for metric in PEER_VALUE_METRICS:
            values = _optional_float_list(group[metric]) if metric in group.columns else []
            valid_count = sum(value is not None for value in values)
            peer_values[metric] = values if valid_count >= self.config.peers.min_peer_count else []
        industry_medians = {
            metric: _optional_float(peer_row.iloc[0].get(f"industry_median_{metric}"))
            if not peer_row.empty
            else None
            for metric in INDUSTRY_METRICS
        }
        scores = self._scores(candidate, peer_values, industry_medians)
        exclusions = list(candidate.base_exclusions)
        if candidate.fundamentals_evaluated:
            exclusions.extend(self._hard_filter_exclusions(candidate.fundamentals, scores))
        if peer_group is None:
            candidate.data_warnings.append("insufficient_peer_group")
        eligible = not exclusions
        if scores.total is not None:
            LOGGER.debug(
                "Candidate %s score=%.1f eligible=%s",
                candidate.company.symbol,
                scores.total,
                eligible,
            )
        return ScreenRecord(
            symbol=candidate.company.symbol,
            name=candidate.company.name,
            as_of=candidate.analysis_date,
            sic=candidate.company.sic,
            peer_group=peer_group,
            eligible=eligible,
            exclusion_reasons=tuple(dict.fromkeys(exclusions)),
            data_warnings=tuple(dict.fromkeys(candidate.data_warnings)),
            average_dollar_volume_20d=candidate.average_dollar_volume_20d,
            industry_medians=industry_medians,
            fundamentals=candidate.fundamentals,
            technical=candidate.technical,
            scores=scores,
            market_history_count=candidate.market_history_count,
            pit_fact_count=candidate.pit_fact_count,
            estimated_market_cap=candidate.estimated_market_cap,
            latest_pit_filing_date=candidate.latest_pit_filing_date,
            latest_pit_period_end=candidate.latest_pit_period_end,
        )
