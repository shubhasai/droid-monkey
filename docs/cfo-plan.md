# droid-monkey business plan

droid-monkey sells as a **CI add-on for Android teams**. It runs on an emulator on every PR or every night, attacks the app with real-world faults, and fixes what breaks with the developer's consent. Each fix is proven by replaying the same attack and arrives as a PR with the evidence.

The plan is built in cfo.ai from droid-monkey's own measured costs, and the plan in turn sets how much the agent may spend on each run.

```
runs → neatlogs traces + run receipts → droid-monkey econ → drivers.csv → cfo.ai model
                                                          └→ unit_economics.json → droid-monkey run --tier (fix budget)
```

## What a run costs (measured)

`droid-monkey econ` reads every run of MonkeyMart, Wikipedia and architecture-samples. The full table, with the source of each number, is in [`business/drivers.csv`](../business/drivers.csv).

| Driver | Value | Source |
|---|---|---|
| One attack | $0 model spend, 31 s mean (p90 44 s) | 74 attack verdicts |
| Attacks that break the app | 36% | 74 verdicts on test apps, some with planted bugs, so higher than a typical app |
| One fix attempt (diagnosis + coding agent + replay) | $0.56 mean, $0.81 p90 | 4 fix attempts |
| One **verified** fix | **$1.12** | all fix spend ÷ 2 verified fixes |
| Planning and decisions per run | $0.0007 | 3 runs |
| One-time setup per repo (code analysis + UX audit) | $1.4 (small app) to $4.9 (Wikipedia) | analysis-run.json, ux-audit.json |
| CI compute per run | about $0.07 | **assumption:** $0.008 per runner minute, 5 min overhead + 8 attacks |

Attacks cost almost nothing; **the cost is in the fixes.** So pricing has to follow fixes, not runs.

The fix sample is small: 4 attempts, 2 verified. These numbers should be refreshed with `droid-monkey econ` as more runs come in.

## Pricing and the fix budget

The founder sets one number, a **70% gross-margin floor**. Each tier's fix budget per run follows from it:

```text
fix budget per run = price ÷ runs per month × (1 − margin floor) − compute − planning
```

| Tier | Price | Runs a month | Fix budget per run | Fixes it covers | Margin if it fixed all expected demand | Margin with the budget (cfo.ai, after Stripe) |
|---|---|---|---|---|---|---|
| Free | $0 | 10, attack-only | $0 | none | n/a | n/a (no revenue) |
| Team | $99 per repo | 30 (nightly) | $0.92 | about 1.6 | 73% | **69.8%** |
| Scale | $499 per repo | 300 (every PR) | $0.42 | about 0.8 | 46% ✗ | **67.1%**, but it covers only about 52% of expected fix demand |
| Usage | $3 per verified fix above the budget | n/a | n/a | n/a | 63% at $1.12 per fix | n/a |

`droid-monkey run --tier team` loads that budget. Once a run has spent it, droid-monkey **keeps attacking** (attacks are almost free) but **stops fixing**, reports the failures it didn't fix, and records each check as a `budget` GUARDRAIL span in neatlogs. A run can never spend past the plan.

## The cfo.ai model

**Model (public, read-only): [cfo.ai/s/dcL7ulePdgkkZNCB](https://cfo.ai/s/dcL7ulePdgkkZNCB)**

Ari, cfo.ai's assistant, built it from [`business/drivers.csv`](../business/drivers.csv) and [`business/inputs.json`](../business/inputs.json), using the prompt in [`business/cfo-prompt.md`](../business/cfo-prompt.md). Measured costs keep their source; assumptions are labelled as such. The model includes a forecast simulator: Scale price, the share of failures approved for fixes, and run usage.

The forecast runs November 2026 to April 2028, in USD:
- $5,000 opening cash and no repos at the start;
- no founder salary and $25 a month fixed costs;
- Stripe at 2.9% + $0.30 per invoice;
- $2.78 average one-time onboarding per new repo.

Gross margins are recurring, after Stripe and before onboarding.

| Scenario | Scale margin (month 18) | Team margin (month 18) | Month-18 MRR | Month-18 revenue | Month-18 cash | Runway |
|---|---|---|---|---|---|---|
| **Base** | 67.1% | 69.8% | $3,812 | $3,812 | $22,618 | cash never goes negative in 18 months |
| Bad release month (July 2027: p90 fix cost, twice the failures) | 67.1% | 69.8% | $3,812 | $3,812 | $22,590 | 18+ months |
| **No budget cap** (fix every failure, no extra billing) | **−5.6%** | 45.0% | $3,812 | $3,812 | **$10,852** | 18+ months |
| Scale repriced ($986.97, covers normal fix demand at 70%) | 70.0% | 69.8% | $5,522 | $5,522 | $29,732 | 18+ months |
| Usage pricing ($3 per verified fix on top of the plan) | 74.0% | 80.7% | $3,812 | $7,484 | $44,764 | 18+ months |
| Growth (higher acquisition) | 67.1% | 69.8% | $9,681 | $9,681 | $62,785 | 18+ months |

**What it says:**
- **The budget cap is what keeps droid-monkey profitable.** Without it, every failure gets a fix attempt, Scale's margin turns negative (−5.6%), Team falls to 45%, and month-18 cash is roughly halved ($22.6k → $10.9k). `run --tier` is the cap: the plan's numbers decide when the agent stops fixing.
- **Scale at $499 holds 67% only by covering about 52% of fix demand.** To cover normal demand at a 70% margin, Scale should cost **$986.97** per repo per month.
- **Usage pricing earns the most:** $7.5k monthly revenue and $44.8k cash by month 18. But only $3.8k of that is subscription, and the rest depends on how many fixes customers approve.
- **A bad release month barely matters** under the cap (−$28 cash), because the cap absorbs the extra fix demand.
- **None of the six scenarios runs out of cash** within the 18 months.

**What this changes in droid-monkey:** Team keeps $99 with its budget. Scale either goes to about $990, or keeps $499 with fewer included fixes plus $3 per verified fix beyond them. `droid-monkey econ` recomputes the budgets from new runs, and `run --tier` enforces them.
