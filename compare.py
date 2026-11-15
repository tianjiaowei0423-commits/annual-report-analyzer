"""
Compare the model's extraction (output/<company>_2025.csv) with the
hand-made ground truth (data/ground_truth.xlsx, sheet "ground_truth").

Usage:
  python compare.py            # every company that has an output file
  python compare.py Norden
"""

import sys
from pathlib import Path

import pandas as pd

GROUND_TRUTH = Path("data/ground_truth.xlsx")
OUTPUT_DIR = Path("output")
TOLERANCE = 1.0  # rounding difference allowed (extraction rule: ±1)


def compare(company: str, truth: pd.DataFrame) -> pd.DataFrame:
    pred = pd.read_csv(OUTPUT_DIR / f"{company.lower()}_2025.csv")
    t = truth[truth["company"] == company][["metric", "value", "page"]]
    merged = t.merge(pred[["metric", "value", "page", "note"]], on="metric",
                     how="left", suffixes=("_truth", "_model"))
    v_t = pd.to_numeric(merged["value_truth"], errors="coerce")
    v_m = pd.to_numeric(merged["value_model"], errors="coerce")
    merged["value_ok"] = (v_t - v_m).abs() <= TOLERANCE
    merged["page_ok"] = (pd.to_numeric(merged["page_truth"], errors="coerce")
                         == pd.to_numeric(merged["page_model"], errors="coerce"))
    merged.insert(0, "company", company)
    return merged


def main() -> None:
    truth = pd.read_excel(GROUND_TRUTH, sheet_name="ground_truth")
    if len(sys.argv) > 1:
        companies = sys.argv[1:]
    else:
        companies = [c for c in truth["company"].unique()
                     if (OUTPUT_DIR / f"{c.lower()}_2025.csv").exists()]

    results = []
    for company in companies:
        r = compare(company, truth)
        results.append(r)
        print(f"\n### {company}: values {r['value_ok'].sum()}/{len(r)} correct, "
              f"pages {r['page_ok'].sum()}/{len(r)} correct")
        wrong = r[~r["value_ok"]]
        if not wrong.empty:
            print("  Wrong values:")
            print(wrong[["metric", "value_truth", "value_model", "page_truth", "page_model", "note"]]
                  .to_string(index=False))
        page_only = r[r["value_ok"] & ~r["page_ok"]]
        if not page_only.empty:
            print("  Right value, different page:")
            print(page_only[["metric", "value_model", "page_truth", "page_model"]]
                  .to_string(index=False))

    if results:
        all_r = pd.concat(results)
        out = OUTPUT_DIR / "comparison.xlsx"
        all_r.to_excel(out, index=False)
        print(f"\nOverall value accuracy: {all_r['value_ok'].mean():.0%}  -> saved {out}")


if __name__ == "__main__":
    main()
