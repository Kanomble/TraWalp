"""Lightweight cumulative stage diagnostics; no per-object profiling or retained samples."""

from dataclasses import asdict, dataclass


@dataclass
class DiscoveryDiagnostics:
    screen_session_seconds: float = 0.0
    candidate_prepare_seconds: float = 0.0
    peer_table_seconds: float = 0.0
    peer_group_lookup_seconds: float = 0.0
    scoring_seconds: float = 0.0
    report_construction_seconds: float = 0.0
    variant_f_evaluation_seconds: float = 0.0
    sessions_processed: int = 0
    companies_processed: int = 0
    prepared_candidates: int = 0
    peer_table_rows: int = 0
    eligible_screen_records: int = 0
    eligible_f_candidates: int = 0

    def as_dict(self):
        return asdict(self)
