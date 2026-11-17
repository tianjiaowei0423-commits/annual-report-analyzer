# annual-report-analyzer

AI tool that reads company annual reports, extracts key financial figures, turns them into
credit ratios and writes a short credit view per company, with checks at every step.

Test set: four Danish shipping companies, 2025 annual reports (DFDS, Maersk, Norden, TORM),
with the 2024 comparatives from the same reports to show the trend.

## How it works

```
Annual report PDF (200+ pages)
   │
   ▼
1. extract.py    Find the ~10 relevant pages (keyword scoring, local and free),
   │             send only those to Claude, get 14 figures back with unit,
   │             source page and a note on how each was found
   ▼
2. compare.py    Check every figure against a hand-made answer key
   │             and open the cited page to confirm the number is printed there
   ▼
3. analyze.py    Turn the 14 figures into 9 credit ratios, put the companies
   │             side by side and show the 2024 -> 2025 change (pure Python)
   ▼
4. summarize.py  Claude writes a short credit view per company from the ratio
                 table; every number in the text is checked against the table
```

The model is used only where judgement on text is needed (reading the statements, writing
the summary). Everything that can be calculated or checked is done in plain Python.

## Results

### 1. Extraction accuracy

For each annual report I extracted the 14 metrics by hand (`data/ground_truth.xlsx`) and
compared them with the tool's output using `compare.py`.

| Company | Values correct | Cited page shows the figure |
|---|---|---|
| DFDS | 14/14 | 14/14 |
| Maersk | 14/14 | 14/14 |
| Norden | 14/14 | 14/14 |
| TORM | 14/14 | 14/14 |
| **Total** | **56/56** | **56/56** |

- **Values correct:** matches the hand-extracted figure (±1 for rounding).
- **Cited page shows the figure:** `compare.py` opens the PDF page the model cites and checks
  that the number (or each part of a sum) is printed there, so every figure can be traced
  back to its source.

**2024 comparatives.** The 2025 reports also print 2024 next to each figure, so
`extract.py --year 2024` reads the same pages and takes the prior-year column. There is no
hand-made answer key for 2024 yet, so two automatic checks are used instead: every 2024
figure is printed on the page the model cites (56/56), and no 2024 figure equals its 2025
value (0/56), which would suggest the wrong column was read.

**Blind test on an unseen company.** TORM was added only after the page-finding rules were
built and tuned on DFDS, Maersk and Norden. On the first run, all 14 values were correct.
The test also exposed a weakness: a segment note that repeats balance sheet line items
was picked instead of the consolidated balance sheet. I changed the page finder so that note
pages can no longer fill the main statement slots, then re-ran all four companies to confirm
nothing else broke.

### 2. Credit ratios (2025)

| Ratio | DFDS | Maersk | Norden | TORM |
|---|---|---|---|---|
| EBITDA margin | 12.1% | 17.7% | 14.5% | 42.6% |
| EBIT margin | 1.7% | 6.5% | 5.1% | 26.6% |
| NIBD / EBITDA, as reported | 4.1x | net cash | 0.8x | 1.5x |
| Net debt / EBITDA, common definition | 4.1x | 1.0x | 0.8x | 1.5x |
| Interest cover (EBIT / interest) | 0.7x | 3.1x | 4.3x | 5.2x |
| Equity ratio | 36.2% | 64.2% | 53.9% | 65.4% |
| Cash conversion (OCF / EBITDA) | 88.2% | 102.4% | 83.2% | 87.4% |
| Capex intensity (capex / revenue) | 5.1% | 8.9% | 20.2% | 23.2% |
| FCF margin (OCF − capex) / revenue | 5.6% | 9.2% | −8.1% | 14.1% |

Full table and definitions: `output/ratios.xlsx`. Only ratios are compared across companies,
because DFDS reports in DKK and the other three in USD.

**One FCF definition for all.** Each company defines free cash flow in its own way. Reported
FCF and OCF − capex differ at all four; at Norden the reported figure is positive
(USD 170.4m) while OCF − capex is negative (USD −251.9m). The tool therefore uses one
common definition for comparison and keeps the reported figure next to it.

**One net debt definition for all.** The same applies to net interest-bearing debt (NIBD).
The common definition is interest-bearing debt incl. leases − cash. For DFDS, Norden and TORM
the two are almost equal. Maersk reports net cash (USD −2,947m), because it also deducts
securities and interest-bearing receivables; on the common definition it has net debt of
USD 9,878m, or 1.0x EBITDA. Leverage is still low, but "net cash" holds only on Maersk's own
definition. This also explains why a net cash company has interest cover of only 3.1x:
its large lease liabilities carry interest.

### 3. Trend, 2024 to 2025

On most ratios all four companies were weaker in 2025 than in 2024 (capex intensity is not
counted as better or worse):

| | Better | Worse | Flat |
|---|---|---|---|
| DFDS | 2 | 5 | 0 |
| Maersk | 1 | 6 | 0 |
| Norden | 2 | 4 | 1 |
| TORM | 1 | 6 | 0 |

The changes that matter most for credit:

- **DFDS:** interest cover fell from 1.8x to 0.7x, so EBIT no longer covers interest;
  leverage rose from 3.9x to 4.1x.
- **Maersk:** interest cover fell from 6.5x to 3.1x as the EBIT margin fell from 11.7% to 6.5%.
- **Norden:** capex intensity rose from 11.7% to 20.2%, the FCF margin fell from −1.4% to
  −8.1%, and leverage rose from 0.5x to 0.8x: the investment is being funded with debt.
- **TORM:** the EBITDA margin fell from 54.6% to 42.6% but is still the highest of the four;
  capex intensity fell from 37.4% to 23.2%.

Margins fell at three of the four companies at the same time, across different segments of
shipping, which points to the market as much as to company-specific factors. Full table:
`output/ratios.xlsx`, sheet `trend` (changes under 0.5 percentage points or 0.1x count as flat).

### 4. Credit summaries

`output/credit_summaries.md` holds a headline, profitability, debt, cash, trend and "watch"
point for each company, plus a peer comparison. Example (DFDS):

> DFDS has the highest leverage of the four at 4.1x net debt / EBITDA, and EBIT does not
> cover interest after interest cover fell from 1.8x to 0.7x.

**How the text is controlled**

- The model sees only the ratio table and may not calculate new numbers or add figures
  from memory.
- Rankings (best to worst on each ratio) and the direction of each 2024 -> 2025 change
  (better, worse, flat) are worked out in Python and given to the model. Every comparison
  word in the text (highest, lowest, improved, fell) must follow them, and the model may not
  calculate the size of a change itself.
- After the call, `summarize.py` checks every number in the text against the table
  (allowing normal rounding) and writes the result at the end of the file.
- No credit ratings and no buy/sell views.

**What the automatic check missed.** On the first full run the number check passed, but
reading the output by hand I found two reasoning errors: interest cover of 0.7x was
described as if it just about covered interest (below 1.0x it does not cover it at all), and a
ratio (capex intensity, 20.2%) was compared with an amount (operating cash flow). Every
number was correct; the logic was not. I added two rules to the prompt (describe each value
as what it is; compare a ratio with a ratio and an amount with an amount), re-ran, and
checked both points again.

A second round came after adding the common net debt definition. The number check passed
again, but the text still had comparison errors: it called Maersk the "strongest balance
sheet" although TORM leads on equity ratio and interest cover and Norden has lower leverage,
and in the peer summary it wrote that Maersk's FCF margin (9.2%) was above TORM's (14.1%),
then corrected itself in the same sentence. More prompt rules would not fix this reliably,
because the model was still comparing numbers itself. So I moved the comparisons into code:
`summarize.py` ranks the companies on each ratio, and the model may only use those rankings.
On the next run every comparison matched.

The lesson: the number check confirms that no figure is invented, but the reasoning still
needs a person to read it, and where a person finds an error that code can prevent, the fix
goes into code rather than into the prompt.

### Cost

Only 10–13 of ~200 pages are sent to the model, about 17,000 input tokens
(roughly USD 0.05) per report. One summary run for all four companies costs a few cents.

## Usage

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Put the Anthropic API key in a `.env` file (`ANTHROPIC_API_KEY=...`, not committed) and the
annual reports in `data/reports/` (e.g. `dfds_2025.pdf`, not committed).

```bash
python extract.py DFDS --dry-run   # show which pages would be sent (free)
python extract.py all              # extract the 14 figures for every company
python extract.py all --year 2024  # the prior-year column from the same reports
python compare.py                  # check against the ground truth
python compare.py --year 2024      # page check and wrong-column check for 2024
python analyze.py                  # ratios and trend -> output/ratios.xlsx
python summarize.py --dry-run      # show the prompt (free)
python summarize.py                # credit summaries -> output/credit_summaries.md
```

## Limitations and next steps

- **2024 has no hand-made answer key yet.** The automatic checks confirm that each figure is
  printed in the report and is not the 2025 value, but not that it is the right line item.
  Next: hand-extract DFDS 2024 as an answer key.
- **Two years only.** Enough to show a direction, not a cycle; shipping earnings swing with
  freight rates, so a longer history would say more.
- **Four companies, one sector.** The page finder was tuned on shipping reports; other
  sectors may need new keywords.
  