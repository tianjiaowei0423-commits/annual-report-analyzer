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

Prior year (--year 2024): there is a hand-made answer key only where ground_truth.xlsx has
rows for that year; for the rest only the page check runs. Both years are printed on the same
page, so a 2024 value equal to the 2025 value is flagged as a possible wrong-column read.

Usage:
  python compare.py                # every company that has an output file
  python compare.py TORM
  python compare.py --year 2024
"""

import argparse
import re
from itertools import combinations
from pathlib import Path

import pandas as pd

from extract import COMPANIES, REPORTS_DIR, YEAR, read_pages

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


def sum_parts(value: float, note: str):
    """Yield every set of numbers in the note that adds up to the value (e.g. 502 + 290 = 792).
    A note can hold more than one sum (statement lines and a cross-check from another note),
    and numbers from different sums can add up by chance, so every candidate is tried.
    Other numbers in the note (years, page numbers, 'includes lease 5,207') are ignored."""
    nums = [float(m.replace(",", "")) for m in PAGE_NUMBER_RE.findall(str(note).replace("−", "-"))]
    nums = [n for n in nums if abs(abs(n) - abs(value)) > MATCH_EPS][:12]
    for k in range(2, min(MAX_PARTS, len(nums)) + 1):
        for combo in combinations(nums, k):
            if (abs(sum(combo) - value) <= TOLERANCE
                    or abs(sum(abs(c) for c in combo) - abs(value)) <= TOLERANCE):
                yield list(combo)


def verify_page(value, page, note, pages: list[str]) -> bool:
    v = pd.to_numeric(value, errors="coerce")
    p = pd.to_numeric(page, errors="coerce")
    if pd.isna(v) or pd.isna(p) or not 1 <= int(p) <= len(pages):
        return False
    text = pages[int(p) - 1].replace("−", "-").replace("–", "-")  # unicode minus, en dash
    nums, flat = numbers_on(text), re.sub(r"\s+", "", text)
    if on_page(v, nums, flat):
        return True
    # a sum (e.g. interest on borrowings 68.3 + lease interest 0.8): every part on the page
    return any(all(on_page(x, nums, flat) for x in parts) for parts in sum_parts(float(v), note))


def compare(company: str, truth: pd.DataFrame, year: int) -> pd.DataFrame | None:
    out_file = OUTPUT_DIR / f"{company.lower()}_{year}.csv"
    if not out_file.exists():
        flag = "" if year == YEAR else f" --year {year}"
        print(f"\n### {company} {year}: {out_file} not found. Run: python extract.py {company}{flag}")
        return None
    pred = pd.read_csv(out_file)[["metric", "value", "page", "note"]]

    t = truth[(truth["company"] == company) & (truth["year"] == year)][["metric", "value", "page"]]
    if t.empty:
        # no answer key for this company and year: the page check still runs
        merged = pred.rename(columns={"value": "value_model", "page": "page_model"})
        merged.insert(1, "value_truth", float("nan"))
        merged.insert(2, "page_truth", float("nan"))
    else:
        merged = t.merge(pred, on="metric", how="left", suffixes=("_truth", "_model"))
    merged["has_truth"] = not t.empty

    v_t = pd.to_numeric(merged["value_truth"], errors="coerce")
    v_m = pd.to_numeric(merged["value_model"], errors="coerce")
    merged["value_ok"] = (v_t - v_m).abs() <= TOLERANCE
    merged["page_same"] = (pd.to_numeric(merged["page_truth"], errors="coerce")
                           == pd.to_numeric(merged["page_model"], errors="coerce"))

    pages = read_pages(REPORTS_DIR / COMPANIES[company]["file"])
    merged["page_verified"] = [verify_page(r.value_model, r.page_model, r.note, pages)
                               for r in merged.itertuples()]

    # Prior year: both columns are printed on the same page, so the page check cannot tell
    # them apart. A value identical to the report-year value suggests the wrong column.
    merged["same_as_report_year"] = False
    current = OUTPUT_DIR / f"{company.lower()}_{YEAR}.csv"
    if year != YEAR and current.exists():
        cur = pd.read_csv(current).set_index("metric")["value"]
        v_cur = pd.to_numeric(merged["metric"].map(cur), errors="coerce")
        merged["same_as_report_year"] = (v_m - v_cur).abs() < 1e-9

    merged.insert(0, "company", company)
    merged.insert(1, "year", year)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Check extracted figures.")
    parser.add_argument("companies", nargs="*", help="default: every company with an output file")
    parser.add_argument("--year", type=int, default=YEAR)
    args = parser.parse_args()

    truth = pd.read_excel(GROUND_TRUTH, sheet_name="ground_truth")
    truth["company"] = truth["company"].astype(str).str.strip()
    companies = args.companies or [c for c in COMPANIES
                                   if (OUTPUT_DIR / f"{c.lower()}_{args.year}.csv").exists()]

    results = []
    for company in companies:
        if company not in COMPANIES:
            print(f"\n### {company}: not in extract.py COMPANIES, skipped.")
            continue
        r = compare(company, truth, args.year)
        if r is None:
            continue
        results.append(r)
        n = len(r)
        if r["has_truth"].iloc[0]:
            values = f"values {r['value_ok'].sum()}/{n} correct, "
        else:
            values = f"no answer key for {args.year} (values not compared), "
        print(f"\n### {company} {args.year}: {values}"
              f"cited page shows the figure {r['page_verified'].sum()}/{n}")

        wrong = r[r["has_truth"] & ~r["value_ok"]]
        if not wrong.empty:
            print("  Wrong values:")
            print(wrong[["metric", "value_truth", "value_model", "page_truth", "page_model", "note"]]
                  .to_string(index=False))
        unverified = r[~r["page_verified"]]
        if not unverified.empty:
            print("  Not found on the cited page (check by hand):")
            print(unverified[["metric", "value_model", "page_model", "note"]].to_string(index=False))
        same = r[r["same_as_report_year"]]
        if not same.empty:
            print(f"  Same value as {YEAR}: the {YEAR} column may have been read (check by hand):")
            print(same[["metric", "value_model", "page_model"]].to_string(index=False))

    if not results:
        print("\nNothing compared.")
        return
    all_r = pd.concat(results)
    name = "comparison.xlsx" if args.year == YEAR else f"comparison_{args.year}.xlsx"
    out = OUTPUT_DIR / name
    all_r.to_excel(out, index=False)
    keyed = all_r[all_r["has_truth"]]
    accuracy = (f"value accuracy {keyed['value_ok'].mean():.0%} "
                f"(on {len(keyed)} figures with an answer key), " if not keyed.empty else "")
    print(f"\nOverall: {accuracy}"
          f"cited page verified {all_r['page_verified'].mean():.0%}, "
          f"same as {YEAR}: {all_r['same_as_report_year'].sum()}  -> saved {out}")


if __name__ == "__main__":
    main()