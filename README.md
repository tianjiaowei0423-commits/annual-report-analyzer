# annual-report-analyzer
AI tool that extracts financial data from annual reports and builds peer benchmarks and credit summaries

## Results

**Accuracy test.** For each annual report I extracted the 14 metrics by hand
(`data/ground_truth.xlsx`) and compared them with the tool's output using `compare.py`.

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

**Cost.** Only 10–13 of ~200 pages are sent to the model, about 17,000 input tokens
(roughly USD 0.05) per report.