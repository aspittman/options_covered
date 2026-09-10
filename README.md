# OptionsCovered

An Alpaca covered-call bot adapted from the architecture of
[aspittman/options_direct](https://github.com/aspittman/options_direct), inspected at
commit `66737da21b00b51b25d93cca30388030d76f3a51`.

It keeps the reference's Python CLI, daily signals, five-minute monitoring,
limit orders, restart launcher and fill analytics. The strategy, collateral
checks, order ledger and historical simulator are rewritten for short calls
backed by **shares already owned**. It does not buy shares automatically.

This is an implementation with configurable research defaults, **not an
empirically optimized or profitability-validated strategy**. The reference's
long-call results and premium budgets do not transfer to covered calls.

## Strategy changes

| Behavior | Reference long-call bot | OptionsCovered |
| --- | --- | --- |
| Market | Bullish MA/MACD trend | Sideways SPY and underlying daily signals |
| Entry | Buy to open | Sell to open one call against 100 owned shares |
| Expiration | 60–90 DTE | 30–45 DTE |
| Delta | Around 0.60 | Around 0.25, tolerance 0.10 |
| Strike | Directional long call | Above stock price and, by default, stock cost basis |
| Exit | Sell long option | Buy to close short option |
| Profit target | Rising option value | Buy back at 50% of entry credit |
| Option stop | Falling option value | Buy back at twice entry credit |
| Expiry management | Close by 30 DTE | Attempt close at 7 DTE |
| Sizing | Premium paid | Unreserved shares and covered stock value |

Sideways means ADX(14) ≤ 20, RSI(14) between 40 and 60, absolute five-session
change in the 50-day moving average ≤ 1.5%, and price within 4% of that average.
The same filter applies to SPY unless `MARKET_FILTER=false`. Only bars through
the previous Alpaca trading session are used; a missing latest completed session
blocks entry. An eligible daily bar can produce one entry attempt per underlying,
persisted across restarts. Unlike the reference's fresh trend-cross requirement,
an ongoing sideways regime can produce another call after the prior one closes
and the five **calendar-day** cooldown expires.

The default watchlist is SPY, QQQ, IWM and DIA. Set `UNDERLYINGS` to the shares
you own and want to write calls against. This bot has a separate
`VIRTUAL_STARTING_CAPITAL=25000` allocation. It opens at most
`MAX_CONTRACTS_PER_TRADE=1`, and each 100-share lot must be worth no more than
`MAX_UNDERLYING_VALUE_PER_POSITION=25000`. Aggregate covered exposure is capped
at `MAX_COVERED_VALUE=25000`; neither limit uses the shared Alpaca account's
buying power.

Because four strategies share one Alpaca account, explicitly assign the exact
100-share lot this bot controls. This records a virtual allocation and places no
stock order:

```bash
python main.py --allocate-shares SPY --share-cost 450
```

The command refuses a lot whose current value exceeds $25,000, a lot already
allocated in this ledger, or shares reserved by another account order. The
allocation persists after a restart and remains strategy capital even while no
call is open.

## Setup and first run

Python 3.10+ on Linux/macOS:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp events.example.json events.json
```

Put your Alpaca **paper** API credentials in `.env` using `APCA_API_KEY_ID` and
`APCA_API_SECRET_KEY`. Existing `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` and
`API_KEY`/`SECRET_KEY` environment names are also accepted. Use a paper account
with options approval and at least 100 shares of an allowed underlying.

The initial settings are `ALPACA_PAPER=true`, `DRY_RUN=true`, and
`ENABLE_NEW_ENTRIES=false`. Run one read-only account cycle:

```bash
python main.py --once
```

To preview entry decisions, set `ENABLE_NEW_ENTRIES=true` and leave
`DRY_RUN=true`. To submit paper orders after reviewing the setup, set
`DRY_RUN=false` while keeping `ALPACA_PAPER=true`. Then run:

```bash
python main.py
# Optional restart supervisor:
python launcher.py
```

`ENABLE_NEW_ENTRIES=false` continues managing existing bot calls when dry run
is off. `DRY_RUN=true` prevents **all** submissions and cancellations, including
exits. The implementation supports `ALPACA_PAPER=false`, but live execution has
not been tested. No account orders were submitted during development.

## Event calendar and market data

Before opening calls, populate `events.json` for each underlying with
`verified_on`, `valid_through`, `earnings` and `ex_dividend`. The example is
deliberately out of date. Supply actual verified ISO dates; empty event lists
mean you verified that no events occur in the covered interval.

Verification must be no more than seven days old, and coverage must extend
through the selected expiration. Earnings or ex-dividend dates from today
through expiration plus one day block that contract. Missing or stale calendars
block new calls, and the log identifies the skipped contract-selection stage.
Existing-call exits do not depend on this file. Refresh the calendar weekly
and after any announced change. There is no automatic corporate-event provider.

Stocks use Alpaca IEX daily bars and latest trades. Options use `indicative` by
default; `OPTION_FEED=opra` requires the appropriate data entitlement. Quotes
and stock trades must be at most 120 seconds old. Indicative quotes and Greeks
may differ from executable market data; missing data blocks entries.

Contracts must have standard 100-share size, a positive bid, sufficient daily
volume and open interest, a narrow spread and the configured delta. Minimum
credit is $0.20 per share, and minimum credit/stock-price ratio is 0.2% for the
entire call term, not an annualized yield. Entries use midpoint day-limit orders
rounded down to conservative nickel/dime ticks. Exits use ask-priced limits
rounded up. Neither guarantees a fill.

## Order safety and recovery

The bot counts account-wide short calls, unfilled sell-call quantities and open
stock sales when checking collateral. Pending stock purchases and pending call
buybacks never count as freed shares. It also honors `qty_available=0`, blocks
unsupported adjusted symbols and open multi-leg orders, and rechecks before
submission. Existing option exposure on the underlying blocks new entries;
external positions are never automatically adopted or closed.

Keep a dedicated account, or coordinate stock sales and option trades with other
tools: account reads and order submission are not an atomic transaction. The
broker's final collateral check remains necessary. A local process lock prevents
two instances using the same ledger; different ledgers/machines are not locked
together. Backing shares are never sold by this bot. A detected collateral
shortfall triggers an attempt to buy back its call.

`logs/trades.sqlite3` records intent **before** submission, binds to the account
and paper/live environment, and tracks cumulative partial fills without counting
them twice. A timeout retains the client order ID for broker lookup. Uncertain
orders pause further submissions. Cancellation is not treated as confirmation:
reservations remain until the broker reports a terminal status. Stale entries
request cancellation after 15 minutes; stale exits after two minutes and retry
with a fresh ask in a subsequent cycle after cancellation is confirmed.

Assignment, expiration and external trading can change broker holdings. If a
tracked position disappears or its quantity differs, entries pause and the bot
logs `POSITION_MISMATCH`. It does not invent a closing fill or realized profit.
Other consistent tracked positions remain eligible for exit management. This
version requires manual reconciliation of assignment/expiration against broker
activity; it does not automatically book the stock disposition or restart the
covered-call cycle after assignment. Preserve the ledger and inspect the broker
activity before changing state. Never delete a ledger to work around an
unresolved order. A `submission_unknown` that the broker cannot resolve also
requires manual investigation.

Every qualified opportunity rejected after signal validation is written to
`logs/rejected_trades.csv` and the SQLite `rejected_trades` table. Records include
contract, quote, spread, DTE, required capital, available virtual capital,
signal date and a reason such as `UNDERLYING_VALUE_OVER_LIMIT`,
`MAX_STRATEGY_EXPOSURE_REACHED`, `INSUFFICIENT_LIQUIDITY`, or
`SPREAD_TOO_WIDE`. Repeated five-minute scans are deduplicated by signal,
underlying, contract and reason.

## Analytics and testing

```bash
python main.py --paper-results
python backtester.py --paper-results
python3 -m unittest -v
```

Reports use confirmed opening credit minus confirmed closing debit, multiplied
by 100. Open-call P/L uses current asks when available. Missing marks are listed
explicitly. Local `--paper-results` does not fetch quotes. The CSV
`logs/trade_analytics.csv` exports incremental confirmed fills; SQLite is the
authoritative ledger. These are **option-only** realized/unrealized figures,
excluding fees, dividends and stock gains/losses, not total covered-call returns.
`main.py --paper-results` also reports allocated-share cost, realized and
unrealized stock P/L, combined return on the $25,000 virtual allocation,
capital employed, assignment/expiration/buy-to-close outcomes, and missing
marks. The report does not infer assignment from a missing position: Alpaca
`OPASN`/`OPEXP` activity and, for assignment, its exact matching stock trade are
required. Alpaca documents `OPASN` as assignment and `OPEXP` as expiration
activity in its account activity feed.

For historical research, provide one underlying's daily CSV containing
`date,open,high,low,close` with consistent prices:

```bash
python backtester.py --csv data/SPY.csv --virtual-capital 25000
```

The simulator buys 100 shares at the first bar's open, uses yesterday's signal
for today's opening call, estimates volatility from previous daily returns, and
models option prices with Black–Scholes. It reports stock-plus-option equity,
drawdown and a 100-share buy-and-hold benchmark, with a modeled spread and
per-contract fees. Final open calls are marked closed at the last bar's ask.
Output goes to `logs/covered_call_backtest_trades.csv` and
`logs/covered_call_equity_curve.csv`.

Use `--virtual-capital 10000`, `25000`, or `50000` to compare allocation
scenarios. The summary reports qualified signals, executed entries, and signals
rejected solely by the underlying capital ceiling. `--starting-cash` remains
available for a separate cash account assumption; by default the simulator uses
the virtual allocation.

This is a synthetic single-underlying research tool, not historical option-chain
execution or a reproduction of every runtime filter. It omits market-wide
filtering, option liquidity, event calendars, actual listed strike grids,
dividends, intraday stops, early exercise and portfolio-wide allocation.
It holds entry volatility fixed while marking each call. It cannot establish
achievable returns or rank settings reliably without real option quotes and
out-of-sample validation. The reference's multi-year download and actual-option
repricing CLI modes have not been ported.

## Strategy limits and sources

Covered calls retain substantial stock downside and cap gains above the strike.
A call profit target or buyback stop does not stop a loss on the stock. Assignment
can occur before expiration, including before an attempted seven-DTE exit.
Buying back an expensive call can also lose money even when the stock rises.

See [Alpaca's covered-call order requirements](https://docs.alpaca.markets/us/docs/options-orders),
[Alpaca order intents](https://alpaca.markets/sdks/python/api_reference/trading/enums.html),
and [OIC's covered-call payoff and assignment explanation](https://www.optionseducation.org/strategies/all-strategies/covered-call-buy-write).
