"""SEC issuer/ticker observations using the existing SecClient transport; no epochs."""

import hashlib
import re
from html.parser import HTMLParser

import requests

from trading_system.data.sec_client import SecResourceNotFound
from trading_system.data.security_reference import normalize_cik


class _Facts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current = None
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get("name", "")
        if tag == "ix:nonnumeric" and name in {
            "dei:TradingSymbol",
            "dei:EntityCentralIndexKey",
            "dei:EntityRegistrantName",
            "dei:DocumentPeriodEndDate",
            "dei:DocumentType",
        }:
            self.current = (name, [])

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "ix:nonnumeric" and self.current is not None:
            name, chunks = self.current
            self.values.setdefault(name, []).append(" ".join("".join(chunks).split()))
            self.current = None


def parse_sec_filing(
    html, *, cik, accession, filing_date, source_reference, form=None, period=None
):
    parser = _Facts()
    parser.feed(html)
    facts = {k: sorted(set(v)) for k, v in parser.values.items()}
    ciks = sorted(
        {c for v in facts.get("dei:EntityCentralIndexKey", []) if (c := normalize_cik(v))}
    )
    return {
        "source": "SEC_EDGAR",
        "source_reference": source_reference,
        "cik_context": cik,
        "document_ciks": ciks,
        "trading_symbols": facts.get("dei:TradingSymbol", []),
        "entity_names": facts.get("dei:EntityRegistrantName", []),
        "filing_date": filing_date,
        "period": period,
        "form": form,
        "accession": accession,
        "facts": facts,
        "response_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "issuer_binding_confirmed": ciks == [cik] and bool(facts.get("dei:TradingSymbol")),
        "interval_proof": False,
    }


def filing_url(cik, accession, document):
    if (
        not re.fullmatch(r"\d{10}", cik)
        or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.htm[l]?", document)
    ):
        raise ValueError("SEC_RESEARCH_FILING_REFERENCE_REFUSED")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession.replace('-', '')}/{document}"
    )


def fetch_sec_filing(client, *, cik, accession, document, filing_date, form=None, period=None):
    url = filing_url(cik, accession, document)
    try:
        response = client._get(url, resource_type="identity_research_filing", cik=cik)
        try:
            if len(response.content) > 12_000_000:
                raise ValueError("SEC_RESEARCH_FILING_TOO_LARGE")
            return {
                "http_status": 200,
                **parse_sec_filing(
                    response.text,
                    cik=cik,
                    accession=accession,
                    filing_date=filing_date,
                    source_reference=url,
                    form=form,
                    period=period,
                ),
            }
        finally:
            response.close()
    except SecResourceNotFound:
        status = 404
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else None
    return {
        "source": "SEC_EDGAR",
        "source_reference": url,
        "http_status": status,
        "cik_context": cik,
        "accession": accession,
        "filing_date": filing_date,
        "trading_symbols": [],
        "issuer_binding_confirmed": False,
        "interval_proof": False,
    }
