"""
Annual report analyzer - version 1: extract key financial figures.

Steps:
  1. Read every page of the annual report PDF (pypdf).
  2. Locate the pages that matter (income statement, balance sheet,
     cash flow statement, key figures, financial items note, staff note)
     by keyword scoring, skipping parent company pages.
  3. Send only those pages, plus the rules in data/ground_truth.xlsx
     (sheet "extraction_rules"), to Claude.
  4. Save the 14 metrics, with unit, PDF page and note, to output/<company>_2025.csv
     in the same layout as the ground truth sheet.

Usage:
  python extract.py DFDS --dry-run   # only show which pages would be sent (free)
  python extract.py DFDS             # run the extraction (calls the API)
  python extract.py all              # all three companies
  python extract.py DFDS --show-page 123   # debug one page (free)
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pypdf import PdfReader

MODEL = "claude-sonnet-5-5"
YEAR = 2025
REPORTS_DIR = Path("data/reports")
GROUND_TRUTH = Path("data/ground_truth.xlsx")
OUTPUT_DIR = Path("output")

COMPANIES = {
    "DFDS": {"file": "dfds_2025.pdf", "currency": "DKK"},
    "Maersk": {"file": "maersk_2025.pdf", "currency": "USD"},
    "Norden": {"file": "norden_2025.pdf", "currency": "USD"},
}

METRICS = {
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

# Each section: words that appear in its heading, and line items expected on the page.
SECTIONS = {
    "income_statement": {
        "titles": ["income statement", "statement of profit or loss", "profit and loss"],
        "keys": ["profit for the year", "profit/loss for the year"],
        "labels": ["revenue", "ebitda", "ebit", "profit for the year", "operating profit",
                   "financial", "tax", "depreciation"],
    },
    "balance_sheet": {
        "titles": ["balance sheet", "statement of financial position"],
        "keys": ["total assets", "total equity and liabilities", "equity and liabilities"],
        "labels": ["total assets", "equity", "cash and cash equivalents", "lease liabilities",
                   "borrowings", "liabilities", "property, plant and equipment", "assets"],
    },
    "cash_flow": {
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
PAGES_PER_SECTION = 2
MAX_CHARS_PER_PAGE = 6000
NUMBER_RE = re.compile(r"\(?-?\d[\d,.]*\)?")


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
    return "parent company" in top or "cash pool" in text.lower()


def score_page(text: str, section: dict) -> float:
    lower = text.lower()
    has_title = any(t in lower for t in section["titles"])
    has_key = any(k in lower for k in section.get("keys", []))
    if not (has_title or has_key):
        return 0.0
    score = 1.0
    if has_key:
        score += 3.0  # a line that only appears on the statement itself
    if section is SECTIONS.get("financial_items_note"):
        # several different interest lines = very likely the right note
        score += 2.0 * sum(1 for k in section["keys"] if k in lower)
    # penalise only when the unwanted topic is the page heading (e.g. a pension note);
    # a passing mention inside the right note is fine
    score -= 4.0 * sum(1 for a in section.get("avoid", []) if a in lower[:300])
    if any(t in lower[:300] for t in section["titles"]):
        score += 3.0  # title near the top = probably the heading of this page
    score += sum(1 for label in section["labels"] if label in lower)
    score += min(len(NUMBER_RE.findall(text)) / 40, 3.0)  # statement pages are dense with numbers
    # Table of contents mentions many sections at once: push it down.
    sections_named = sum(
        any(t in lower for t in s["titles"]) for s in SECTIONS.values()
    )
    if sections_named >= 4:
        score -= 4.0
    return score


def find_parent_start(pages: list[str]) -> int:
    """Danish reports put the consolidated statements first and the parent company
    statements later. If a second full income statement appears more than 20 pages
    after the best one, treat everything from there on as parent company pages.
    Returns the first parent company page index (len(pages) if none found)."""
    section = SECTIONS["income_statement"]
    candidates = []
    for i, text in enumerate(pages):
        lower = text.lower()
        if any(t in lower[:300] for t in section["titles"]):
            candidates.append((score_page(text, section), i))
    if not candidates:
        return len(pages)
    candidates.sort(key=lambda c: (-c[0], c[1]))  # best score, then earliest page
    best = candidates[0][1]

    # A real second set of statements is a full set: a cash flow statement
    # follows within a few pages. A note that merely mentions "income statement"
    # in its heading does not pass this test.
    cf_titles = SECTIONS["cash_flow"]["titles"]

    def starts_full_set(i: int) -> bool:
        return any(
            any(t in pages[j].lower()[:300] for t in cf_titles)
            for j in range(i + 1, min(i + 9, len(pages)))
        )

    later = [i for _, i in candidates if i > best + 20 and starts_full_set(i)]
    return min(later) - 1 if later else len(pages)  # -1: the divider page before it


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

    found = {"income_statement": rank(SECTIONS["income_statement"], PAGES_PER_SECTION)}
    anchor = found["income_statement"][0][0] if found["income_statement"] else None
    found["balance_sheet"] = rank(SECTIONS["balance_sheet"], PAGES_PER_SECTION, anchor)
    found["cash_flow"] = rank(SECTIONS["cash_flow"], PAGES_PER_SECTION, anchor)
    found["key_figures"] = rank(SECTIONS["key_figures"], PAGES_PER_SECTION)
    found["financial_items_note"] = rank(SECTIONS["financial_items_note"], 3, anchor, notes_only=True)
    found["employees_note"] = rank(SECTIONS["employees_note"], PAGES_PER_SECTION, anchor, notes_only=True)
    return found, parent_start


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:80]
    return "(empty page)"


# ------------------------------------------------------------ 3. call Claude
def load_rules() -> str:
    try:
        rules = pd.read_excel(GROUND_TRUTH, sheet_name="extraction_rules")
    except FileNotFoundError:
        print(f"Warning: {GROUND_TRUTH} not found, running without rules.")
        return "(no rules)"
    except ValueError:
        sheets = pd.ExcelFile(GROUND_TRUTH).sheet_names
        print(f"Warning: no sheet named 'extraction_rules' in {GROUND_TRUTH}. "
              f"Sheets found: {sheets}. Running without rules.")
        return "(no rules)"
    rules = rules.dropna(subset=["rule"])
    return "\n".join(f"- [{r.metric}] {r.rule}" for r in rules.itertuples())


RECORD_TOOL = {
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
                        "unit": {"type": "string", "enum": ["million", "billion", "thousand", "count"]},
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


def build_prompt(company: str, currency: str, pages: list[str], page_ids: list[int], rules: str) -> str:
    metric_list = "\n".join(f"- {k}: {v}" for k, v in METRICS.items())
    page_text = "\n\n".join(
        f"=== PDF page {i + 1} ===\n{pages[i][:MAX_CHARS_PER_PAGE]}" for i in page_ids
    )
    return f"""You are a credit analyst extracting figures from the {YEAR} annual report of {company}
(reporting currency {currency}). Below are selected pages of the report.

Extract these 14 metrics for financial year {YEAR}:
{metric_list}

Extraction rules (learned from manual extraction, follow them strictly):
{rules}

Output rules:
- Use only the pages below. Take the {YEAR} column, not the prior year.
- value: the number exactly as reported (keep the sign; costs and capex shown in brackets are negative).
  Do not convert units. If a metric is the sum of several lines, add them and list the parts in note.
- unit: the unit stated on that page (million, billion, thousand); "count" for employees.
- currency: e.g. DKK or USD; null for employees.
- page: the number from the "=== PDF page N ===" marker where the figure is found.
- note: short; the exact line label used, how it was calculated, and any definition differences.
  If the metric is not on these pages, set value and page to null and write "not found".
- Return all 14 metrics by calling the record_metrics tool exactly once. Do not answer in plain text.

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


def extract(company: str, pages: list[str], page_ids: list[int]) -> tuple[pd.DataFrame, object]:
    import anthropic  # imported here so --dry-run works without an API key

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment / .env
    prompt = build_prompt(company, COMPANIES[company]["currency"], pages, page_ids, load_rules())
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        tools=[RECORD_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )
    rows = parse_metrics(response)
    df = pd.DataFrame(rows)
    df.insert(0, "year", YEAR)
    df.insert(0, "company", company)
    # keep the same metric order as the ground truth sheet
    df["metric"] = pd.Categorical(df["metric"], categories=list(METRICS), ordered=True)
    df = df.sort_values("metric")[["company", "year", "metric", "value", "unit", "currency", "page", "note"]]
    return df, response.usage


# ------------------------------------------------------------------- 4. run
def run(company: str, dry_run: bool) -> None:
    pdf_path = REPORTS_DIR / COMPANIES[company]["file"]
    print(f"\n### {company}: reading {pdf_path}")
    pages = read_pages(pdf_path)
    located, parent_start = locate_pages(pages)

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

    df, usage = extract(company, pages, page_ids)
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / f"{company.lower()}_{YEAR}.csv"
    df.to_csv(out, index=False)
    print(df[["metric", "value", "unit", "page"]].to_string(index=False))
    print(f"Saved {out}  (tokens in {usage.input_tokens:,}, out {usage.output_tokens:,})")


def show_page(company: str, page_no: int) -> None:
    """Debug: print the start of one page and its score for every section."""
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


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract key figures from annual reports.")
    parser.add_argument("company", choices=list(COMPANIES) + ["all"])
    parser.add_argument("--dry-run", action="store_true", help="only show selected pages, no API call")
    parser.add_argument("--show-page", type=int, help="debug: show text and scores of one PDF page")
    args = parser.parse_args()
    if args.show_page:
        show_page(args.company, args.show_page)
        return
    targets = list(COMPANIES) if args.company == "all" else [args.company]
    for company in targets:
        run(company, args.dry_run)


if __name__ == "__main__":
    main()
