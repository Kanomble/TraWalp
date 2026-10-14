"""Local authoritative common-stock boundary; SEC owns issuer identity."""

from dataclasses import dataclass

from trading_system.data.security_reference import (
    REFERENCE_SEMANTICS,
    SECURITY_TYPES,
    SOURCE,
    SOURCE_VERSION,
)

UNAVAILABLE = "AUTHORITATIVE_COMPANY_SECURITY_TYPE_UNAVAILABLE"
INSTRUMENT_TYPE_SEMANTICS = REFERENCE_SEMANTICS + ";UNKNOWN_FAIL_CLOSED"
COMPANY_ONLY_REQUIREMENT = "COMMON_STOCK_WITH_SEC_ISSUER_IDENTITY"


@dataclass(frozen=True)
class SecurityTypeEvidence:
    security_type: str = "UNKNOWN"
    source: str | None = None
    provider_security_type: str | None = None
    source_version: str | None = None
    provider_cik: str | None = None
    primary_exchange: str | None = None
    composite_figi: str | None = None
    share_class_figi: str | None = None
    provider_updated_at: str | None = None
    reference_present: bool = False

    @property
    def resolved(self):
        return (
            self.source == SOURCE
            and self.source_version == SOURCE_VERSION
            and self.security_type in SECURITY_TYPES
            and self.security_type != "UNKNOWN"
        )

    @property
    def company_common_stock(self):
        # Issuer/SIC/CIK checks are applied separately in company_universe.
        return self.resolved and self.security_type == "COMMON_STOCK"


def local_security_types(database, symbols, *, reference_snapshot=None):
    """Read current local snapshot only. No client, credentials or network imports."""
    records, _ = (
        reference_snapshot
        if reference_snapshot is not None
        else database.security_reference_snapshot()
    )
    result = {}
    for symbol in symbols:
        row = records.get(symbol)
        if row is None:
            result[symbol] = SecurityTypeEvidence()
            continue
        valid_scope = row["market"] == "stocks" and row["locale"] == "us" and row["active"]
        result[symbol] = SecurityTypeEvidence(
            security_type=row["security_type"] if valid_scope else "UNKNOWN",
            source=row["source"],
            provider_security_type=row["provider_security_type"],
            source_version=row["source_version"],
            provider_cik=row["cik"],
            primary_exchange=row["primary_exchange"],
            composite_figi=row["composite_figi"],
            share_class_figi=row["share_class_figi"],
            provider_updated_at=row["provider_updated_at"],
            reference_present=True,
        )
    return result
