"""Fail-closed security identity boundary; no provider or name-based inference.

The persisted Alpaca asset class and SEC company/SIC identity do not establish
per-security operating-company common-stock status. No audited local source
currently supplies that distinction. A future adapter must persist authoritative
symbol/security identity, category, issuer kind, source and provenance before
replacing this unavailable result. Existing asset/SEC sync cannot populate it.
"""

from dataclasses import dataclass

UNAVAILABLE = "AUTHORITATIVE_COMPANY_SECURITY_TYPE_UNAVAILABLE"
INSTRUMENT_TYPE_SEMANTICS = (
    "AUTHORITATIVE_PER_SECURITY_TYPE_AND_OPERATING_COMPANY;UNKNOWN_FAIL_CLOSED"
)
COMPANY_ONLY_REQUIREMENT = "COMMON_STOCK_AND_OPERATING_COMPANY_WITH_AUTHORITATIVE_SOURCE"


@dataclass(frozen=True)
class SecurityTypeEvidence:
    security_type: str = "UNKNOWN"
    operating_company: bool | None = None
    source: str | None = None

    @property
    def resolved(self):
        return (
            bool(self.source)
            and self.security_type
            in {"COMMON_STOCK", "ETF", "ETP", "FUND", "WARRANT", "UNIT", "OTHER"}
            and (self.security_type != "COMMON_STOCK" or self.operating_company is not None)
        )

    @property
    def company_common_stock(self):
        return (
            self.resolved
            and self.security_type == "COMMON_STOCK"
            and self.operating_company is True
        )


def local_security_types(database, symbols):
    """Explicit unsupported capability, not an inference from US_EQUITY or SIC.

    There is deliberately no config override or user-supplied category file that
    could masquerade as an authoritative source. Tests inject synthetic evidence
    at this adapter boundary; production remains unavailable until a source exists.
    """
    return {symbol: SecurityTypeEvidence() for symbol in symbols}
