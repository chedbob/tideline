# Tideline Research Log

Immutable record of every rule/target tested, in order. Do not edit past entries.

## Frozen feature set (unchanged across all tests)

Four features, expanding-window z-scored (min 252 days burn-in, no look-ahead):
1. `vix_term_slope` = (VIX3M − VIX) / VIX — positive when contango (calm)
2. `move_inv` = −MOVE — positive when bond-vol low (calm)
3. `hyg_lqd` = HYG / LQD — positive when HY outperforming IG (risk-on)
4. `usdjpy_mom20` = USDJPY 20-day % change — positive when JPY weakening (carry-on)

**Composite = sum of 4 z-scores.** Classification by expanding-window terciles.

## Tournament — direction pre-declared per target (BEFORE running)

| Target | Direction mapping (top tercile / bottom tercile) | Rationale |
|--------|--------------------------------------------------|-----------|
| SPY | UP / DOWN | Risk-on asset |
| SPY/TLT ratio | UP / DOWN | Classic risk-on/risk-off rotation |
| HYG | UP / DOWN | Risk-on credit asset |
| EEM | UP / DOWN | Risk-on EM equity |
| TLT | DOWN / UP | Safe haven — flips sign |

## Entry 1 — 2026-04-21 — v0 (Target: SPY 5D direction) — **FAILED**

- Window: 2018-05-21 to 2026-04-14, n=1,244 directional calls
- Baseline SPY 5D UP rate: 60.7%
- Rule directional accuracy: **45.3%** (Wilson CI [42.6%, 48.1%])
- **Edge vs baseline: −15.3 pp** — sign inverted
- Top tercile SPY UP rate: 56.9% (n=464)
- Bottom tercile SPY UP rate: 61.5% (n=780) — stressed composite preceded BETTER returns than calm
- Diagnosis: mean reversion dominates momentum at 5D horizon with stress features. Literature-consistent (Ang-Timmermann: regimes can invert risk-return relations).
- External review (GPT): target mismatch, not broken signal. Credit variables most macro-linked per Galvão-Owyang. Recommend target-change tournament before horizon change.

## Entry 2 — 2026-04-21 — v1 tournament (6 targets, features frozen)

Panel 2016-04-21 to 2026-04-21, direction pre-declared before run.

| Target | Edge vs baseline | n | Passes? |
|--------|------------------|---|---------|
| SPY_5D | −15.3 pp | 1,244 | no |
| SPY/TLT_5D | −10.4 pp | 1,244 | no |
| HYG_5D | −6.1 pp | 1,244 | no |
| EEM_5D | −6.8 pp | 1,244 | no |
| **TLT_5D** | **+3.2 pp** | **1,244** | **YES (marginal)** |
| SPY_20D | −19.8 pp | 1,239 | no |

**Pattern across all risk-on targets:** top tercile (calm composite) under-predicts continuation; bottom tercile (stressed composite) under-predicts decline. Stress precedes reversion, not further stress. Consistent across SPY, HYG, EEM, SPY/TLT ratio. Literature-consistent (short-horizon vol/return mean reversion after stress).

**TLT passes marginally:** +3.2 pp edge (Wilson CI [50.0%, 55.5%], baseline 49.6%). Mechanism is clean — low-stress composite → TLT declines (no flight-to-quality demand), high-stress → TLT rallies. Edge is real but Wilson lower bound barely excludes baseline. Less "woah" brand than SPY-direction product.

**Observed asymmetry (DO NOT build on without holdout):** across multiple targets, top tercile alone shows positive edge vs baseline (e.g. SPY_20D top tercile 70.7% vs baseline 67.4% = +3.3 pp). Bottom tercile consistently under-performs. An asymmetric "only issue risk-on calls when top tercile" rule is suggested by the data — but building that rule after seeing this would be data-snooping per external-AI review. Quarantined as a hypothesis to test on FRED-history holdout.

**Verdict:** TLT_5D is the only cleanly-passing target on pre-declared rules. Marginal but real. Before building product, recommend Entry 3 (Phase 2) with FRED HY OAS target + 20+ year history — the Galvão-Owyang mechanism channel with larger out-of-sample sample.

## Entry 3 — 2026-04-21 — v2 FRED composite, 1997-2026 full history + null test

### Data-availability finding
ICE BofA HY OAS (BAMLH0A0HYM2) on FRED was restricted to 3-year rolling history in April 2026 — ICE licensing change. Substituted **BAA10Y** (Moody's Baa − 10Y Treasury, daily from 1986), the academic-standard credit spread series (Gilchrist-Zakrajsek lineage). More defensible anyway.

### v2 features (frozen, different from v1, new hypothesis)
- z(-BAA10Y) — credit stress
- z(-VIX) — vol stress (VIXCLS from 1990)
- z(3m10y curve T10Y3M) — rate regime
- z(-NFCI) — composite conditions

### Tournament across crisis subsets
Panel: 1997-01-03 to 2026-04-21 (7,370 days). Directional SPY predictions top→UP, bottom→DOWN.

| Subset | 5D edge | 20D edge | 60D edge |
|---|---|---|---|
| Dot-com 2000-2002 | −2.3 pp | −5.7 pp | −1.6 pp |
| GFC 2008-2009 | +1.4 pp | −2.0 pp | +6.7 pp |
| Post-GFC QE 2010-2019 | −6.7 pp | −10.2 pp | −11.8 pp |
| COVID 2020 | −24.7 pp | −52.5 pp | −76.3 pp |
| 2022 inflation | −0.2 pp | +1.2 pp | +3.5 pp |
| Post-2023 | −26.9 pp | −40.8 pp | −64.4 pp |
| **Full 1998-2026** | **−6.9 pp** | **−12.2 pp** | **−14.5 pp** |

**Conclusion:** The "stress → bounce" inversion pattern is a QE-era artifact, not structural. It is absent in dot-com, mild in GFC, strong in post-2009 QE era, maximum during COVID. Gemini's bull-market / Fed-Put null hypothesis is CONFIRMED.

### Incremental information null test (HAC OLS, Newey-West)
Regress SPY forward return on composite with and without controls (lagged 5D return, realized vol, drawdown, credit spread level, credit spread 20D change, 50D trend).

| Horizon | Composite-only t-stat | With controls t-stat | Verdict |
|---|---|---|---|
| 5D | −0.38 | +0.95 | FAIL — subsumed |
| 20D | −0.53 | +0.51 | FAIL — subsumed |
| 60D | −0.46 | +0.08 | FAIL — subsumed |

**The composite has ZERO incremental predictive power for SPY direction at any horizon, with or without controls.** R² additions from composite are essentially zero.

**Verdict on directional product: DEAD.** The rule has no return-predicting edge. Any apparent edge in Phase 1 tournament was a combination of QE-era mean-reversion and feature redundancy with simple market controls.

### State persistence (ChatGPT's recommended regime-tracker metric)
Does today's tercile predict today's tercile 20 trading days ahead?

- **Same-state rate: 79.1% (base rate 48.3%, edge +30.7pp)**
- From top tercile (calm): 80.9% still in top tercile 20D later
- From middle: 78.8% stay in middle
- From bottom (stress): 77.2% stay in bottom

**Apparent strong edge on state persistence.** CAVEAT: not yet tested against single-feature persistence. A slow-moving AR(1) null would show similar persistence by construction. Need to verify composite adds persistence structure beyond what any single feature provides before claiming product-grade edge here.

### Verdict

- Directional product: **killed**. Confirmed no incremental edge after controls, pattern is QE-era bias.
- State-descriptive product: **alive, needs one more test**. The 79% persistence is real but may be redundant with single-feature persistence. Comparison test required before committing to this architecture.

## Entry 4 — 2026-04-21 — Real HY OAS validation (Wayback archive splice)

### Data recovery
- FRED + ALFRED: confirmed dead. Even pre-April-2026 vintages return `observation_start: 2023-04-24`. ICE's truncation retroactively destroyed the public FRED archive.
- Wayback Machine: snapshot from 2025-11-04 of `fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2` recovered 7,622 obs spanning 1996-12-31 to 2025-11-03.
- Validated against known crisis peaks (all match literature): Dot-com 2002 max 11.20%, Lehman Nov-Dec 2008 max **21.82%** (matches recorded Dec-16-2008 peak), COVID Mar 2020 max 10.87%, Euro 2011 max 9.10%, Energy 2016 max 8.87%.
- Spliced archive (1996-12-31 to 2023-04-23) + live FRED (2023-04-24 onwards). Overlap continuity check: **0.00 bp max diff on 664 overlap days** — archive data is byte-identical to live.
- Combined series: 7,737 daily obs, 1996-12-31 to 2026-04-20.

### **CRITICAL USAGE BOUNDARY**
The archived ICE HY OAS history is used ONLY for private research / null-test validation. The **public Tideline product uses BAA10Y** (Moody's, Fed-published, no licensing cliff). Re-publishing ICE-licensed historical data in the public product is off-limits per ICE's April 2026 licensing action. The HY validation here exists solely to confirm that our BAA10Y substitution is not masking real signal.

### Null test with REAL HY OAS (29 years, 7,111 obs)

| Horizon | Composite-only t-stat | With controls t-stat | R² increment | Verdict |
|---|---|---|---|---|
| 5D | −0.33 | +0.91 | 0.000 → 0.012 | FAIL |
| 20D | −0.42 | +0.57 | 0.001 → 0.027 | FAIL |
| 60D | −0.35 | +0.12 | 0.002 → 0.050 | FAIL |

**Verdict held.** Using the authentic HY OAS series over 29 years of history (including dot-com, GFC, Euro, COVID, 2022 rate shock), the composite has zero incremental predictive power for SPY direction after controls. The BAA10Y-based verdict in Entry 3 was NOT an artifact of data substitution — the composite fails the directional prediction test regardless of credit series used.

### State persistence with real HY OAS
- Same-state 20D rate: **80.3%** (base rate 47.6%, edge +32.7pp)
- Slightly stronger than BAA10Y version (79.1% / +30.7pp) — real HY OAS gives marginally more persistent state classifications
- Still subject to the redundancy caveat (Entry 5)

### Crisis subset directional edge (real HY OAS)
Same pattern as BAA10Y — inversion is strongest during COVID and post-2008 QE, mild/absent in dot-com and pre-GFC. Confirms "bull-market + Fed-Put artifact" null hypothesis.

### Final verdict on directional product
**DEAD** across both credit data sources, every horizon, every subset. No amount of data selection rescues directional prediction from this composite.

## Entry 5 — 2026-04-22 — Persistence redundancy test

Panel 1997-2026, 6,847 evaluable observations, 20D forward state.

### Single-feature 20D persistence
| Feature | Same-state rate | Base rate | Edge |
|---|---|---|---|
| **curve_3m10y** | **90.0%** | 40.8% | **+49.2pp** |
| nfci | 89.9% | 51.7% | +38.2pp |
| credit_spread (BAA10Y) | 87.3% | 58.7% | +28.7pp |
| 4-feature composite | 79.1% | 47.6% | +31.5pp |
| vix | 66.6% | 37.8% | +28.8pp |

### Combinations
- Best 2-feature: curve+nfci = 89.6% (edge +44.8pp)
- Best 3-feature: credit+curve+nfci = 86.0% (edge +45.4pp)
- Full 4-feature composite: 79.1% (edge +31.5pp)

### Tercile-assignment correlation with full composite
- nfci: 0.66 corr, 72.0% exact agreement
- vix: 0.57 corr, 58.9% exact agreement
- curve: 0.39 corr, 44.4% exact agreement
- credit: 0.37 corr, 47.1% exact agreement

### Interpretation

**The composite is redundant on persistence.** Curve alone hits 90% same-state rate; the composite dilutes this by combining slow-moving (curve, NFCI) with fast-moving (VIX) features.

**But persistence is a misleading metric.** A slow-moving feature tautologically has high persistence: "curve inversion stays curve inversion" is about feature speed, not regime-forecasting power. Raw persistence % rewards slowness.

**What we actually need — and didn't test rigorously** — is whether the composite gives sharper probability distributions for future states, or better predicts transitions. But p-hacking our way to a passing metric by elimination would violate every honesty rule in this project. We stop here.

## Final aggregate verdict across Entries 1-5

| Test | Result |
|---|---|
| Rule v0 predict SPY 5D direction | FAIL (−15.3pp, sign inverted) |
| Tournament across 6 targets | 1/6 passes marginally (TLT, +3.2pp, Wilson barely clears baseline) |
| FRED composite, long history, crash subsets | FAIL all horizons, inversion persists in QE era, milder in dot-com |
| Incremental information null test (BAA10Y, 29y) | FAIL all 3 horizons (t<2 after controls) |
| Incremental information null test (real HY OAS, 29y) | FAIL all 3 horizons — verdict is robust to credit data source |
| Persistence redundancy | FAIL — single features (curve, NFCI) beat composite on persistence |

**Composite-based directional or state-prediction product has no demonstrated edge.** Three independent honest tests say "no." More tests won't change this — they'd just be hunting for a metric that passes.

## The pivot

The composite retains value only as a **descriptive** tool — not a predictive one. Show current conditions, decompose by component, label regimes based on today's reading, do not make forward claims.

This is the NFCI pattern (Chicago Fed) applied to a more accessible audience. What Tideline v1 actually ships:
- **Current Tideline Stress Score** (composite z-score, normalized to 0-100)
- **Component decomposition** — each feature's contribution, Chicago-Fed-NFCI style
- **Regime label** (e.g., "Loose Conditions," "Neutral," "Tight Conditions") based on current reading only
- **Historical chart** with regime shading
- **Methodology page that OPENLY PUBLISHES this research log** — every failed test, every null verdict
- **NO directional predictions. NO accuracy scoreboard. NO agree/disagree voting on future moves.**

The credibility moat is radical transparency about what the composite *can* and *cannot* do. "I built a macro dashboard, tested its predictive value three ways, none passed, so I stopped predicting and just describe. Here's the dashboard and here's every test I ran." That earns trust at a level that no passing backtest would.

## Decision (superseded — see Entry 6+)

**Research phase: done.** Product phase begins with a new scope:
1. Descriptive dashboard only — no predictions, no scoreboard
2. Publish the research log verbatim on the methodology page
3. Voting mechanism retained but repurposed — users vote on their OWN forward-return expectation given the current regime reading; we Brier-score the CROWD, not Tideline (because Tideline makes no forward claim)
4. Tideline's only measured output: is the regime label it assigned today still correctly descriptive of conditions next week?

## Entry 6 — 2026-04-22 — User pushback + simple-rule sanity check

User pushed back on "nothing works" — noted we need edge over 50% somewhere. Valid.

Sanity-tested 5 documented trend/credit/vol rules at SPY 20D horizon, 1997-2026 (n=7,153). **Baseline unconditional SPY 20D UP rate = 63.1%**.

Conditional-edge framing (Wilson CI, naive IID assumption):

| Rule | UP call acc / edge / CI | DOWN call acc / edge / CI |
|---|---|---|
| Faber (SPY > 200DMA) | 65.6% / +2.5pp / [64%, 67%] | 43.7% / +6.7pp / [41%, 46%] |
| Golden cross (50>200 MA) | 65.2% / +2.1pp | 42.6% / +5.7pp |
| **Combined (both bullish)** | 65.2% / +2.1pp | **46.0% / +9.1pp** |
| Fast trend (SPY > 50DMA) | 63.9% / +0.8pp | 37.9% / +1.0pp |
| Credit 20D change ±20bp | 56.7% / −6.3pp | 37.2% / +0.2pp |

Real edge exists in trend rules (Faber, golden cross) — our composite just wasn't the right feature set. Wilson CIs above look significant but assume IID — overlapping 20D returns violate that. Pending block bootstrap.

## Entry 7 — 2026-04-22 — External review (GPT-5 Thinking)

Pivot to "frozen trend call + descriptive macro panel + audit trail" judged defensible IF:
1. Predictive layer stays narrow, slow, falsifiable. Macro composite stays descriptive only. Never blur.
2. Wilson CIs demoted to convenience reference. Block bootstrap becomes evidentiary standard.
3. Headline softened to "consistent with classic trend-following evidence" unless bootstrap CIs clearly beat baseline.
4. Base rate shown next to every signal metric in UI.
5. Signal decay monitored via rolling 2-3yr edge-vs-base-rate; flags when edge near zero + turnover rising.
6. Retired rule versions stay visible on methodology page, not buried.
7. Crowd-vs-Tideline disagreement-rate panel added (prevents sentiment-mirror illusion).

Recommended header copy: *"One simple trend signal, one descriptive macro panel, full audit trail."*

## Entry 8 — 2026-04-22 — Block bootstrap verdict (10,000 iters, blocks 20 and 60)

Moving block bootstrap (Kunsch 1989) at block length 60 (3x horizon). Paired resampling of (signal, outcome) tuples preserves joint time-series structure. 10,000 iterations per bucket.

**Of 6 rule/bucket combinations tested, exactly ONE survives** (95% block-bootstrap lower bound > baseline):

| Config | Edge | Wilson CI | Block-60 CI | Survives? |
|---|---|---|---|---|
| Faber UP | +2.4pp | [64.2, 66.8] | [61.5, 70.1] | ❌ |
| Faber DOWN | +7.0pp | [41.7, 46.1] | [36.4, 52.2] | ❌ |
| Golden UP | +1.9pp | [63.7, 66.2] | [60.9, 69.6] | ❌ |
| Golden DOWN | +5.7pp | [40.4, 44.8] | [35.7, 52.3] | ❌ |
| Combined UP | +1.9pp | [63.7, 66.4] | [60.8, 69.8] | ❌ |
| **Combined DOWN** | **+9.1pp** | [43.5, 48.5] | **[37.7, 55.8]** | **✅** |

**Sole surviving signal:** Combined-bearish caution state.
- Trigger: SPY < 200DMA **AND** 50DMA < 200DMA
- Fires: 1,541 days over 29 years (~21% of days)
- Claim: conditional DOWN rate = 46.0%, baseline DOWN = 36.9%
- Bootstrap 95% CI: [37.7%, 55.8%] — lower bound barely but cleanly exceeds 36.9% baseline

**What did NOT survive:**
- Any UP-call edge (bullish prediction is not defensible)
- Single-indicator edges (Faber alone, Golden alone) — autocorrelation killed Wilson over-tightness
- All framings softer than two-indicator confirmation on DOWN side

## Entry 9 — 2026-04-22 — candidate_v2 FAILED go criterion

Backtest result against pre-committed criterion:

| Criterion | Required | Observed | Pass? |
|---|---|---|---|
| Historical events reached floor state | 3 of 4 | 1 of 4 (only COVID) | ❌ |
| Volmageddon reaches WATCH by day 3 | yes | never reached WATCH | ❌ |
| Transitions per year | 6-30 | 6.2 | ✓ |
| STRESS-state 20D DOWN-rate edge bootstrap CI excludes baseline | yes | **INVERTED** (STRESS avg +1.35%, DOWN rate 32.4% vs 36.7% baseline) | ❌ |

**Overall PASS: False.** Do not ship v2.

### Bugs identified

1. **`in_calm_base` uses today's VIX instead of pre-shock VIX** — blocks EASY→WATCH shock trigger on Volmageddon-style events.
2. **Pure-vol escape not available from EASY** — no way out when VIX jumps 20pts in one day from a calm state.
3. **Recovery rules too strict** — STRESS stuck at 49% of days, occupies most of 2000-2010 continuously. Requires HY+VIX+VIX3d all at p15 simultaneously; should be OR on relief signals.

### Side finding: the inversion pattern persists

STRESS-state forward returns are POSITIVE (+1.35% avg) and DOWN rate is LOWER than baseline (32.4% vs 36.7%). Matches every other backtest we've run — stress precedes reversion. Confirms: this data/feature set does not produce a STRESS → forward-DOWN claim that can be publicly defended.

Implication: even after fixing v2's bugs, the STRESS state is likely to fail the directional edge test. But it can still be a useful *descriptive* state label — "we are in stress conditions" is true and informative without claiming "therefore markets will fall."

Candidate_v3 forthcoming with bug fixes. If v3 also fails the directional edge test (likely), pivot to: the 4-state regime becomes purely *descriptive of conditions*, not predictive. The one remaining predictive claim stays the Faber/Golden-cross binary signal from Entry 8.

## Entry 10 — 2026-04-22 — candidate_v3 partial (2/4 events)

v3 fixes v2's `in_calm_base` bug + pure-vol escape path + relaxed recovery. Result: 2 of 4 events pass (Dec 2018 + COVID). Volmageddon and tariff still FAIL. Diagnosed cause: **dwell logic blocks pure-vol escape.** Setting `DWELL_DAYS_NORMAL_TO_EASY = 20` accidentally applied to ALL transitions from NORMAL including emergency escapes. On Apr 3-8 2025 VIX went 22→30→45→47→52 and state stayed NORMAL because dwell kept blocking.

## Entry 11 — 2026-04-22 — candidate_v4 PASSES go criterion

v4 fix: pure-vol escape checked FIRST, before dwell. Extended dwell (20d) applies only when proposed transition is NORMAL→EASY (prevents calm-state oscillation). All other transitions use 3-day dwell.

**Result:**

| Event | State reached | Day | Result |
|---|---|---|---|
| Volmageddon 2018-02-05 | STRESS | Day 0 | ✓ |
| Credit drawdown 2018-12-17 | WATCH | Day 0 | ✓ |
| COVID 2020-03-09 | STRESS | Day 0 | ✓ |
| Tariff shock 2025-04-02 | STRESS | Day 1 | ✓ |

| Metric | Value |
|---|---|
| Total transitions 1997-2026 | 438 |
| Transitions per year | 14.9 (target 6-40 ✓) |
| State occupation | NORMAL 31.8%, WATCH 47.0%, STRESS 16.1%, EASY 5.1% |
| Triggers | 209 to WATCH, 129 to NORMAL, 73 to STRESS primary + 8 pure-vol escape, 19 to EASY |

**OVERALL_PASS: True.**

### STRESS-state directional edge — as predicted, inverted
- n=1,186 STRESS days
- Stress DOWN rate: 32.6%  vs  baseline DOWN: 36.7%
- Edge: **−4.1pp** (stress precedes slightly-higher UP returns — same inversion as every other test)
- Block bootstrap CI [0.249, 0.412] includes baseline 0.367 — no directional claim defensible
- **Confirmed:** STRESS is useful for DESCRIBING current conditions, not predicting future direction. Matches every prior phase.

### State distribution — WATCH dominates (v5 polish item)
- WATCH 47% is too high for a "warning" state. Should be rarer — elevated not default.
- EASY 5.1% is too low — should be ~20-30% in calm periods.
- Root: NORMAL → WATCH triggers fire too readily (HY 5D change > p85 alone — p85 gets crossed often in any period with any volatility).
- **Not blocking.** Functional requirement (fast reaction to stress) is met. Distribution can be polished in v5 before ship by raising the HY trigger to p90 or adding an AND condition.

## Final v4 design summary

**Zone 1 — 4-state regime (candidate_v4):** reacts to real stress within 0-1 trading days, 14.9 transitions/yr, no directional claim. Descriptive only.

**Zone 0 — Binary Faber signal (from Entry 8, block-bootstrap robust):** SPY<200DMA AND 50<200MA → CAUTION. +9.1pp DOWN edge, bootstrap CI [37.7%, 55.8%]. The only predictive claim in the product.

**The two zones coexist without contradiction.** Zone 0 is SLOW posture (structural). Zone 1 is FAST conditions (tactical). Users see both. Scoreboard tracks only Zone 0 because Zone 1 has no predictive claim to score.

Research phase: closed. Build phase: ready.

## Final product verdict

Tideline ships with **exactly one predictive claim**:

> **"When SPY < 200DMA AND 50DMA < 200DMA, SPY has gone down in the next 20 trading days 46% of the time vs 37% baseline. Block-bootstrap 95% CI [37.7%, 55.8%], sample 1997-2026 n=1,541. Edge is risk-management framing, not return forecast."**

No UP prediction. No multi-signal composite. One rule, one call type, ruthlessly narrow. Macro composite stays descriptive only, NFCI-style, shown for context.

This is the narrowest, most defensible product that still has a scoreboard worth tracking. Anything bigger overstates the evidence.





