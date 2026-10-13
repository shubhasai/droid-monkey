<sub>[Evidence](README.md) · [neatlogs](neatlogs.md) · [Entire](entire.md) · **code graph** · [cfo.ai](cfo-ai.md) · [posts on X](building-in-public.md)</sub>

# 🕸️ Entire's code graph: what the diagnoser's `graph_search` tool returns

> [!TIP]
> **In short:** asked about each of MonkeyMart's 6 bugs in plain words, Entire's code graph put **the right file in the top 3 every time**, and **the bug's own lines in the top 3 for 4 of the 6**, each in under a second.

Real output of droid-monkey's `graph_search` tool ([`codetools.py`](../src/droid_monkey/codetools.py)), the same code the diagnoser is given, run on MonkeyMart `main` at `495eb2b`, which still has all its planted bugs. Each query is the kind of plain-language question the diagnoser asks after a failure. The tool runs `entire graph search --query … --repo <app> --top-k 12 --format json --profile full` (Entire's `graph` plugin, v0.4.0) and keeps only the app's own source files.

## Did it point at the bug?

| Bug | Where the bug is | Entire results | Kept (app source) | Bug's line in the top 3 | Right file in the top 3 |
|---|---|---|---|---|---|
| rotation | `ui/DetailScreen.kt:62–68` | 13 | 2 | ✅ #2 | ✅ #1 |
| process_death | `ui/CheckoutViewModel.kt:14–19` | 11 | 4 | ❌ | ✅ #3 |
| network_loss | `data/OrderRepository.kt:27` | 13 | 6 | ✅ #1 | yes, #1 |
| permission_revoke | `ui/CouponScanner.kt:44` | 14 | 4 | ❌ | ✅ #1 |
| hunt: swallowed exception | `data/DeliveryEstimator.kt:10` | 12 | 6 | ✅ #1 | yes, #1 |
| hunt: deal applied twice | `ui/CheckoutScreen.kt:64` | 14 | 6 | ✅ #1 | yes, #1 |

**4 of 6** queries put the bug's own lines in the top 3, and **6 of 6** put the right file there. Each search took under a second. Where it missed the exact lines, it found a neighbour in the same file: `CheckoutViewModel.placeOrder` instead of the six `mutableStateOf` fields above it, and the camera's `onOpened` callback instead of the `openCamera` call on the line before. For the deal applied twice, the hit is the whole `CheckoutScreen` function (lines 33–89): the right function, not the exact line. That is why the diagnoser gets `graph_search` alongside `read_code` and `search_code`, not instead of them.

## Why the filter matters

Unfiltered, Entire also ranks files that describe the bugs instead of containing them: droid-monkey's own job logs in `.droid-monkey/` (which hold earlier diagnoses) and MonkeyMart's `repro/` scripts. For the process-death query, 7 of Entire's 11 results were such files. The tool drops everything outside the app's source, so a diagnosis has to come from the code, not from notes about it.

## The top results, as the diagnoser sees them

<details>
<summary><b>All 6 queries and their top results</b></summary>

**rotation**: _crashed: IllegalStateException FragmentManager has been destroyed when the deal dialog shows after rotating during the detail load_

| # | Result | Kind |
|---|---|---|
| 1 | `ui/DetailScreen.kt:54-54` DealDialogFragment.newInstance | method |
| 2 | `ui/DetailScreen.kt:61-71` load ⬅️ **the bug** | function |

**process_death**: _checkout form fields lost after the process is killed in the background and restored_

| # | Result | Kind |
|---|---|---|
| 1 | `ui/CheckoutScreen.kt:33-107` CheckoutField | function |
| 2 | `MainActivity.kt:61-85` MonkeyMartApp | function |
| 3 | `ui/CheckoutViewModel.kt:29-35` CheckoutViewModel.placeOrder | method |

**network_loss**: _IOException crashes the app when placing an order with no network_

| # | Result | Kind |
|---|---|---|
| 1 | `data/OrderRepository.kt:24-30` OrderRepository.submit ⬅️ **the bug** | method |
| 2 | `ui/CheckoutViewModel.kt:29-35` CheckoutViewModel.placeOrder | method |
| 3 | `ui/CheckoutScreen.kt:33-89` CheckoutScreen | function |

**permission_revoke**: _SecurityException opening the camera after the camera permission was revoked_

| # | Result | Kind |
|---|---|---|
| 1 | `ui/CouponScanner.kt:45-48` onOpened | function |
| 2 | `ui/CouponScanner.kt:52-55` onError | function |
| 3 | `ui/CouponScanner.kt:50-50` onDisconnected | function |

**hunt: swallowed exception**: _NumberFormatException caught and logged in the delivery estimate for a pincode_

| # | Result | Kind |
|---|---|---|
| 1 | `data/DeliveryEstimator.kt:7-14` days ⬅️ **the bug** | function |
| 2 | `ui/CheckoutScreen.kt:33-89` CheckoutScreen | function |
| 3 | `data/OrderRepository.kt:21-36` OrderRepository | class |

**hunt: deal applied twice**: _checkout total applies the deal discount twice, does not match the detail price_

| # | Result | Kind |
|---|---|---|
| 1 | `ui/CheckoutScreen.kt:33-89` CheckoutScreen ⬅️ **the bug** | function |
| 2 | `ui/CheckoutViewModel.kt:29-35` CheckoutViewModel.placeOrder | method |
| 3 | `ui/DetailScreen.kt:74-114` DetailScreen | function |

</details>

## In the traced runs

`graph_search` is one of the diagnoser's tools, next to `read_code` and `search_code` ([`diagnoser.py`](../src/droid_monkey/diagnoser.py)). The diagnoser picks its tools itself. In the runs traced in neatlogs it chose `read_code` and `search_code` (see the tool performance screenshot in [neatlogs.md](neatlogs.md)), so this file is the record of what the graph returns.

## Run it yourself

```bash
entire plugin install graph
entire graph search --query "IOException crashes the app when placing an order with no network" --repo MonkeyMart --top-k 12 --format json --profile full
```
