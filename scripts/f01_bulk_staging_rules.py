"""Companyfacts-only research staging rules. No Canonical tables or trading consumers."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from trading_system.data.xbrl_parser import VALID_FORMS
from trading_system.fundamentals.semantic import (
    BALANCE_TAGS,
    DEBT_COMPONENTS,
    FLOW_SERIES,
    TOTAL_DEBT,
)

START = date(2010, 1, 1)
FORMS = frozenset(VALID_FORMS) | {"40-F/A"}
ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")
# Only scoped aliases. Components of debt are separate, never synthesized into total debt.
INSTANT = BALANCE_TAGS | {"total_debt": TOTAL_DEBT}
DURATION = {
    tag: metric for metric, series in FLOW_SERIES.items() for tags in series for tag in tags
}
MAPPING = DURATION | {tag: metric for metric, tag in INSTANT.items()}
MAPPING |= {
    tag: kind
    for pair in DEBT_COMPONENTS
    for kind, tag in zip(("debt_current", "debt_noncurrent"), pair, strict=True)
}
METRICS = sorted(set(MAPPING.values()))
INSTANT_METRICS = {*INSTANT, "debt_current", "debt_noncurrent"}


def day(raw):
    if not isinstance(raw, str) or len(raw) != 10:
        return None
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.isoformat() == raw else None


def classified(raw, metric, unit, accession, metadata, conflicted):
    end, start, filed = (day(raw.get(k)) for k in ("end", "start", "filed"))
    if not end or not filed or end > filed or ("start" in raw and (start is None or start > end)):
        return "INVALID_PERIOD_OR_FILED", None, None
    if not isinstance(accession, str) or not ACCESSION.fullmatch(accession):
        return "INVALID_ACCESSION", None, None
    form = raw.get("form")
    if form not in FORMS:
        return "FORM_OUTSIDE_PERIODIC_SCOPE", None, None
    if accession in conflicted:
        return "SUBMISSIONS_CONFLICT", None, None
    known = metadata.get(accession)
    if known and known[:2] != (filed, form):
        return "SUBMISSIONS_FORM_OR_FILED_MISMATCH", None, None
    if not metric:
        return "UNMAPPED_OR_SCOPE_UNVERIFIED", None, None
    if (start is None) != (metric in INSTANT_METRICS):
        return "PERIOD_TYPE_MISMATCH", None, None
    if unit != ("USD/shares" if metric == "eps_diluted" else "USD"):
        return "UNIT_OUTSIDE_RESEARCH_SCOPE", None, None
    try:
        amount = Decimal(str(raw["val"]))
    except (InvalidOperation, KeyError, ValueError):
        return "INVALID_VALUE", None, None
    if not amount.is_finite() or abs(amount.as_tuple().exponent) > 1000:
        return "INVALID_VALUE", None, None
    # Formatting trims insignificant fractional zeros without Decimal context rounding.
    value = format(amount, "f")
    if "." in value:
        value = value.rstrip("0").rstrip(".")
    value = "0" if amount.is_zero() else value
    stamp = known[2] if known else None
    return "CANDIDATE", value, stamp.isoformat() if stamp else None


def process(payload, metadata, conflicted):
    observations, facts, groups = [], [], defaultdict(list)
    coverage = defaultdict(Counter)
    filings = {}
    counts = Counter()
    for taxonomy, concepts in sorted(payload["facts"].items()):
        for tag, concept in sorted(concepts.items()):
            metric = MAPPING.get(tag) if taxonomy == "us-gaap" else None
            for unit, rows in sorted(concept.get("units", {}).items()):
                for ordinal, raw in enumerate(rows):
                    counts["provider_observations_seen"] += 1
                    if not isinstance(raw, dict):
                        counts["non_object_quarantined"] += 1
                        observations.append(
                            (
                                taxonomy,
                                tag,
                                unit,
                                ordinal,
                                json.dumps(raw),
                                metric,
                                "NON_OBJECT",
                                None,
                            )
                        )
                        continue
                    end = day(raw.get("end"))
                    filed = day(raw.get("filed"))
                    # Use period end for valid observations; retain undatable errors.
                    if (end or filed) and (end or filed) < START:
                        counts["before_start_excluded"] += 1
                        continue
                    accession = raw.get("accn")
                    result, value, accepted = classified(
                        raw, metric, unit, accession, metadata, conflicted
                    )
                    coverage_key = (end.year, metric) if end and metric else None
                    if coverage_key:
                        coverage[coverage_key]["observations"] += 1
                    raw_json = json.dumps(raw, sort_keys=True, ensure_ascii=False)
                    index = len(observations)
                    observations.append(
                        (taxonomy, tag, unit, ordinal, raw_json, metric, result, None)
                    )
                    if (
                        isinstance(accession, str)
                        and ACCESSION.fullmatch(accession)
                        and raw.get("form") in FORMS
                        and filed
                    ):
                        filing = filings.setdefault(
                            accession,
                            {
                                "accession": accession,
                                "filed": filed.isoformat(),
                                "form": raw["form"],
                                "acceptance_utc": None,
                                "problems": set(),
                            },
                        )
                        known = metadata.get(accession)
                        if known:
                            filing.update(
                                filed=known[0].isoformat(),
                                form=known[1],
                                acceptance_utc=known[2].isoformat() if known[2] else None,
                            )
                        if not known:
                            filing["problems"].add("SUBMISSIONS_MISSING")
                        if result in {
                            "SUBMISSIONS_CONFLICT",
                            "SUBMISSIONS_FORM_OR_FILED_MISMATCH",
                            "INVALID_PERIOD_OR_FILED",
                        }:
                            filing["problems"].add(result)
                    if result == "CANDIDATE":
                        key = (
                            metric,
                            unit,
                            raw.get("start") or "",
                            raw["end"],
                            raw["filed"],
                            accession,
                        )
                        groups[key].append(
                            (index, (taxonomy, tag, value, raw.get("frame")), accepted)
                        )
                    else:
                        counts[result] += 1
                        if coverage_key:
                            coverage[coverage_key]["excluded"] += 1

    for key, members in sorted(groups.items()):
        metric, unit, start, end, filed, accession = key
        year = int(end[:4])
        if len({identity for _, identity, _ in members}) != 1:
            counts["AMBIGUOUS_CANDIDATE"] += len(members)
            coverage[year, metric]["ambiguous"] += len(members)
            filings[accession]["problems"].add("AMBIGUOUS_CANDIDATE")
            for index, _, _ in members:
                observations[index] = observations[index][:-2] + ("AMBIGUOUS_CANDIDATE", None)
            continue
        _, (taxonomy, tag, value, frame), accepted = members[0]
        facts.append(
            (
                metric,
                unit,
                start or None,
                end,
                filed,
                accession,
                taxonomy,
                tag,
                value,
                frame,
                accepted,
                len(members),
            )
        )
        fact_id = len(facts)
        counts["duplicate_occurrences_retained"] += len(members) - 1
        counts["retrospective_candidates"] += 1
        coverage[year, metric]["retrospective"] += 1
        if accepted:
            counts["acceptance_timestamp_bound"] += 1
            coverage[year, metric]["with_acceptance"] += 1
        for index, _, _ in members:
            observations[index] = observations[index][:-2] + ("STAGING_CANDIDATE", fact_id)
    return observations, facts, coverage, filings, counts
