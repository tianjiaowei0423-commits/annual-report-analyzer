"""
Annual report analyzer - step 3: short credit summaries.

Sends the ratio table (from analyze.py) to Claude and asks for a short credit view per
company plus a peer comparison, in the order: profitability -> debt -> cash.
Every number in the text is then checked against the table, so nothing is invented.

Usage:
  python summarize.py --dry-run   # show the facts and the prompt, no API call (free)
  python summarize.py             # write the summaries (one API call, a few cents)

Output:
  output/credit_summaries.md
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from analyze import DEFINITIONS, compute_ratios, load_metrics

MODEL = "claude-sonnet-5-5"
YEAR = 2025
OUTPUT_DIR = Path("output")

# Facts given to the model: (column in the ratio/metric table, label, kind)
FACTS = [
    ("revenue", "Revenue", "money"),
    ("ebitda", "EBITDA", "money"),
    ("ebit", "EBIT", "money"),
    ("net_profit", "Net profit", "money"),
    ("net_interest_bearing_debt", "Net interest-bearing debt as reported by the company", "money"),
    ("nibd_common_m", "Net debt, common definition (interest-bearing debt incl. leases - cash)", "money"),
    ("operating_cash_flow", "Operating cash flow", "money"),
    ("capex", "Capex", "money"),
    ("fcf_common_m", "FCF, common definition (OCF - capex)", "money"),
    ("fcf_reported_m", "FCF as reported by the company", "money"),
    ("ebitda_margin", "EBITDA margin", "pct"),
    ("ebit_margin", "EBIT margin", "pct"),
    ("nibd_to_ebitda", "NIBD / EBITDA, as reported", "x"),
    ("nibd_common_to_ebitda", "Net debt / EBITDA, common definition", "x"),
    ("interest_cover", "Interest cover (EBIT / interest)", "x"),
    ("equity_ratio", "Equity ratio", "pct"),
    ("cash_conversion", "Cash conversion (OCF / EBITDA)", "pct"),
    ("capex_intensity", "Capex intensity (capex / revenue)", "pct"),
    ("fcf_margin", "FCF margin, common definition", "pct"),
]

SUMMARY_TOOL = {
    "name": "record_summaries",
    "description": "Record the credit summaries.",
    "input_schema": {
        "type": "object",
        "properties": {
            "companies": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "company": {"type": "string"},
                        "headline": {"type": "string", "description": "One sentence: the overall credit picture."},
                        "profitability": {"type": "string", "description": "1-2 sentences."},
                        "debt": {"type": "string", "description": "1-2 sentences: leverage, interest cover, equity ratio."},
                        "cash": {"type": "string", "description": "1-2 sentences: cash conversion, capex, FCF."},
                        "watch": {"type": "string", "description": "One sentence: the main point to watch."},
                    },
                    "required": ["company", "headline", "profitability", "debt", "cash", "watch"],
                },
            },
            "peer_summary": {"type": "string", "description": "3-4 sentences comparing the companies."},
        },
        "required": ["companies", "peer_summary"],
    },
}


# ------------------------------------------------------------- 1. the facts
def fmt(value: float, kind: str, currency: str) -> str:
    if pd.isna(value):
        return "n/a"
    if kind == "pct":
        return f"{value:.1%}"
    if kind == "x":
        return f"{value:.1f}x"
    whole = abs(value - round(value)) < 1e-9 or abs(value) >= 10_000
    return f"{currency} {value:,.0f}m" if whole else f"{currency} {value:,.1f}m"


def build_facts() -> tuple[dict, pd.DataFrame]:
    metrics = load_metrics(use_truth=False)
    ratios = compute_ratios(metrics)
    # employees is in both tables; keep one copy so the join needs no suffix
    table = metrics.drop(columns=["currency"]).join(ratios.drop(columns=["employees"]))
    facts = {}
    for company, row in table.iterrows():
        cur = row["currency"]
        facts[company] = {label: fmt(row[col], kind, cur) for col, label, kind in FACTS}
        if row["net_interest_bearing_debt"] < 0:
            facts[company]["NIBD / EBITDA, as reported"] = "net cash on the company's own definition (NIBD is negative)"
    return facts, table


# Rankings are worked out here, in Python, so the model never has to compare numbers itself.
# (column, label, True if a higher value is better)
RANKED = [
    ("ebitda_margin", "EBITDA margin", True),
    ("ebit_margin", "EBIT margin", True),
    ("nibd_common_to_ebitda", "Net debt / EBITDA, common definition", False),
    ("interest_cover", "Interest cover", True),
    ("equity_ratio", "Equity ratio", True),
    ("cash_conversion", "Cash conversion", True),
    ("fcf_margin", "FCF margin, common definition", True),
    ("capex_intensity", "Capex intensity", None),  # neither better nor worse: highest first
]


def build_rankings(table: pd.DataFrame) -> list[str]:
    """One line per ratio, from best to worst, e.g. 'Interest cover, best to worst: TORM 5.2x, ...'."""
    kinds = {col: kind for col, _, kind in FACTS}
    lines = []
    for col, label, higher_better in RANKED:
        col_values = table[col].dropna().sort_values(ascending=higher_better is False)
        order = "highest to lowest" if higher_better is None else "best to worst"
        items = ", ".join(f"{c} {fmt(v, kinds[col], '')}" for c, v in col_values.items())
        lines.append(f"- {label}, {order}: {items}")
    return lines


# --------------------------------------------------------- 2. ask Claude
def build_prompt(facts: dict, rankings: list[str]) -> str:
    defs = "\n".join(f"- {k}: {f}. {h}" for k, (f, h, _) in DEFINITIONS.items())
    return f"""You are a credit analyst. Below are figures for {YEAR}, extracted from the annual reports of
{len(facts)} Danish-listed shipping and logistics companies, with ratios calculated from them.
Money is in millions of each company's own reporting currency.

FACTS (JSON):
{json.dumps(facts, indent=2)}

RANKINGS (worked out from the facts):
{chr(10).join(rankings)}

RATIO DEFINITIONS:
{defs}

Write a short credit view of each company, in this order: profitability, then debt
(leverage, interest cover, equity ratio), then cash (cash conversion, capex, free cash flow).
Then compare the companies in a short peer summary.

Rules:
- Use only the facts above. Every number you write must appear in the facts, written the same
  way (e.g. 12.1%, 4.1x, USD 251.9m). Do not calculate new numbers, do not round differently,
  and do not add anything from memory about these companies.
- Compare ratios, not absolute amounts: the companies report in different currencies.
- Every comparison word (highest, lowest, best, weakest, above, below another company) must
  follow the RANKINGS. Do not call a company "strongest" or "weakest" overall; name the
  ratios on which it leads or lags instead.
- Describe leverage by its level on the common definition (low, moderate, high). A company
  that reports net cash but has low leverage on the common definition is still low-leverage;
  do not call the two views opposite.
- Write each sentence once, as a finished statement. Do not correct yourself in the text;
  check against the RANKINGS before writing.
- Describe each value as what it is. Interest cover below 1.0x means EBIT does not cover
  interest at all (not "barely covers"). Net cash means negative NIBD.
- Compare like with like: a ratio with a ratio, an amount with an amount. Write "capex
  (USD 630.1m) exceeds operating cash flow (USD 378.2m)", not "capex intensity exceeds
  operating cash flow".
- When "FCF as reported" differs a lot from the common definition, say so briefly: companies
  define free cash flow differently.
- Base the leverage view on "Net debt / EBITDA, common definition". When the company's own
  NIBD differs a lot from the common definition (for example net cash on its own definition
  but net debt on the common one), say so and name both: companies define net debt differently.
- In the peer summary, note that the business models differ (ferries and logistics, container
  shipping, dry bulk and tankers, product tankers), so part of the gaps comes from the business,
  not only from how well each company is run.
- No credit ratings, no buy/sell views. Plain, direct analyst English; avoid filler words such as
  "robust", "solid foundation", "navigate", "landscape".
- Return everything by calling the record_summaries tool exactly once. Do not answer in plain text."""


def call_claude(prompt: str) -> tuple[dict, object]:
    import anthropic  # imported here so --dry-run works without an API key

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        tools=[SUMMARY_TOOL],  # no forced tool_choice: this model does not support it
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_summaries":
            return block.input, response.usage
    # fallback, as in extract.py: the model answered in text, take the JSON object from it
    text = "".join(getattr(b, "text", "") for b in response.content)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"Model returned no summaries. Response was:\n{text[:1000]}")
    return json.loads(text[start:end + 1]), response.usage


# ------------------------------------------------- 3. check every number
NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def allowed_numbers(table: pd.DataFrame) -> list[float]:
    """Every number the model may use, in the forms it may appear (12.1% -> 12.1, 4.1x -> 4.1)."""
    out = [float(YEAR), 1.0]  # 1.0x: the "does not cover interest" line named in the rules
    # thresholds named in the ratio definitions ("around 3x", "below about 2x") may be quoted
    for _, how, _ in DEFINITIONS.values():
        out += [float(n) for n in NUM_RE.findall(how)]
    pct_cols = {c for c, _, k in FACTS if k == "pct"}
    for col, _, _ in FACTS:
        for v in table[col].dropna():
            out.append(abs(v * 100) if col in pct_cols else abs(v))
    return out


def check_numbers(text: str, allowed: list[float]) -> list[str]:
    """Numbers in the text that do not match any fact (rounding to the digits written is OK)."""
    bad = []
    for m in NUM_RE.findall(text):
        s = m.replace(",", "").lstrip("-")
        try:
            x = float(s)
        except ValueError:
            continue
        decimals = len(s.split(".")[1]) if "." in s else 0
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        if not any(abs(x - a) <= tol for a in allowed):
            bad.append(m)
    return bad


# ------------------------------------------------------------- 4. output
def to_markdown(result: dict, facts: dict) -> str:
    lines = [f"# Credit summaries, {YEAR}", "",
             "Written by Claude from the ratio table in `output/ratios.xlsx`. The underlying figures "
             "were extracted from the annual reports and checked against a hand-made ground truth.", ""]
    for c in result["companies"]:
        lines += [f"## {c['company']}", "", f"**{c['headline']}**", "",
                  f"- **Profitability:** {c['profitability']}",
                  f"- **Debt:** {c['debt']}",
                  f"- **Cash:** {c['cash']}",
                  f"- **Watch:** {c['watch']}", ""]
    lines += ["## Peer comparison", "", result["peer_summary"], ""]
    return "\n".join(lines)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Write short credit summaries from the ratios.")
    parser.add_argument("--dry-run", action="store_true", help="show facts and prompt, no API call")
    args = parser.parse_args()

    facts, table = build_facts()
    prompt = build_prompt(facts, build_rankings(table))
    if args.dry_run:
        print(prompt)
        print(f"\n(about {len(prompt) // 4:,} input tokens; no API call made)")
        return

    result, usage = call_claude(prompt)
    md = to_markdown(result, facts)

    allowed = allowed_numbers(table)
    body = md.split("\n", 4)[-1]  # skip the fixed header text
    bad = check_numbers(body, allowed)

    if bad:
        check = (f"Number check: {len(bad)} number(s) not found in the ratio table, "
                 f"check by hand: {', '.join(sorted(set(bad)))}")
    else:
        check = "Number check: every number in the text matches the ratio table."
    md += (f"\n---\n\n*{check.rstrip('.')}. Numbers are checked automatically; "
           f"the reasoning still needs a person to read it.*\n")

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "credit_summaries.md"
    out.write_text(md, encoding="utf-8")
    print(md)
    print(f"Saved {out}  (tokens in {usage.input_tokens:,}, out {usage.output_tokens:,})")


if __name__ == "__main__":
    main()