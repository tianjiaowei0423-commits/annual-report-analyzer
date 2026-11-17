# annual-report-analyzer

AI tool that reads company annual reports, extracts key financial figures, turns them into
credit ratios and writes a short credit view per company, with checks at every step.

Test set: four Danish shipping companies, 2025 annual reports (DFDS, Maersk, Norden, TORM).

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
3. analyze.py    Turn the 14 figures into 9 credit ratios and put the
   │             companies side by side (pure Python, no API call)
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

### 3. Credit summaries

`output/credit_summaries.md` holds a headline, profitability, debt, cash and "watch" point
for each company, plus a peer comparison. Example (DFDS):

> DFDS has the highest leverage in the group and an EBIT that does not cover interest,
> though cash generation before investment is still positive.

**How the text is controlled**

- The model sees only the ratio table and may not calculate new numbers or add figures
  from memory.
- Rankings (best to worst on each ratio) are worked out in Python and given to the model.
  Every comparison word in the text (highest, lowest, above, below) must follow them.
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
python compare.py                  # check against the ground truth
python analyze.py                  # ratios -> output/ratios.xlsx
python summarize.py --dry-run      # show the prompt (free)
python summarize.py                # credit summaries -> output/credit_summaries.md
```

## Limitations and next steps

- **One year only.** Next: extract 2024 as well, to show whether each company is improving or
  getting worse.
- **Four companies, one sector.** The page finder was tuned on shipping reports; other
  sectors may need new keywords.