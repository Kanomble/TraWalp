"""Public TraderNews research only; effective points never become complete epochs."""

import hashlib
import re
import time
from datetime import datetime
from html.parser import HTMLParser

import requests


def _date(value):
    for pattern in ("%A, %B %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    return None


class _Cells(HTMLParser):
    """Collect cells, including TraderNews tables with missing tr/td end tags."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.stack, self.cell = [], [], None

    def finish(self):
        if self.cell is not None:
            table, chunks = self.cell
            self.tables[table].append(" ".join(" ".join(chunks).split()))
            self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.finish()
            self.tables.append([])
            self.stack.append(len(self.tables) - 1)
        elif tag in {"th", "td"} and self.stack:
            self.finish()
            self.cell = (self.stack[-1], [])
        elif tag in {"br", "p"} and self.cell is not None:
            self.cell[1].append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th", "tr"}:
            self.finish()
        elif tag == "table" and self.stack:
            self.finish()
            self.stack.pop()

    def handle_data(self, data):
        if self.cell is not None:
            self.cell[1].append(data)


def parse_nasdaq_notice(html, notice_id):
    parser = _Cells()
    parser.feed(html)
    parser.finish()
    reference = f"https://www.nasdaqtrader.com/TraderNews.aspx?id={notice_id}"
    records = []
    labels = {
        "Current Symbol",
        "New Symbol",
        "Current CUSIP",
        "New CUSIP",
        "Current Company Name & Issue Type",
        "New Company Name & Issue Type",
        "Marketplace Effective Date",
    }
    for cells in parser.tables:
        fields = {
            cell.rstrip(":"): cells[i + 1]
            for i, cell in enumerate(cells[:-1])
            if cell.rstrip(":") in labels
        }
        if fields.get("Current Symbol") and fields.get("New Symbol"):
            records.append(
                {
                    "source": "NASDAQ_TRADER",
                    "source_reference": reference,
                    "old_symbol": fields["Current Symbol"],
                    "new_symbol": fields["New Symbol"],
                    "old_cusip": fields.get("Current CUSIP"),
                    "new_cusip": fields.get("New CUSIP"),
                    "effective_date": _date(fields.get("Marketplace Effective Date", "")),
                    "date_semantics": "MARKETPLACE_EFFECTIVE_DATE",
                    "event_type": "NAME_SYMBOL_CUSIP_CHANGE",
                    "raw_fields": fields,
                    "complete_interval": False,
                }
            )
        # The actual DTN transfer layout uses dated column headings and an
        # Ordinary/Units/Warrants cell. Keep the explicitly labelled Ordinary leg.
        old = next(
            (i for i, c in enumerate(cells) if c.startswith("NYSE Ticker/CUSIP through ")), None
        )
        new = next(
            (i for i, c in enumerate(cells) if c.startswith("NASDAQ Ticker/CUSIP as of ")), None
        )
        if old is not None and new is not None:
            width = 6  # Six columns in this reviewed public DTN layout.
            if old + width < len(cells) and new + width < len(cells):
                before = re.search(
                    r"\bOrdinary\s+([A-Z0-9.]+):\s*([A-Z0-9]{9})\b", cells[old + width]
                )
                after = re.search(
                    r"\bOrdinary\s+([A-Z0-9.]+):\s*([A-Z0-9]{9})\b", cells[new + width]
                )
                if before and after:
                    records.append(
                        {
                            "source": "NASDAQ_TRADER",
                            "source_reference": reference,
                            "old_symbol": before[1],
                            "new_symbol": after[1],
                            "old_cusip": before[2],
                            "new_cusip": after[2],
                            "effective_date": _date(cells[new].split(" as of ", 1)[1]),
                            "old_valid_through": _date(cells[old].split(" through ", 1)[1]),
                            "date_semantics": "EXCHANGE_TRANSFER_AS_OF",
                            "event_type": "EXCHANGE_TRANSFER",
                            "issue_type": "Ordinary",
                            "raw_fields": {
                                cells[old]: cells[old + width],
                                cells[new]: cells[new + width],
                            },
                            "complete_interval": False,
                        }
                    )
    return {
        "source_reference": reference,
        "records": records,
        "response_sha256": hashlib.sha256(html.encode()).hexdigest(),
    }


class NasdaqLifecycleResearchClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.requests = 0
        self._last = None

    def notice(self, notice_id):
        if not re.fullmatch(r"(?:ECA|dtn)\d{4}-\d{1,4}", notice_id) or self.requests >= 8:
            raise ValueError("NASDAQ_RESEARCH_SCOPE_REFUSED")
        if self._last is not None:
            time.sleep(max(0, 1 - (time.monotonic() - self._last)))
        self._last = time.monotonic()
        self.requests += 1
        try:
            response = self.session.get(
                "https://www.nasdaqtrader.com/TraderNews.aspx",
                params={"id": notice_id},
                timeout=(10, 20),
                allow_redirects=False,
            )
            self._last = time.monotonic()
            try:
                if response.status_code != 200:
                    return {
                        "notice_id": notice_id,
                        "http_status": response.status_code,
                        "records": [],
                    }
                if len(response.content) > 2_000_000:
                    raise ValueError("NASDAQ_RESEARCH_RESPONSE_TOO_LARGE")
                return {
                    "notice_id": notice_id,
                    "http_status": 200,
                    **parse_nasdaq_notice(response.text, notice_id),
                }
            finally:
                response.close()
        except requests.RequestException:
            return {
                "notice_id": notice_id,
                "http_status": None,
                "records": [],
                "error": "NETWORK_FAILED",
            }

    def close(self):
        self.session.close()
