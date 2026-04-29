# Data Archive — Usage Boundaries

## `hy_oas_bamls_archived_20251104.csv`

Snapshot of `BAMLH0A0HYM2` (ICE BofA US High Yield Index Option-Adjusted Spread) pulled from the Wayback Machine cache of FRED's public CSV endpoint, dated 2025-11-04. Covers 1996-12-31 to 2025-11-03, 7,622 daily observations.

**Why this archive exists:** ICE Data Indices restricted FRED's public access to this series in April 2026 to a rolling 3-year window. All long-history copies of this series were retroactively removed from FRED's live API and ALFRED's vintage archive. The Wayback snapshot captured the data while it was still publicly published on FRED.

## `hy_oas_spliced.csv`

Combined series: archive (1996-12-31 to 2023-04-23) + live FRED (2023-04-24 onwards). Overlap continuity was verified at 0.00 bp max difference across 664 overlap days. Used for backtest research only.

## Usage boundary — read before using

### Allowed

- Private research, backtests, null tests, internal validation of model substitutions
- Academic-style research log entries referencing "we validated the null-test verdict against ICE HY OAS history"
- Confirming that a substitute series (e.g., BAA10Y) doesn't mask signal

### NOT allowed

- Re-publishing historical HY OAS values in any public-facing Tideline display, chart, or download
- Using the HY OAS history as a composite input in the deployed live product
- Sharing the CSV outside this research repository
- Any commercial use

### Why this matters

ICE licensed this data to FRED under terms that included public redistribution. When ICE restricted the license in April 2026, they ended public redistribution rights. Using a Wayback-cached copy for private research is defensible as archival/academic use. Using it in a public product — even with attribution — is not.

### Production product

The live public Tideline product uses:
- **BAA10Y** (Moody's Baa corporate bond yield − 10Y Treasury, Fed-published, 1986-present) as the composite credit-stress feature
- Optionally: an HYG-derived HY spread proxy computed from iShares-published data as a supplementary display

Neither of these dependencies carry ICE licensing risk.
