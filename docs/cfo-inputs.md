# cfo.ai inputs: droid-monkey business plan

The numbers below were measured on droid-monkey's own runs on Oct 12, 2026, from the run receipts and the cost reported for each traced call. Anything not measured is marked **assumption**. The finished plan goes in `docs/cfo-plan.*`.

## The product
droid-monkey is a CLI agent for Android teams. It explores an app on an emulator and attacks it with real-world faults: process death, rotation, network loss, low memory and revoked permissions. It finds the crashes and lost state, fixes them only with the developer's consent, proves each fix by replaying the same attack, and opens a PR with the evidence.

How it is sold: a **CI add-on**, running as a GitHub Action on pull requests or nightly. A hosted console comes later, covering runs across repos, issues and trends.

## Measured cost per unit

| Unit | Model / resource | Measured cost | Measured time |
|---|---|---|---|
| One attack, no fix | emulator only, no model | $0 inference | median 32 s, p90 44 s (74 attacks) |
| Plan + decide, per run | Claude Haiku 5.5 | $0.0003 to $0.0014 | seconds |
| Diagnosis, per failure | Claude Haiku 5.5 | $0.003 (small app) to $0.22 (Wikipedia, large repo) | under 1 min |
| Fix + verify, per failure | Claude Code, Sonnet 5.5 | $0.41 to $0.63 | about 2.4 min, including rebuild and replay |
| Bug hunt in normal use, per app | Claude Haiku 5.5 | $0.0008 | 16 s |
| Code analysis, once per app | Claude Code | $1.23 (small app) / $3.74 (large app) | minutes |
| UX audit, once per app | Claude | $0.18 (small) / $1.17 (large) | minutes |

A typical CI run (one app, 8 attacks, about 2 failures fixed) comes to about **$1.0 to $1.5 inference**. That's 2 × (diagnosis + fix) plus planning; the attacks themselves cost nothing in inference.

## Assumptions to set in cfo.ai (not measured)
- **Emulator compute:** a Linux CI runner with KVM, about $0.008 to $0.016 per minute, depending on the runner size. A run of 8 attacks plus boot and build takes about 10 to 15 minutes, so about $0.10 to $0.25 per run. Check current runner prices.
- **Runs per repo:** 20 to 60 per month (PR-labelled runs plus nightly).
- **Fix rate:** about 25% of attacks fail, and most of those get a fix attempt.
- **Pricing to test:**
  - Free: 1 repo, 20 attack-only runs per month, no fixes.
  - Team: $49 per repo per month, 100 runs, fixes included up to a fair-use limit.
  - Usage: $0.50 per verified fix above the limit.
- **Costs besides inference:** one founder; hosting the console later; GitHub Marketplace fees.
- **Funding:** none, bootstrapped (set it to your situation).

## Prompt for cfo.ai
> Build a 24-month business plan for droid-monkey, a CI add-on for Android teams that finds lifecycle and resilience bugs on an emulator and fixes them with verified PRs. Use these unit costs: $0 inference per attack-only run; per fixed failure, about $0.60 inference (diagnosis + Claude Code fix + replay); one-time per repo, about $1.5 to $5 (code analysis + UX audit); emulator compute about $0.10 to $0.25 per run. Pricing: Free (1 repo, 20 attack-only runs a month), Team $49 per repo per month (100 runs, fixes included), plus $0.50 per verified fix above the limit. Model gross margin per tier, a customer ramp starting from 0, monthly revenue, burn and runway, and the break-even point. Show how sensitive the margin is to the fix rate (10%, 25% and 50% of attacks failing).
