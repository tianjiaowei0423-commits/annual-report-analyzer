"""
Annual report analyzer - step 2: ratios and peer comparison.

Reads the extracted metrics (output/<company>_2025.csv), turns them into ratios,
and puts all companies side by side.

Usage:
  python analyze.py            # every company with an output file
  python analyze.py --truth    # use the hand-made ground truth instead (for checking)

Output:
  output/ratios.xlsx  sheet "ratios"      one row per company
                      sheet "definitions" how each ratio is calculated and how to read it
"""

import argparse
from pathlib import Path

import pandas as pd

OUTPUT_DIR = Path("output")
GROUND_TRUTH = Path("data/ground_truth.xlsx")
YEAR = 2025
TO_MILLION = {"million": 1.0, "billion": 1000.0, "thousand": 0.001}

# name: (formula as text, how to read it, format)
DEFINITIONS = {
    "ebitda_margin": ("EBITDA / revenue", "Operating earnings power before depreciation. Higher = more profitable.", "pct"),
    "ebit_margin": ("EBIT / revenue", "Profitability after depreciation of ships, ferries and equipment.", "pct"),
    "nibd_to_ebitda": ("Net interest-bearing debt as reported by the company / EBITDA",
                       "Leverage: years of EBITDA needed to repay net debt. Below 0 = net cash. "
                       "Around 3x or more is usually seen as high.", "x"),
    "nibd_common_to_ebitda": ("(Interest-bearing debt incl. leases - cash) / EBITDA",
                              "Leverage on one common definition for all companies. Company-reported "
                              "NIBD can deduct more than cash (e.g. securities, receivables), "
                              "so both are shown.", "x"),
    "interest_cover": ("EBIT / interest expense",
                       "How many times operating profit covers interest. Below about 2x is a warning sign.", "x"),
    "equity_ratio": ("Total equity / total assets", "Share of assets financed by owners. Higher = more buffer.", "pct"),
    "cash_conversion": ("Operating cash flow / EBITDA",
                        "How much of EBITDA turns into operating cash (after tax, interest and working capital).", "pct"),
    "capex_intensity": ("Capex / revenue", "How heavy the reinvestment in the fleet and assets is.", "pct"),
    "fcf_margin": ("(Operating cash flow - capex) / revenue",
                   "Free cash flow on one common definition for all companies. "
                   "Company-reported FCF is shown separately, because each company defines it differently.", "pct"),
}


def load_metrics(use_truth: bool) -> pd.DataFrame:
    """One row per company, one column per metric, money in millions of the reporting currency."""
    if use_truth:
        df = pd.read_excel(GROUND_TRUTH, sheet_name="ground_truth")
    else:
        files = sorted(OUTPUT_DIR.glob(f"*_{YEAR}.csv"))
        if not files:
            raise SystemExit(f"No output/*_{YEAR}.csv files. Run extract.py first.")
        df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)

    df["company"] = df["company"].astype(str).str.strip()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    factor = df["unit"].map(TO_MILLION).fillna(1.0)  # "count" (employees) stays as it is
    df["value_m"] = df["value"] * factor

    wide = df.pivot_table(index="company", columns="metric", values="value_m", aggfunc="first")
    currency = (df.dropna(subset=["currency"]).groupby("company")["currency"].first())
    wide.insert(0, "currency", currency)
    return wide


def safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    b = b.where(b != 0)
    return a / b


def compute_ratios(m: pd.DataFrame) -> pd.DataFrame:
    capex = m["capex"].abs()  # capex is stored as a negative cash flow
    r = pd.DataFrame(index=m.index)
    r["currency"] = m["currency"]
    r["revenue_m"] = m["revenue"]
    r["ebitda_margin"] = safe_div(m["ebitda"], m["revenue"])
    r["ebit_margin"] = safe_div(m["ebit"], m["revenue"])
    nibd_common = m["total_borrowings"] - m["cash"]
    r["nibd_to_ebitda"] = safe_div(m["net_interest_bearing_debt"], m["ebitda"])
    r["nibd_common_to_ebitda"] = safe_div(nibd_common, m["ebitda"])
    r["interest_cover"] = safe_div(m["ebit"], m["interest_expense"])
    r["equity_ratio"] = safe_div(m["total_equity"], m["total_assets"])
    r["cash_conversion"] = safe_div(m["operating_cash_flow"], m["ebitda"])
    r["capex_intensity"] = safe_div(capex, m["revenue"])
    r["fcf_margin"] = safe_div(m["operating_cash_flow"] - capex, m["revenue"])
    r["fcf_reported_m"] = m["free_cash_flow"]
    r["fcf_common_m"] = m["operating_cash_flow"] - capex
    r["nibd_reported_m"] = m["net_interest_bearing_debt"]
    r["nibd_common_m"] = nibd_common
    r["employees"] = m["employees"]
    return r


def show(r: pd.DataFrame) -> pd.DataFrame:
    """Readable version for the terminal: percentages and multiples as text."""
    out = pd.DataFrame(index=r.index)
    out["cur"] = r["currency"]
    out["revenue (m)"] = r["revenue_m"].map("{:,.0f}".format)
    for name, (_, _, kind) in DEFINITIONS.items():
        col = r[name]
        if kind == "pct":
            out[name] = col.map(lambda v: "n/a" if pd.isna(v) else f"{v:.1%}")
        elif name in ("nibd_to_ebitda", "nibd_common_to_ebitda"):
            out[name] = col.map(lambda v: "n/a" if pd.isna(v) else ("net cash" if v < 0 else f"{v:.1f}x"))
        else:
            out[name] = col.map(lambda v: "n/a" if pd.isna(v) else f"{v:.1f}x")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Ratios and peer comparison.")
    parser.add_argument("--truth", action="store_true", help="use the ground truth instead of the model output")
    args = parser.parse_args()

    metrics = load_metrics(args.truth)
    missing = metrics.isna().sum(axis=1)
    for company, n in missing[missing > 0].items():
        print(f"Warning: {company} has {n} missing metric(s); related ratios show n/a.")

    ratios = compute_ratios(metrics)
    print(f"\nSource: {'ground truth' if args.truth else 'model output'}  |  money in millions, own currency\n")
    print(show(ratios).T.to_string())

    print("\nFCF as reported vs one common definition (OCF - capex), millions:")
    print(ratios[["currency", "fcf_reported_m", "fcf_common_m"]].to_string())

    print("\nNet debt as reported vs one common definition (debt incl. leases - cash), millions:")
    print(ratios[["currency", "nibd_reported_m", "nibd_common_m"]].to_string())

    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "ratios.xlsx"
    defs = pd.DataFrame([{"ratio": k, "formula": f, "how to read": h} for k, (f, h, _) in DEFINITIONS.items()])
    with pd.ExcelWriter(out) as w:
        ratios.round(4).to_excel(w, sheet_name="ratios")
        defs.to_excel(w, sheet_name="definitions", index=False)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()
