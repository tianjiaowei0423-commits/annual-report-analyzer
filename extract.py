"""
Annual report analyzer - version 1: extract key financial figures.

Sectors: every company belongs to a sector (shipping, beverages). All sectors share the
14 core metrics; a sector can add its own metrics, page sections and starting rules
(SECTOR_METRICS, SECTOR_SECTIONS, SECTOR_RULES). Shipping output stays in output/,
other sectors go to output/<sector>/ so the shipping peer tables are not mixed up.

Steps:
  1. Read every page of the annual report PDF (pypdf).
  2. Locate the pages that matter (income statement, balance sheet,
     cash flow statement, key figures, financial items note, staff note)
     by keyword scoring, skipping parent company pages.
  3. Send only those pages, plus the rules in data/ground_truth.xlsx
     (sheet "extraction_rules"), to Claude.
  4. Save the 14 metrics, with unit, PDF page and note, to output/<company>_<year>.csv
     in the same layout as the ground truth sheet.

The 2025 reports also show 2024 as the comparative column, so --year 2024 reads the
same pages of the same reports and takes the 2024 column instead.

Usage:
  python extract.py DFDS --dry-run   # only show which pages would be sent (free)
  python extract.py DFDS             # run the extraction (calls the API)
  python extract.py all              # every shipping company (default sector)
  python extract.py all --sector beverages   # every beverage company
  python extract.py all --year 2024  # prior-year column from the same 2025 reports
  python extract.py DFDS --show-page 123   # debug one page (free)
"""

import argparse
import json
import logging
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pypdf import PdfReader

logging.getLogger("pypdf").setLevel(logging.ERROR)  # hide font warnings

MODEL = "claude-sonnet-5-5"
YEAR = 2025  # year of the annual reports in data/reports
REPORTS_DIR = Path("data/reports")
GROUND_TRUTH = Path("data/ground_truth.xlsx")
OUTPUT_DIR = Path("output")

COMPANIES = {
    "DFDS": {"file": "dfds_2025.pdf", "currency": "DKK", "sector": "shipping"},
    "Maersk": {"file": "maersk_2025.pdf", "currency": "USD", "sector": "shipping"},
    "Norden": {"file": "norden_2025.pdf", "currency": "USD", "sector": "shipping"},
    "TORM": {"file": "torm_2025.pdf", "currency": "USD", "sector": "shipping"},
    # beverages: acquirer + peer set decided 2026-11-20 (see notes/assumptions_log.md)
    "Carlsberg": {"file": "carlsberg_2025.pdf", "currency": "DKK", "sector": "beverages"},
    "Heineken": {"file": "heineken_2025.pdf", "currency": "EUR", "sector": "beverages"},
    "ABInBev": {"file": "abinbev_2025.pdf", "currency": "USD", "sector": "beverages"},
    "RoyalUnibrew": {"file": "royalunibrew_2025.pdf", "currency": "DKK", "sector": "beverages"},
    "Asahi": {"file": "asahi_2025.pdf", "currency": "JPY", "sector": "beverages"},
    "CCHBC": {"file": "hbc_2025.pdf", "currency": "EUR", "sector": "beverages"},
    "CCEP": {"file": "ccep_2025.pdf", "currency": "EUR", "sector": "beverages"},
    "AGBarr": {"file": "agbarr_2025.pdf", "currency": "GBP", "sector": "beverages",
               "fy_note": "A.G. Barr's financial year 2025 is the 53 weeks ended 31 January 2026; "
                          "its column may be headed 2026 or FY25/26. Take that column, not the "
                          "year ended January 2025."},
    "Monster": {"file": "monster_2025.pdf", "currency": "USD", "sector": "beverages"},
}
DEFAULT_SECTOR = "shipping"

CORE_METRICS = {
    "revenue": "Revenue",
    "ebitda": "EBITDA",
    "ebit": "EBIT / operating profit",
    "net_profit": "Profit for the year (group total)",
    "interest_expense": "Gross interest expense on borrowings and lease liabilities",
    "total_assets": "Total assets",
    "total_equity": "Total equity incl. non-controlling interests",
    "cash": "Cash and cash equivalents (balance sheet)",
    "total_borrowings": "All interest-bearing liabilities incl. lease liabilities",
    "net_interest_bearing_debt": "Net interest-bearing debt (company-reported)",
    "operating_cash_flow": "Cash flow from operating activities",
    "capex": "Gross capital expenditure (PP&E + intangibles)",
    "free_cash_flow": "Free cash flow (company-reported)",
    "employees": "Number of employees (prefer average FTE)",
}

# Extra metrics per sector, on top of the 14 core metrics.
SECTOR_METRICS = {
    "beverages": {
        "volume_total": "Total sales volume in million hectolitres, beer and soft drinks together (group)",
        "organic_volume_growth": "Organic volume growth, % (excl. acquisitions and divestments)",
        "organic_revenue_growth": "Organic revenue growth, % (excl. acquisitions, divestments and currency)",
        "organic_operating_profit_growth": "Organic operating profit growth, % (company-reported)",
        "gross_profit": "Gross profit",
        "sales_distribution_expenses": "Sales and distribution expenses (marketing and selling costs)",
        "special_items": "Special items, net, before tax (restructuring, impairment, deal and integration costs)",
        "operating_profit_adjusted": "Operating profit on the company's adjusted basis "
                                     "(e.g. before special items, or MPM), company-reported",
    },
}

# Starting rules for a sector, used next to the rules in ground_truth.xlsx. Refine them
# after the hand extraction, as was done for shipping.
SECTOR_RULES = {
    "beverages": [
        "[volume_total] Take total group volume in million hl (beer + soft drinks / other beverages). "
        "If several volume measures are shown (e.g. pro rata vs total), take the total one and name it in note",
        "[organic_volume_growth, organic_revenue_growth, organic_operating_profit_growth] Take the "
        "company's own organic figure as a percentage; never use reported growth, which includes "
        "acquisitions and currency. unit = percent",
        "[gross_profit] Take 'Gross profit' from the income statement; if there is none, revenue minus "
        "cost of sales, and say so in note",
        "[special_items] Take the net special items line before tax; costs are negative",
        "[operating_profit_adjusted] Take the company's adjusted operating profit (e.g. 'before special "
        "items' or '(MPM)'); in note, write which adjustments the company makes",
        "[ebit] EBIT = operating profit after special items. If the report has no such line, add "
        "'operating profit before special items' and 'special items, net', and list both parts in note",
        "[free_cash_flow] If the report shows both a free cash flow before acquisitions (e.g. 'free "
        "operating cash flow') and one after acquisitions ('free cash flow'), take the one before "
        "acquisitions; give the other figure in note",
        "[revenue] If the report shows revenue both before and after excise tax/duties, take the one AFTER "
        "excise (Heineken: 'Net revenue'; Carlsberg: 'Revenue' below 'Excise duties'). This overrides the "
        "general 'take Revenue' rule; give the other figure in note",
        "[ebitda] Take EBITDA before special/exceptional items, as the company reports it (Carlsberg: "
        "'EBITDA'; Heineken: 'EBITDA (beia)'). Give the reported EBITDA after these items in note",
        "[organic_volume_growth, organic_revenue_growth, organic_operating_profit_growth] If a section says "
        "that its growth % means organic growth (e.g. 'growth % ... indicates organic growth'), an unlabelled "
        "growth rate in that section is the organic figure",
        "[volume_total, organic_volume_growth] Some companies report volume in 'mhl' (million hectolitres) "
        "and only beer volume in absolute terms; then take consolidated beer volume and say so in note. "
        "Organic volume growth may be given only for total consolidated volume; take it and name the basis",
    ],
}

METRICS = dict(CORE_METRICS)  # the active list; set_sector() adds the sector metrics

# Each section: words that appear in its heading, and line items expected on the page.
SECTIONS = {
    "income_statement": {
        "primary": True,  # a main statement, never a note page
        "titles": ["income statement", "statement of profit or loss", "profit and loss"],
        "keys": ["profit for the year", "profit/loss for the year"],
        "avoid_body": ["investing activities", "financing activities"],
        "labels": ["revenue", "ebitda", "ebit", "profit for the year", "operating profit",
                   "financial", "tax", "depreciation"],
    },
    "balance_sheet": {
        "primary": True,
        "titles": ["balance sheet", "statement of financial position"],
        "keys": ["total assets", "total equity and liabilities", "equity and liabilities"],
        "labels": ["total assets", "equity", "cash and cash equivalents", "lease liabilities",
                   "borrowings", "liabilities", "property, plant and equipment", "assets"],
    },
    "cash_flow": {
        "primary": True,
        "titles": ["cash flow statement", "statement of cash flows", "cash flows"],
        "keys": ["cash flow from operating activities", "cash flows from operating activities"],
        "labels": ["operating activities", "investing activities", "financing activities",
                   "property, plant and equipment", "intangible", "dividends"],
    },
    "key_figures": {
        "titles": ["key figures", "financial highlights", "five-year summary", "financial summary"],
        "labels": ["net interest-bearing debt", "free cash flow", "employees", "ebitda",
                   "fte", "return on", "equity ratio"],
    },
    "financial_items_note": {
        "titles": ["financial expenses", "financial items", "finance costs", "interest expenses",
                   "financial income and costs", "financial income and expenses", "financial costs"],
        "labels": ["interest", "lease liabilities", "borrowings", "expenses", "income"],
        "keys": ["interest on lease liabilities", "interest expenses", "interest expense",
                 "lease interest", "interest on bank loans", "interest on borrowings"],
        "avoid": ["pension", "defined benefit", "jubilee"],
    },
    "employees_note": {
        "titles": ["average number of employees", "full-time equivalent", "staff costs",
                   "employee costs"],
        "labels": ["wages", "salaries", "pension", "employees", "fte"],
    },
}
CORE_SECTIONS = dict(SECTIONS)

# Extra pages to find per sector. "notes_only": search only after the income statement.
SECTOR_SECTIONS = {
    "beverages": {
        "volumes_and_growth": {
            "titles": ["organic growth", "organic development", "key figures", "group performance",
                       "volumes",
                       # Heineken p.7: "Performance highlights" with "Consolidated beer volume 234.0mhl"
                       "performance highlights"],
            "keys": ["million hl", "m hl", "acquisitions, net", "organic", "total volumes",
                     "pro rata volumes",
                     # Heineken: "234.0mhl", "in millions of hectolitres", "Total consolidated volume"
                     "mhl", "millions of hectolitres", "total consolidated volume"],
            # regional pages repeat the same words; the group page is the one we want
            "avoid": ["western europe", "asia", "central & eastern europe", "eastern europe and india",
                      ],
            "labels": ["volume", "revenue", "operating profit", "organic", "acquisition",
                       "currency", "hl", "beer volume", "consolidated volume"],
            # Heineken: volume (p.7), its organic growth (p.48) and the growth table (p.47)
            # sit on different pages
            "pages": 5,
        },
        "special_items_note": {
            "notes_only": True,
            "titles": ["special items"],
            "keys": ["restructuring", "integration costs", "impairment"],
            "labels": ["restructuring", "impairment", "acquisition", "integration", "special items"],
        },
    },
}


# Changes to a core section for one sector (merged into a copy of the core section).
SECTOR_SECTION_OVERRIDES = {
    "beverages": {
        # Carlsberg's five-year key figures page starts with a volume table
        "key_figures": {"keys": ["total volume", "five-year",
                                 # Heineken key figures: "Net debt position"
                                 "net debt"],
                        # Heineken p.8: lines only on the key figures page itself
                        "labels": ["net debt", "market capitalisation", "average number of employees"]},
        # US 10-K title (Monster), UK 'profit for the period' (A.G. Barr, 53-week year),
        # 'profit after tax' (Coca-Cola HBC, CCEP); labels help when the title is read late
        "income_statement": {"titles": ["statements of income", "statement of income"],
                             "keys": ["profit for the period", "profit after tax"],
                             "labels": ["gross profit", "cost of sales", "profit before tax",
                                        "finance costs"],
                             # summary pages that repeat the income statement lines (CCEP p.310,
                             # A.G. Barr p.194): penalised only when the heading is at the top
                             "avoid": ["selected financial data", "non-gaap measures",
                                       "reconciliation of non-gaap"]},
    },
}

# A five-year summary names every statement (income statement, statement of financial
# position, cash flows...), so the table-of-contents penalty hits it. A real table of
# contents has few figures with thousands separators; a five-year summary has many.
# Only switched on per sector, so the tuned shipping page choice stays as it was.
TOC_EXEMPT_DENSE = {"beverages": True}
DENSE_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
DENSE_MIN = 40
SETTINGS = {"toc_exempt_dense": False, "note_alone": False}  # set by set_sector()

# Words that mark the income statement of a parent company in a statement set. Shipping
# parents report revenue; a brewing group's parent is a holding company with no revenue
# line, so its income statement is recognised by the profit line instead.
SECTOR_STATEMENT_KEYS = {
    "beverages": ["revenue", "profit for the period", "profit for the year"],
}
STATEMENT_KEYS = ["revenue"]  # the active list; set_sector() changes it


def set_sector(sector: str) -> None:
    """Make the metrics and sections of one sector active (core + sector extras)."""
    METRICS.clear()
    METRICS.update(CORE_METRICS)
    METRICS.update(SECTOR_METRICS.get(sector, {}))
    SECTIONS.clear()
    SECTIONS.update(CORE_SECTIONS)
    SECTIONS.update(SECTOR_SECTIONS.get(sector, {}))
    for name, change in SECTOR_SECTION_OVERRIDES.get(sector, {}).items():
        merged = dict(CORE_SECTIONS[name])
        for key, words in change.items():
            merged[key] = (list(merged.get(key, [])) + words if isinstance(words, list) else words)
        SECTIONS[name] = merged
    SETTINGS["toc_exempt_dense"] = TOC_EXEMPT_DENSE.get(sector, False)
    SETTINGS["note_alone"] = NOTE_ALONE.get(sector, False)
    STATEMENT_KEYS[:] = SECTOR_STATEMENT_KEYS.get(sector, ["revenue"])


def output_path(company: str, year: int) -> Path:
    """Shipping keeps output/<company>_<year>.csv; other sectors use output/<sector>/."""
    sector = COMPANIES[company]["sector"]
    folder = OUTPUT_DIR if sector == DEFAULT_SECTOR else OUTPUT_DIR / sector
    return folder / f"{company.lower()}_{year}.csv"


PAGES_PER_SECTION = 2
MAX_CHARS_PER_PAGE = 6000
NUMBER_RE = re.compile(r"\(?-?\d[\d,.]*\)?")
# A note heading at the start of a line, e.g. "NOTE 3 – SEGMENT", "Note 2.1: Revenue",
# "NOTE 3 — continued". The dash/colon after the number keeps the "Note 2025 2024"
# column header of a main statement from matching.
NOTE_HEADING_RE = re.compile(r"(?im)^\s*note\s+\d{1,2}(\.\d{1,2})?\s*[–—:-]")
NOTE_PENALTY = 8.0
# CCEP writes the note heading alone on its line ("Note 18"), with no dash after it.
# Switched on per sector, so the tuned shipping page choice stays as it was.
NOTE_ALONE_RE = re.compile(r"(?im)^[ \t]*note[ \t]+\d{1,2}[ \t]*$")
NOTE_ALONE = {"beverages": True}


# ---------------------------------------------------------------- 1. read PDF
def read_pages(pdf_path: Path) -> list[str]:
    reader = PdfReader(pdf_path)
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # a single broken page should not stop the run
            pages.append("")
    return pages


# ---------------------------------------------------------- 2. locate pages
def is_parent_company_page(text: str) -> bool:
    top = text[:400].lower()
    if "parent company" in top or "cash pool" in text.lower():
        return True
    # Swiss groups (Coca-Cola HBC) put the parent's statutory accounts under this heading.
    # Only the first line counts: the page navigation bar repeats the words on every page.
    return first_line(text).lower().startswith("swiss statutory reporting")


def is_note_page(text: str) -> bool:
    """True if a note heading appears near the top of the page. Notes can repeat the
    line items of a main statement (e.g. a segment balance sheet), so they must not
    win the income statement / balance sheet / cash flow slots."""
    if NOTE_HEADING_RE.search(text[:300]):
        return True
    return SETTINGS["note_alone"] and bool(NOTE_ALONE_RE.search(text[:300]))


def squash(text: str) -> str:
    """Lowercase and drop all whitespace, so 'In come statement' still matches."""
    return re.sub(r"\s+", "", text.lower())


def has_any(phrases: list[str], text: str) -> bool:
    flat = squash(text)
    return any(squash(p) in flat for p in phrases)


def score_page(text: str, section: dict) -> float:
    lower = text.lower()
    has_title = has_any(section["titles"], text)
    has_key = has_any(section.get("keys", []), text)
    if not (has_title or has_key):
        return 0.0
    score = 1.0
    if section.get("primary") and is_note_page(text):
        score -= NOTE_PENALTY
    if has_key:
        score += 3.0  # a line that only appears on the statement itself
    if section is SECTIONS.get("financial_items_note"):
        # several different interest lines = very likely the right note
        score += 2.0 * sum(1 for k in section["keys"] if has_any([k], text))
    # penalise only when the unwanted topic is the page heading (e.g. a pension note);
    # a passing mention inside the right note is fine
    score -= 4.0 * sum(1 for a in section.get("avoid", []) if has_any([a], text[:300]))
    # content that belongs to a different statement anywhere on the page
    score -= 6.0 * sum(1 for a in section.get("avoid_body", []) if has_any([a], text))
    if has_any(section["titles"], text[:300]):
        score += 3.0  # title near the top = probably the heading of this page
    score += sum(1 for label in section["labels"] if label in lower)
    score += min(len(NUMBER_RE.findall(text)) / 40, 3.0)  # statement pages are dense with numbers
    # Table of contents mentions many sections at once: push it down.
    sections_named = sum(
        has_any(s["titles"], text) for s in CORE_SECTIONS.values()
    )
    dense_table = (SETTINGS["toc_exempt_dense"]
                   and len(DENSE_NUMBER_RE.findall(text)) >= DENSE_MIN)
    if sections_named >= 4 and not dense_table:
        score -= 4.0
    return score


STATEMENT_SETS: list[int] = []  # filled by find_parent_start, shown in the dry run


def find_parent_start(pages: list[str]) -> int:
    """Danish reports put the consolidated statements first and the parent company
    statements later. A statement set = a page with an income statement title and
    a revenue line (or another STATEMENT_KEYS line), followed within 1-3 pages by a page headed 'balance sheet' or
    'statement of financial position'. The earliest set is the consolidated one;
    the last set more than 20 pages later marks the parent company section.
    Returns len(pages) if there is no second set."""
    inc = SECTIONS["income_statement"]["titles"]
    bs = SECTIONS["balance_sheet"]["titles"]

    def bs_follows(i: int) -> bool:
        return any(has_any(bs, pages[j][:300]) for j in range(i + 1, min(i + 4, len(pages))))

    sets = [i for i, text in enumerate(pages)
            if has_any(inc, text) and has_any(STATEMENT_KEYS, text) and bs_follows(i)]
    STATEMENT_SETS[:] = sets
    if not sets:
        return len(pages)
    # False matches can appear inside the consolidated notes, but the parent company
    # statements are always the last part of the financial statements: take the last set.
    later = [i for i in sets if i > sets[0] + 20]
    return later[-1] - 1 if later else len(pages)  # -1: the divider page before it


def locate_pages(pages: list[str]) -> tuple[dict[str, list[tuple[int, float]]], int]:
    """Return ({section: [(page index 0-based, score)]}, parent_start), best first."""
    parent_start = find_parent_start(pages)
    usable = [i for i in range(parent_start) if not is_parent_company_page(pages[i])]

    def rank(section: dict, n: int, anchor: int | None = None,
             notes_only: bool = False) -> list[tuple[int, float]]:
        scored = []
        for i in usable:
            # notes to the consolidated statements always come after the statements
            if notes_only and anchor is not None and i <= anchor:
                continue
            s = score_page(pages[i], section)
            if s <= 0:
                continue
            # balance sheet and cash flow sit a few pages after the income statement
            if not notes_only and anchor is not None and 0 < i - anchor <= 8:
                s += 6.0
            scored.append((s, i))
        scored.sort(key=lambda c: (-c[0], c[1]))
        return [(i, s) for s, i in scored[:n]]

    # The income statement is followed within 1-3 pages by the balance sheet.
    bs_titles = SECTIONS["balance_sheet"]["titles"]
    before_bs = {i for i in usable
                 if any(has_any(bs_titles, pages[j][:300])
                        for j in range(i + 1, min(i + 4, len(pages))))}

    def rank_income() -> list[tuple[int, float]]:
        scored = []
        for i in usable:
            s = score_page(pages[i], SECTIONS["income_statement"])
            if s <= 0:
                continue
            if i in before_bs:
                s += 4.0
            scored.append((s, i))
        scored.sort(key=lambda c: (-c[0], c[1]))
        return [(i, s) for s, i in scored[:PAGES_PER_SECTION]]

    found = {"income_statement": rank_income()}
    anchor = found["income_statement"][0][0] if found["income_statement"] else None
    found["balance_sheet"] = rank(SECTIONS["balance_sheet"], PAGES_PER_SECTION, anchor)
    found["cash_flow"] = rank(SECTIONS["cash_flow"], PAGES_PER_SECTION, anchor)
    found["key_figures"] = rank(SECTIONS["key_figures"], PAGES_PER_SECTION)
    found["financial_items_note"] = rank(SECTIONS["financial_items_note"], 3, anchor, notes_only=True)
    found["employees_note"] = rank(SECTIONS["employees_note"], PAGES_PER_SECTION, anchor, notes_only=True)
    for name, section in SECTIONS.items():  # sector sections
        if name not in CORE_SECTIONS:
            found[name] = rank(section, section.get("pages", PAGES_PER_SECTION), anchor,
                               notes_only=section.get("notes_only", False))
    return found, parent_start


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80]
    return "(empty page)"


# ------------------------------------------------------------ 3. call Claude
def load_rules(sector: str = DEFAULT_SECTOR) -> str:
    try:
        rules = pd.read_excel(GROUND_TRUTH, sheet_name="extraction_rules")
    except FileNotFoundError:
        print(f"Warning: {GROUND_TRUTH} not found, running without rules.")
        return sector_rules(sector) or "(no rules)"
    except ValueError:
        sheets = pd.ExcelFile(GROUND_TRUTH).sheet_names
        print(f"Warning: no sheet named 'extraction_rules' in {GROUND_TRUTH}. "
              f"Sheets found: {sheets}. Running without rules.")
        return sector_rules(sector) or "(no rules)"
    rules = rules.dropna(subset=["rule"])
    # general rules and rules for the active metrics only
    rules = rules[rules["metric"].astype(str).str.strip().isin(["general", *METRICS])]
    lines = [f"- [{r.metric}] {r.rule}" for r in rules.itertuples()]
    extra = sector_rules(sector)
    return "\n".join(lines + ([extra] if extra else []))


def sector_rules(sector: str) -> str:
    return "\n".join(f"- {r}" for r in SECTOR_RULES.get(sector, []))


def record_tool() -> dict:
    """The tool schema, built for the active metric list."""
    return {
    "name": "record_metrics",
    "description": "Record the extracted financial metrics.",
    "input_schema": {
        "type": "object",
        "properties": {
            "metrics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": list(METRICS)},
                        "value": {"type": ["number", "null"]},
                        "unit": {"type": "string", "enum": ["million", "billion", "thousand", "count",
                                                            "percent", "million hl"]},
                        "currency": {"type": ["string", "null"]},
                        "page": {"type": ["integer", "null"]},
                        "note": {"type": "string"},
                    },
                    "required": ["metric", "value", "unit", "currency", "page", "note"],
                },
            }
        },
        "required": ["metrics"],
    },
    }


def year_rule(year: int) -> str:
    if year == YEAR:
        return f"Take the {YEAR} column, not the prior year."
    return (f"Take the {year} column: the prior-year comparative figures printed next to {YEAR}. "
            f"Do not take the {YEAR} column. Where this report restates {year} figures, use them as "
            f"shown here. The extraction rules say 'current-year column'; in this run that means "
            f"the {year} column.")


def build_prompt(company: str, currency: str, pages: list[str], page_ids: list[int], rules: str,
                 year: int = YEAR) -> str:
    metric_list = "\n".join(f"- {k}: {v}" for k, v in METRICS.items())
    page_text = "\n\n".join(
        f"=== PDF page {i + 1} ===\n{pages[i][:MAX_CHARS_PER_PAGE]}" for i in page_ids
    )
    return f"""You are a credit analyst extracting figures from the {YEAR} annual report of {company}
(reporting currency {currency}). Below are selected pages of the report.

Extract these {len(METRICS)} metrics for financial year {year}:
{metric_list}

Extraction rules (learned from manual extraction, follow them strictly):
{rules}

Output rules:
- Use only the pages below. {year_rule(year)} {COMPANIES[company].get("fy_note", "") if year == YEAR else ""}
- value: the number exactly as reported (keep the sign; costs and capex shown in brackets are negative).
  Do not convert units. If a metric is the sum of several lines, add them and list the parts in note.
- unit: the unit stated on that page (million, billion, thousand); "count" for employees;
  "percent" for growth rates; "million hl" for volumes.
- currency: e.g. DKK or USD; null for employees, volumes and percentages.
- page: the number from the "=== PDF page N ===" marker where the figure is found.
- note: short; the exact line label used, how it was calculated, and any definition differences.
  If the metric is not on these pages, set value and page to null and write "not found".
- Return all {len(METRICS)} metrics by calling the record_metrics tool exactly once. Do not answer in plain text.

{page_text}"""


def parse_metrics(response) -> list[dict]:
    """Take the metrics from the record_metrics tool call; if the model answered
    in plain text instead, fall back to the first JSON object in that text."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_metrics":
            return block.input["metrics"]
    text = "".join(getattr(b, "text", "") for b in response.content)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Model returned no metrics. Response was:\n{text[:1000]}")
    return json.loads(text[start:end + 1])["metrics"]


def extract(company: str, pages: list[str], page_ids: list[int], year: int) -> tuple[pd.DataFrame, object]:
    import anthropic  # imported here so --dry-run works without an API key

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment / .env
    rules = load_rules(COMPANIES[company]["sector"])
    prompt = build_prompt(company, COMPANIES[company]["currency"], pages, page_ids, rules, year)
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        tools=[record_tool()],
        messages=[{"role": "user", "content": prompt}],
    )
    rows = parse_metrics(response)
    df = pd.DataFrame(rows)
    df.insert(0, "year", year)
    df.insert(0, "company", company)
    # keep the same metric order as the ground truth sheet
    df["metric"] = pd.Categorical(df["metric"], categories=list(METRICS), ordered=True)
    df = df.sort_values("metric")[["company", "year", "metric", "value", "unit", "currency", "page", "note"]]
    return df, response.usage


# ------------------------------------------------------------------- 4. run
def run(company: str, dry_run: bool, year: int = YEAR) -> None:
    set_sector(COMPANIES[company]["sector"])
    pdf_path = REPORTS_DIR / COMPANIES[company]["file"]
    print(f"\n### {company} {year} ({COMPANIES[company]['sector']}): reading {pdf_path}")
    pages = read_pages(pdf_path)
    located, parent_start = locate_pages(pages)

    print(f"Statement sets found at pages: {[i + 1 for i in STATEMENT_SETS]}")
    if parent_start < len(pages):
        print(f"Parent company section detected from p.{parent_start + 1}: skipped")
    print(f"{len(pages)} pages. Selected pages (PDF page numbers):")
    for section, hits in located.items():
        shown = ", ".join(f"p.{i + 1} ({s:.1f}) [{first_line(pages[i])[:40]}]" for i, s in hits)
        print(f"  {section:<22} {shown or 'none found'}")

    page_ids = sorted({i for hits in located.values() for i, _ in hits})
    print(f"Sending {len(page_ids)} pages: {[i + 1 for i in page_ids]}")
    if dry_run:
        return

    df, usage = extract(company, pages, page_ids, year)
    out = output_path(company, year)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(df[["metric", "value", "unit", "page"]].to_string(index=False))
    print(f"Saved {out}  (tokens in {usage.input_tokens:,}, out {usage.output_tokens:,})")


def show_page(company: str, page_no: int) -> None:
    """Debug: print the start of one page and its score for every section."""
    set_sector(COMPANIES[company]["sector"])
    pages = read_pages(REPORTS_DIR / COMPANIES[company]["file"])
    text = pages[page_no - 1]
    print(f"--- p.{page_no}, first 500 characters as pypdf reads them ---")
    print(text[:500])
    print("--- scores (matched words) ---")
    lower = text.lower()
    for name, section in SECTIONS.items():
        words = [w for w in section["titles"] + section.get("keys", []) + section.get("avoid", [])
                 if w in lower]
        print(f"  {name:<22} {score_page(text, section):5.1f}  {words}")
    print(f"  parent company page?   {is_parent_company_page(text)}")
    print(f"  note page?             {is_note_page(text)}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract key figures from annual reports.")
    parser.add_argument("company", choices=list(COMPANIES) + ["all"])
    parser.add_argument("--dry-run", action="store_true", help="only show selected pages, no API call")
    parser.add_argument("--show-page", type=int, help="debug: show text and scores of one PDF page")
    parser.add_argument("--year", type=int, default=YEAR, choices=[YEAR, YEAR - 1],
                        help=f"financial year to extract (default {YEAR}; {YEAR - 1} = prior-year column)")
    sectors = sorted({c["sector"] for c in COMPANIES.values()})
    parser.add_argument("--sector", choices=sectors, default=DEFAULT_SECTOR,
                        help=f"with 'all': which sector to run (default {DEFAULT_SECTOR})")
    args = parser.parse_args()
    if args.show_page:
        show_page(args.company, args.show_page)
        return
    if args.company == "all":
        targets = [c for c, info in COMPANIES.items() if info["sector"] == args.sector]
    else:
        targets = [args.company]
    for company in targets:
        run(company, args.dry_run, args.year)


if __name__ == "__main__":
    main()