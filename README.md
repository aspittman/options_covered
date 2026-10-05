# OptionsCovered

OptionsCovered can select and buy paper stock lots, then sell covered calls through Alpaca. It runs two styles: **regular**
(the original daily strategy) and **oasis** (intraday EMA-cloud/momentum entries).
Start with your own paper account and disabled entries. This guide is for a fresh
installation; it does not require someone else's `.env`, virtual environment, or logs.

## Automatic paper buy-write workflow

With `AUTO_BUY_SHARES=true`, the bot scans its configured watchlist in order and
selects stock/call pairs using the existing daily indicators, Oasis momentum,
option delta, bid/ask spread, volume, open interest, and dated event checks. It
buys stock only after finding an eligible call. It does not automatically purchase
SLV or buy shares simply because they fit the budget.

1. Check the underlying's daily suitability, market regime, loss cooldown and
   calendar; select a qualifying standard call.
2. Recheck the stock ask/spread and call, available cash, existing broker exposure,
   and the entire 100-share cost against the fixed $25,000 virtual allocation.
3. Reserve the full stock limit cost and persist a unique order ID **before**
   submitting a regular-session, DAY limit order for exactly 100 paper shares.
4. Reconcile confirmed fills into owned allocations at actual cost. Wait for the
   complete lot and broker-confirmed, unreserved coverage before selling a call.
5. Recheck the signal, call chain, calendar, actual share cost and quotes before
   submitting the call. If the setup expires, retain the shares for a later setup.

This is sequential, not an atomic combined order: the stock can fill without a
call subsequently filling. Calls use the existing monitored exits; stock exposure
remains while a call is closed, canceled or expires. **Uncovered full lots are held
and reused for later qualifying calls; the bot does not automatically sell stock.**
Confirmed assignment removes delivered shares and records their actual stock P/L.
Partial canceled stock lots stay marked, budgeted, and flagged `partial_lot_review`;
no call is sold against fewer than 100 shares and no automatic top-up is attempted.
Review partial lots in the paper account and ledger before making manual changes.

Outstanding purchases time out after `ENTRY_TIMEOUT_MINUTES`, or are canceled when
entries/automatic acquisition are disabled, the market closes, a shared loss block
appears, or Oasis reaches its cutoff. Cash remains reserved until cancellation is
confirmed. Unknown submissions are looked up by saved client ID; they are never
blindly retried. The same underlying/signal bar cannot create a second stock order.

The default maximum is two allocated/pending stock lots (`MAX_CONTRACTS`), within
`MAX_COVERED_VALUE`, `MAX_UNDERLYING_VALUE_PER_POSITION` and the fixed virtual budget.
Pending and partial stock fills count toward that budget. Stock purchases use cash,
not margin, with `STOCK_CASH_BUFFER=1000`; `MAX_STOCK_SPREAD=0.005` means a maximum
0.5% bid/ask spread. They cannot merge with an existing broker stock/option position
or order in the same underlying. Existing foreign shares require manual allocation.

Automatic stock acquisition is **paper-only**; `AUTO_BUY_SHARES=true` with
`ALPACA_PAPER=false` is rejected. `DRY_RUN=true` prevents all order submissions and
cancellations, including stock purchases. To run the paper workflow after verifying
setup, use `ALPACA_PAPER=true`, `AUTO_BUY_SHARES=true`, `ENABLE_NEW_ENTRIES=true`, and
`DRY_RUN=false`, then restart. No live stock-order path is added.

### Calendar coverage and retained-share reporting

Maintain local `events.json` from verified issuer/company information. Every
candidate needs `verified_on`, `valid_through`, `earnings`, and `ex_dividend` as in
`events.example.json`. Cover the planned expiration and check events through the
following day; reverify at least every seven days. Unknown data blocks purchases
and call entries. The bot does not fabricate event-free calendars for the watchlist.

The local calendar prepared on September 15, 2026 covers **SLV only**, through
October 31, and needs reverification after September 22. Other symbols require
verified calendar rows before they can be purchased. SLV is in the default universe;
an explicit `UNDERLYINGS` override still controls your own scan list. Calendar
coverage alone is not a buy signal. The example calendar's year-2000 dates remain
intentionally invalid for fresh installations.

All stock intents and incremental fills are stored in the same SQLite ledger as
calls. `stock_lifecycle` in paper results distinguishes `stock_order_pending`,
`partial_lot_review`, `call_order_pending`, `covered`, and `uncovered_reusable`.
`pending_stock_reservation` shows committed but unfilled cash. Lifetime return
includes marked partial/full shares and confirmed stock dispositions as well as
option P/L. Missing stock marks show incomplete performance, not assumed zero loss.
The 20% Oasis stops apply to the **call leg**, not to the stock lot or combined trade.

## 1. Install prerequisites

Use **Python 3.11 or 3.12 (64-bit)** and Git. These are the recommended versions for
the pinned dependencies; newer Python versions may lack compatible package wheels.
You need internet access and an Alpaca paper account with options access and the
market-data permissions used by the bot. Paper fills can differ from real fills;
see [Alpaca paper trading](https://docs.alpaca.markets/us/docs/paper-trading) and
[options access](https://docs.alpaca.markets/us/docs/options-trading).

Choose the instructions for your operating system below. Run commands one line at
a time in the indicated terminal; do not paste the surrounding Markdown fences.

### Windows 10/11 — PowerShell

1. Install Python 3.12 from [python.org](https://www.python.org/downloads/windows/).
   Include the Python launcher and add Python to PATH when offered.
2. Install [Git for Windows](https://git-scm.com/downloads/win), allowing command-line use.
3. Close and reopen PowerShell, then run:

```powershell
py -3.12 --version
git --version
New-Item -ItemType Directory -Force "$HOME\MyBotz"
Set-Location "$HOME\MyBotz"
git clone https://github.com/aspittman/options_covered.git
Set-Location options_covered
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install --upgrade pip
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

If you installed Python 3.11, use `py -3.11` instead. The commands use the virtual
environment's interpreter directly, so no activation or PowerShell execution-policy
change is necessary. For subsequent `python ...` examples in this guide, Windows
users should substitute `.\venv\Scripts\python.exe ...`.

### macOS — Terminal

1. Install Python 3.12 from [python.org](https://www.python.org/downloads/macos/).
   If the installer supplies **Install Certificates.command**, run it to configure
   HTTPS certificates. Do not disable TLS verification to bypass certificate errors.
2. Run `git --version`; if macOS offers Command Line Tools, install them and wait
   for completion. Alternatively install Git from [git-scm.com](https://git-scm.com/downloads/mac).
3. Open Terminal and run:

```bash
python3.12 --version
git --version
mkdir -p ~/MyBotz
cd ~/MyBotz
git clone https://github.com/aspittman/options_covered.git
cd options_covered
python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

In nano, save with **Control+O**, Enter, then exit with **Control+X** (Control, not
Command). Use `python3.11` if that is the supported version you installed.

### Linux — Terminal

Install Git, Python and virtual-environment support through your distribution.
For Ubuntu 24.04 / a Debian-based system providing Python 3.11 or 3.12:

```bash
sudo apt update
sudo apt install git python3 python3-venv python3-pip
python3 --version
git --version
mkdir -p ~/MyBotz
cd ~/MyBotz
git clone https://github.com/aspittman/options_covered.git
cd options_covered
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

Check the printed Python version before creating the environment. If your default
is outside 3.11–3.12, install a supported interpreter through your distribution and
use its explicit executable (for example `python3.12 -m venv venv`). On Fedora,
use `dnf` to install Git and a supported Python version; do not run `apt` commands.
Do not use `sudo pip` or install these dependencies into the system Python.

## 2. Configure your own paper account

1. Sign in to Alpaca, select your **paper trading** account, and generate your own
   paper API key and secret. Paper and live credentials are separate.
2. Edit the `.env` file created above. Keep the other example settings, and set:

```dotenv
APCA_API_KEY_ID=replace_with_your_paper_key
APCA_API_SECRET_KEY=replace_with_your_paper_secret
ALPACA_PAPER=true
ENABLE_NEW_ENTRIES=false
VIRTUAL_STARTING_CAPITAL=25000
LOSS_GUARD_SCOPE=portfolio
OPTION_TRAILING_STOP_PERCENT=0.20
```

3. Save as exactly `.env`, not `.env.txt`. On Windows enable file-name extensions
   in File Explorer if needed. Do not paste keys into code, screenshots, issues,
   chat messages, or commits. `.env`, `venv/`, and `logs/` are local and ignored by Git.
4. Check that your paper account supports this option strategy and has sufficient
   simulated funds. A virtual allocation is a reporting/risk budget, not an Alpaca
   deposit or a separate subaccount. Four $25,000 virtual budgets do not create
   $100,000 of buying power. Shared-account broker limits still apply.
5. Keep a fresh local ledger for your own account. Never copy the repository owner's
   `.env` or `logs/`. Never point a friend’s bot at your loss ledgers or report files.
   If you change brokerage account, use a separate installation and fresh ledger.

### Choose automatic acquisition or existing-share allocation

The example enables `AUTO_BUY_SHARES=true` while keeping entries disabled and dry
run enabled for initial setup. Automatic mode selects an eligible pair, buys the
paper shares and allocates confirmed fills itself; no manual pre-purchase is needed.
Prepare the verified event calendar described above before expecting entries.

For shares you already own, set `AUTO_BUY_SHARES=false` if you only want the original
covered-call mode, and explicitly allocate an unreserved 100-share lot:

```text
python main.py --allocate-shares SYMBOL --share-cost ACTUAL_COST
```

Replace the placeholders with the actual symbol and verified per-share cost for the
specific lot. On Windows use `.\venv\Scripts\python.exe` instead of `python`.
Stop the other Covered instance first. This allocation command places no orders.
Never allocate the same shares to two strategies. A $250 share price requires
$25,000 for 100 shares; budget the entire stock cost rather than the call premium.

The example also sets `DRY_RUN=true`: this blocks **all** submissions and cancellations,
including exits. After verifying setup, change it to `DRY_RUN=false` for paper order
management. Use `ENABLE_NEW_ENTRIES=false` (with `DRY_RUN=false`) when you want to pause
entries while continuing to manage existing positions.

## 3. Verify setup without placing orders

From the repository folder, with the environment active on macOS/Linux:

```bash
python setup_check.py
python setup_check.py --broker
```

Windows:

```powershell
.\venv\Scripts\python.exe setup_check.py
.\venv\Scripts\python.exe setup_check.py --broker
```

The first check validates local configuration and market timezone support. The
second only reads your paper account. Neither creates a trading ledger nor submits
or cancels orders. Confirm both pass and review the reported account status/options
level. This verifies connectivity, not the availability of every data feed or contract.
Existing shell environment variables override `.env`; check them if the printed
mode/settings disagree with the file.

## 4. Start with new entries disabled

Keep `ALPACA_PAPER=true` and `ENABLE_NEW_ENTRIES=false`, then run:

```bash
python main.py
```

Windows:

```powershell
.\venv\Scripts\python.exe main.py
```

Always run from this bot's folder. Use one process per bot and a separate terminal
for each bot. Read the startup log and verify that new entries are disabled.
For a single cycle, use `python main.py --once` (with the Windows interpreter substitution where needed).

A disabled-entry run can still manage existing positions and cancel blocked or stale
orders. It is not a no-order simulation of an existing ledger. Start onboarding with
a fresh paper account/ledger; use the read-only checks above if you only want to
verify credentials. Covered additionally defaults to `DRY_RUN=true`, which prevents exit orders too.

Watch at least one market-open cycle. Rejection/skip messages are normal: no signal,
stale quotes, liquidity, insufficient collateral, or a shared loss cooldown can all
prevent a trade. Do not weaken safeguards just to force activity.

## 5. Enable paper entries deliberately

1. Review your configured capital, contract count, stock coverage/cash collateral,
   and strategy settings. Regular retains its original stops. **Oasis alone** uses
   the 20% fixed option stop and 20% option-premium trail.
2. Stop the process with **Ctrl+C**. Change `.env` to `ENABLE_NEW_ENTRIES=true`.
   Set `AUTO_BUY_SHARES=true` and `DRY_RUN=false` to allow automatic paper stock/call orders.
3. Restart with the same `main.py` command, verify the mode, and monitor the first
   submissions and confirmed fills in both the terminal and Alpaca paper dashboard.
4. To pause new entries, stop, set `ENABLE_NEW_ENTRIES=false`, and restart.
   Stopping the program or letting the computer sleep also stops its monitored exits.
   Ctrl+C does not liquidate positions or guarantee cancellation of broker orders.

This guide does not enable live trading. Stops use monitored limit-order exits and
can fail to fill; their trigger percentages are not guaranteed maximum losses.
Keep the computer awake, connected, and the process running while relying on it
for exits. Inspect broker positions and open orders before shutting down.

## 6. Read the return after every cycle

Each completed cycle ends with a **SINCE INCEPTION** table containing rows for
`options_direct`, `options_inverted`, `options_covered`, and `options_secured`, with
signed percentages, dates, and data status. Both regular and Oasis are included in
each bot's combined result, along with its retained historical variants.

```text
SINCE INCEPTION | bot P/L / starting allocation | PAPER
options_direct       +2.40% | since ... | as of ... | latest cycle | ok
options_inverted     -0.80% | since ... | as of ... | latest cycle | ok
options_covered         N/A | no cycle report yet
options_secured      +0.60% | since ... | as of ... | latest cycle | ok
```

These are illustrative numbers, not results or forecasts. The formula is:

```text
since-inception return (%) = 100 × (recorded realized P/L + marked unrealized P/L)
                                  / original VIRTUAL_STARTING_CAPITAL
```

A $600 combined profit on a $25,000 allocation is +2.40%. This is a nonannualized
bot return, not the whole Alpaca account return or the percentage return on a single
option's premium. It is before taxes and any fees not recorded in the bot ledger.
Covered returns include its allocated shares; secured returns include assigned stock.

“Inception” means the earliest recorded fill/allocation, or the first successful
report for a fresh empty ledger. It is not the repository's creation/clone date.
All retained fills are included regardless of `BOT_PERFORMANCE_START_DATE`, which
may still select a shorter period in older research tables. Missing prices or
unresolved history show **N/A**, not an assumed zero loss. A peer report older than
three minutes is labeled **STALE**; timestamps let you identify a stopped bot.
The bots do not synchronize their scans, so peer rows show their latest cycle.
Direct/inverted produce no completed-cycle table while waiting for market open.

The machine-readable copy is `logs/since_inception.json`, with field
`since_inception_return_pct`. Live-mode snapshots, where supported, are separated
under `logs/live/`; paper dashboards reject live-mode reports. Keep the starting
allocation fixed: changing it causes N/A rather than silently rewriting the return.
Do not delete this report or the underlying ledger to reset performance.
The original trade-history file for this bot is `logs/trades.sqlite3`.
Back up the entire `logs/` folder after stopping the bot; SQLite sidecar files may
be needed. An incomplete/deleted history cannot recreate true lifetime returns.

## 7. Run multiple bots on your computer

Clone any additional repositories beside this one, not inside it:

```text
MyBotz/
  options_direct/
  options_inverted/
  options_covered/
  options_secured/
```

From `MyBotz`, clone each repository you do not already have:

```bash
git clone https://github.com/aspittman/options_direct.git
git clone https://github.com/aspittman/options_inverted.git
git clone https://github.com/aspittman/options_covered.git
git clone https://github.com/aspittman/options_secured.git
```

Follow each README separately: each folder needs its own environment and `.env`.
Keep separate ledgers, even when your bots use the same paper account. Never reuse
someone else's history. Default sibling placement enables both the four-bot display
and your shared loss guard. With `LOSS_GUARD_SCOPE=portfolio`, a recorded loss in any
of these bots blocks that underlying for both variants across all four bots through
calendar day 30; day 31 permits re-entry. Only recorded local activity is covered;
this is not complete tax accounting or a tax-compliance guarantee.

For a custom location, `LOSS_LEDGER_PATHS` controls ledger discovery (see the
[strategy reference](STRATEGY_REFERENCE.md)); it does not configure the display.
`PERFORMANCE_REPORT_PATHS` is an optional JSON object mapping bot names to their
`since_inception.json` files, for example:

```dotenv
PERFORMANCE_REPORT_PATHS={"options_covered":"/path/to/options_covered/logs/since_inception.json"}
```

On Windows use forward slashes in JSON paths, such as `C:/Users/you/MyBotz/...`.
These local files do not automatically synchronize across different computers.
A bot you haven't installed or started appears as N/A.

## 8. Restart, update, and troubleshoot

After opening a new terminal, return to the repository folder. On macOS/Linux run
`source venv/bin/activate` again. On Windows continue using
`.\venv\Scripts\python.exe`. Then run `main.py` as above.

To update: inspect broker exposure first, stop the bot, and back up `.env` and all
of `logs/` privately. Run `git status` and preserve any local code changes, then
`git pull --ff-only` and `python -m pip install -r requirements.txt` (use the Windows
interpreter there). Review new `.env.example` settings without overwriting your
existing `.env`. Restart and verify startup settings. Never delete a ledger to
resolve a startup error or mix an old ledger with a different account.

| Symptom | What to check |
| --- | --- |
| `git` / `py` / `python3.12` not found | Finish installing, reopen the terminal, check PATH and the installed Python version. |
| Clone denied / repository not found | Verify the repository URL and GitHub access; authenticate to GitHub if the repo is private. Alpaca keys are unrelated to GitHub login. |
| `No module named ...` | Use this bot's venv interpreter and rerun `-m pip install -r requirements.txt`. |
| No matching dependency / compiler error | Check Python is 64-bit 3.11/3.12 and pip is current; save the first failing package/error. Do not randomly unpin packages. |
| Missing `.env` / credentials | Run from the repository directory, check `.env.txt`, placeholders, and shell overrides. |
| 401 / 403 / unavailable quotes | Verify paper keys and account/options/data permissions; check network and Alpaca service status. Do not bypass quote checks. |
| `ZoneInfoNotFoundError` | Reinstall requirements in the venv; `tzdata` supplies market timezones on Windows. |
| Already running / locked ledger | Stop the other instance normally; don't delete a lock to bypass an active process. |
| No trades | Read skip/rejection messages, market clock, entry toggle, loss guard and collateral limits. |
| Return N/A / STALE | Check the row's status/as-of timestamp, current prices, reconciliation and whether that sibling bot is running. |

For a code-only regression check, use an isolated test checkout/environment with
fake API credentials and `LOSS_GUARD_SCOPE=bot`; the suite uses mocks and fixtures.
The test command for this repository is `python -m unittest discover -q`. Do not point tests at your
production ledger. This update was tested on Linux; Windows locking is also covered
with a simulated Windows backend, but native Windows/macOS execution is not verified.

Read the [strategy/configuration/research reference](STRATEGY_REFERENCE.md) for
indicator rules, exit behavior, exports, and backtest commands. For environment
background, see [Python's venv documentation](https://docs.python.org/3.12/library/venv.html).
