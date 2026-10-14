"""Source-backed symbol/security lifecycle intervals; never inferred from prices.

An interval is the complete membership of the *specified common-share security*
under this symbol, [effective_start, effective_end). An open end asserts ongoing
membership. A dated reference snapshot is not such an interval. observed_at is
audit time, not an effective date or a claim of historical information availability.
"""

from dataclasses import asdict, dataclass
from datetime import date, datetime

ACTIVE = "VERIFIED_ACTIVE_EPOCH"
OUTSIDE = "VERIFIED_OUTSIDE_EPOCH"
UNKNOWN = "IDENTITY_EPOCH_UNKNOWN"
LIFECYCLE_EVIDENCE = "AUTHORITATIVE_COMMON_STOCK_SYMBOL_LIFECYCLE"
EPOCH_SEMANTICS = "SOURCE_VERIFIED_SECURITY_INTERVAL;END_EXCLUSIVE;UNKNOWN_FAIL_CLOSED_V1"


@dataclass(frozen=True)
class SymbolIdentityEpoch:
    symbol: str
    effective_start: date
    effective_end: date | None
    source: str
    evidence_type: str
    verified: bool
    observed_at: str
    evidence_reference: str
    cik: str | None = None
    stable_security_identity: str | None = None

    def __post_init__(self):
        if self.effective_end is not None and self.effective_end <= self.effective_start:
            raise ValueError("IDENTITY_EPOCH_INVALID_INTERVAL")
        if self.verified and (
            not self.cik
            or not self.cik.strip()
            or not self.stable_security_identity
            or not self.stable_security_identity.rpartition(":")[2].strip()
        ):
            raise ValueError("IDENTITY_EPOCH_SECURITY_IDENTITY_REQUIRED")
        if self.verified and (
            self.evidence_type != LIFECYCLE_EVIDENCE
            or not self.source.strip()
            or not self.evidence_reference.strip()
            or datetime.fromisoformat(self.observed_at).tzinfo is None
        ):
            raise ValueError("IDENTITY_EPOCH_TEMPORAL_EVIDENCE_REQUIRED")

    def contains(self, session):
        return self.effective_start <= session and (
            self.effective_end is None or session < self.effective_end
        )

    def record(self):
        return {
            **asdict(self),
            "effective_start": str(self.effective_start),
            "effective_end": str(self.effective_end) if self.effective_end else None,
        }

    @classmethod
    def from_record(cls, row):
        return cls(
            **{
                **row,
                "effective_start": date.fromisoformat(row["effective_start"]),
                "effective_end": date.fromisoformat(row["effective_end"])
                if row["effective_end"]
                else None,
                "verified": row["verified"] in (True, 1),
            }
        )


def _matches_current(epoch, identity):
    # Issuer equality cannot bind a particular security/share class.
    if not epoch.cik or epoch.symbol != identity["symbol"] or epoch.cik != identity.get("cik"):
        return False
    stable = epoch.stable_security_identity
    if not stable or not stable.rpartition(":")[2].strip():
        return False
    current = {identity.get("stable_security_identity")}
    for field in ("share_class_figi", "composite_figi"):
        if identity.get(field):
            current.add(f"{field.upper()}:{identity[field]}")
    return stable in current


def current_identity_epoch(identity, records):
    """Resolve one exact current-security interval, conservatively rejecting ambiguity.

    Earlier, differently identified securities may also have records. Multiple
    intervals for the current security are intentionally unresolved in V1 rather
    than stitched together. Conflicting overlapping symbol mappings fail closed.
    """
    try:
        epochs = [SymbolIdentityEpoch.from_record(row) for row in records]
        matches = [e for e in epochs if _matches_current(e, identity)]
        if len(matches) != 1 or not matches[0].verified:
            return None
        epoch = matches[0]
        for other in epochs:
            if other is epoch or not other.verified:
                continue
            if (epoch.effective_end is None or other.effective_start < epoch.effective_end) and (
                other.effective_end is None or epoch.effective_start < other.effective_end
            ):
                return None
        return epoch.record()
    except (KeyError, TypeError, ValueError, AttributeError):
        # Legacy verified rows without concrete security IDs remain stored but unresolved.
        return None


def identity_epoch(identity):
    try:
        epoch = SymbolIdentityEpoch.from_record(identity["identity_epoch"])
        return epoch if epoch.verified and _matches_current(epoch, identity) else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def epoch_status(identity, session):
    epoch = identity_epoch(identity)
    return UNKNOWN if epoch is None else ACTIVE if epoch.contains(session) else OUTSIDE


def same_epoch_window(identity_by_symbol, symbols, sessions):
    """Require every observation of each leg, including T, in its single interval."""
    for symbol in symbols:
        epoch = identity_epoch(identity_by_symbol.get(symbol, {}))
        if epoch is None:
            return UNKNOWN
        if any(not epoch.contains(day) for day in sessions):
            return "IDENTITY_EPOCH_CROSSING"
    return ACTIVE
