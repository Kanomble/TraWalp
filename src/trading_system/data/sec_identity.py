"""Shared SEC/current-symbol identity resolution without network access."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SecIdentityConflict:
    """A proposed SEC identity that contradicts a persisted canonical identity."""

    symbol: str
    proposed_cik: str
    existing_cik: str
    existing_symbol: str
    source: str


@dataclass(frozen=True)
class SecIdentityResolution:
    ticker_map: dict[str, str]
    alias_mappings: dict[str, str]
    canonical_symbols: dict[str, str]
    conflicts: tuple[SecIdentityConflict, ...]


def _security_symbol_priority(symbol: str) -> tuple[int, str]:
    structural = (".WS", ".RT", ".U", ".PR")
    return (int(any(marker in symbol for marker in structural)), symbol)


def resolve_sec_identities(
    symbols: set[str],
    persisted: Mapping[str, str],
    current_sec: Mapping[str, str],
) -> SecIdentityResolution:
    """Resolve current SEC identities without silently moving persisted symbols or CIKs."""

    ticker_map: dict[str, str] = {}
    conflicts: list[SecIdentityConflict] = []
    for symbol in sorted(symbols):
        persisted_cik = persisted.get(symbol)
        current_cik = current_sec.get(symbol)
        if persisted_cik is not None and current_cik is not None and persisted_cik != current_cik:
            conflicts.append(
                SecIdentityConflict(
                    symbol=symbol,
                    proposed_cik=current_cik,
                    existing_cik=persisted_cik,
                    existing_symbol=symbol,
                    source="exact_sec_ticker",
                )
            )
            ticker_map[symbol] = persisted_cik
        elif current_cik is not None:
            ticker_map[symbol] = current_cik
        elif persisted_cik is not None:
            ticker_map[symbol] = persisted_cik

    alias_mappings = {
        symbol: current_sec[symbol.replace(".", "-")]
        for symbol in symbols - ticker_map.keys()
        if "." in symbol and symbol.replace(".", "-") in current_sec
    }
    ticker_map.update(alias_mappings)

    cik_symbols: dict[str, list[str]] = defaultdict(list)
    for symbol, cik in sorted(ticker_map.items()):
        cik_symbols[cik].append(symbol)
    existing_by_cik = {cik: symbol for symbol, cik in persisted.items()}
    canonical_symbols: dict[str, str] = {}
    for cik, candidates in cik_symbols.items():
        existing_symbol = existing_by_cik.get(cik)
        if existing_symbol is None:
            canonical_symbols[cik] = min(candidates, key=_security_symbol_priority)
        elif existing_symbol in candidates:
            canonical_symbols[cik] = existing_symbol
        else:
            proposed_symbol = min(candidates, key=_security_symbol_priority)
            conflicts.append(
                SecIdentityConflict(
                    symbol=proposed_symbol,
                    proposed_cik=cik,
                    existing_cik=cik,
                    existing_symbol=existing_symbol,
                    source=(
                        "dot_hyphen_alias"
                        if proposed_symbol in alias_mappings
                        else "exact_sec_ticker"
                    ),
                )
            )

    return SecIdentityResolution(
        ticker_map=ticker_map,
        alias_mappings=alias_mappings,
        canonical_symbols=canonical_symbols,
        conflicts=tuple(conflicts),
    )


def identity_conflict_is_resolved(
    symbol: str,
    persisted: Mapping[str, str],
    current_sec: Mapping[str, str],
) -> bool:
    persisted_cik = persisted.get(symbol)
    if persisted_cik is None:
        return True
    current_cik = current_sec.get(symbol)
    if current_cik == persisted_cik:
        return True
    return bool("." in symbol and current_sec.get(symbol.replace(".", "-")) == persisted_cik)
