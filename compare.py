"""
Compare the model's extraction (output/<company>_2025.csv) with the
hand-made ground truth (data/ground_truth.xlsx, sheet "ground_truth").

Two checks per metric:
  value_ok       the value matches the ground truth (within +-1 for rounding)
  page_verified  the page the model cites really shows the figure: the value itself,
                 or, for a sum, the numbers in the model's note that add up to it
                 (the same figure often appears on several pages, so this is fairer
                 than requiring the exact page in the ground truth)
page_same (same page as the ground truth) is still saved, for reference.

Usage:
  python compare.py            # every company that has an output file
  python compare.py TORM
"""

import re
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

from extract import COMPANIES, REPORTS_DIR, read_pages

GROUND_TRUTH = Path("data/ground_truth.xlsx")
OUTPUT_DIR = Path("output")
TOLERANCE = 1.0      # rounding difference allowed (extraction rule: +-1)
MATCH_EPS = 0.051    # a figure "is on the page" if it matches to one decimal
PAGE_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
MAX_PARTS = 6        # a sum in the note has at most this many parts


def numbers_on(text: str) -> list[float]:
    """Every number on a page as an absolute value ("1,339.5" -> 1339.5, "-308.5" -> 308.5)."""
    text = text.replace("−", "-")  # unicode minus
    out = []
    for m in PAGE_NUMBER_RE.findall(text):
        try:
            out.append(abs(float(m.replace(",", ""))))
        except ValueError:
            pass
    return out


def written_forms(x: float) -> list[str]:
    """How a figure can be printed: 3743 -> '3,743', '3743'; 570.8 -> '570.8'."""
    x = abs(x)
    if abs(x - round(x)) < 1e-9:
        n = int(round(x))
        return [f"{n:,}", str(n), f"{n:,}.0", f"{n}.0"]
    return [f"{x:,.1f}", f"{x:.1f}", f"{x:,.2f}", f"{x:.2f}"]


def on_page(x: float, page_numbers: list[float], flat_text: str) -> bool:
    if any(abs(abs(x) - n) <= MATCH_EPS for n in page_numbers):
        return True
    # pypdf sometimes glues neighbouring columns together ("3,7433,560"), so also look
    # for the printed form inside the text. Only for distinctive figures (4+ digits or
    # a decimal), so a short number like 7 does not match everywhere.
    forms = written_forms(x)
    if abs(x) >= 1000 or abs(abs(x) - round(abs(x))) > 1e-9:
        return any(f in flat_text for f in forms)
    # pypdf can also put a space inside a number ("-766" read as "-76 6"). With spaces
    # removed, a short negative figure is still distinctive with its sign or brackets,
    # as long as no digit follows ("-95" must not match "-950").
    if x < 0:
        n = re.escape(str(int(round(abs(x)))))
        return bool(re.search(rf"(-{n}(?![\d.,]\d)(?!\d))|(\({n}\))", flat_text))
    return False


def sum_parts(value: float, note: str) -> list[float] | None:
    """Find numbers in the note that add up to the value (e.g. 502 + 290 = 792).
    Other numbers in the note (years, page numbers, 'includes lease 5,207') are ignored."""
    nums = [float(m.replace(",", "")) for m in PAGE_NUMBER_RE.findall(str(note).replace("−", "-"))]
    nums = [n for n in nums if abs(abs(n) - abs(value)) > MATCH_EPS][:12]
    for k in range(2, min(MAX_PARTS, len(nums)) + 1):
        for combo in combinations(nums, k):
            if (abs(sum(combo) - value) <= TOLERANCE
                    or abs(sum(abs(c) for c in combo) - abs(value)) <= TOLERANCE):
                return list(combo)
    return None


def verify_page(value, page, note, pages: list[str]) -> bool:
    v = pd.to_numeric(value, errors="coerce")
    p = pd.to_numeric(page, errors="coerce")
    if pd.isna(v) or pd.isna(p) or not 1 <= int(p) <= len(pages):
        return False
    text = pages[int(p) - 1].replace("−", "-").replace("–", "-")  # unicode minus, en dash
    nums, flat = numbers_on(text), re.sub(r"\s+", "", text)
    if on_page(v, nums, flat):
        return True
    # a sum (e.g. interest on borrowings 68.3 + lease interest 0.8): each part on the page
    parts = sum_parts(float(v), note)
    return parts is not None and all(on_page(x, nums, flat) for x in parts)


def compare(company: str, truth: pd.DataFrame) -> pd.DataFrame | None:
    t = truth[truth["company"] == company][["metric", "value", "page"]]
    if t.empty:
        found = sorted(repr(c) for c in truth["company"].dropna().unique())
        print(f"\n### {company}: no rows in the ground truth sheet, skipped.\n"
              f"  Company names in {GROUND_TRUTH}: {', '.join(found)}\n"
              f"  Check the spelling (also capitals and spaces) and that Excel is saved.")
        return None
    out_file = OUTPUT_DIR / f"{company.lower()}_2025.csv"
    if not out_file.exists():
        print(f"\n### {company}: {out_file} not found. Run: python extract.py {company}")
        return None

    pred = pd.read_csv(out_file)
    merged = t.merge(pred[["metric", "value", "page", "note"]], on="metric",
                     how="left", suffixes=("_truth", "_model"))
    v_t = pd.to_numeric(merged["value_truth"], errors="coerce")
    v_m = pd.to_numeric(merged["value_model"], errors="coerce")
    merged["value_ok"] = (v_t - v_m).abs() <= TOLERANCE
    merged["page_same"] = (pd.to_numeric(merged["page_truth"], errors="coerce")
                           == pd.to_numeric(merged["page_model"], errors="coerce"))

    if company in COMPANIES:
        pages = read_pages(REPORTS_DIR / COMPANIES[company]["file"])
        merged["page_verified"] = [verify_page(r.value_model, r.page_model, r.note, pages)
                                   for r in merged.itertuples()]
    else:
        print(f"  ({company} not in extract.py COMPANIES: page check skipped)")
        merged["page_verified"] = False
    merged.insert(0, "company", company)
    return merged


def main() -> None:
    truth = pd.read_excel(GROUND_TRUTH, sheet_name="ground_truth")
    truth["company"] = truth["company"].astype(str).str.strip()
    if len(sys.argv) > 1:
        companies = sys.argv[1:]
    else:
        companies = [c for c in truth["company"].unique()
                     if (OUTPUT_DIR / f"{c.lower()}_2025.csv").exists()]

    results = []
    for company in companies:
        r = compare(company, truth)
        if r is None:
            continue
        results.append(r)
        n = len(r)
        print(f"\n### {company}: values {r['value_ok'].sum()}/{n} correct, "
              f"cited page shows the figure {r['page_verified'].sum()}/{n} "
              f"(same page as ground truth {r['page_same'].sum()}/{n})")
        wrong = r[~r["value_ok"]]
        if not wrong.empty:
            print("  Wrong values:")
            print(wrong[["metric", "value_truth", "value_model", "page_truth", "page_model", "note"]]
                  .to_string(index=False))
        unverified = r[r["value_ok"] & ~r["page_verified"]]
        if not unverified.empty:
            print("  Right value, but not found on the cited page (check by hand):")
            print(unverified[["metric", "value_model", "page_truth", "page_model", "note"]]
                  .to_string(index=False))
        other_page = r[r["value_ok"] & r["page_verified"] & ~r["page_same"]]
        if not other_page.empty:
            print("  Right value on a different page than the ground truth (fine, for info):")
            print(other_page[["metric", "value_model", "page_truth", "page_model"]]
                  .to_string(index=False))

    if results:
        all_r = pd.concat(results)
        out = OUTPUT_DIR / "comparison.xlsx"
        all_r.to_excel(out, index=False)
        print(f"\nOverall value accuracy: {all_r['value_ok'].mean():.0%}, "
              f"cited page verified: {all_r['page_verified'].mean():.0%}  -> saved {out}")
    else:
        print("\nNothing compared.")


if __name__ == "__main__":
    main()
