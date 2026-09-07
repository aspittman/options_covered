"""Single-underlying covered-call research with explicitly synthetic option prices."""
import argparse
import json
from math import exp, log, sqrt
from pathlib import Path
from statistics import NormalDist

import pandas as pd

from config import Settings
from risk import exit_reason
from strategy import indicators, sideways

NORMAL = NormalDist()


def call_price(spot, strike, dte, volatility, rate=.04):
    if dte <= 0:
        return max(0, spot - strike)
    t = dte / 365
    sigma = max(volatility, .01)
    d1 = (log(spot / strike) + (rate + sigma * sigma / 2) * t) / (sigma * sqrt(t))
    d2 = d1 - sigma * sqrt(t)
    return max(0, spot * NORMAL.cdf(d1) - strike * exp(-rate * t) * NORMAL.cdf(d2))


def simulate(bars, settings, starting_cash, spread=.10, fee=.65):
    """Buy 100 shares once; write calls at next open after eligible daily signals.

    Flat 50% profit and 2x-credit stop rules match runtime. Closing liability is
    modeled at ask, opening credit at bid; never report premiums as total profit.
    No future information is used to select strikes or estimate volatility.
    """
    bars = bars.sort_index()
    if len(bars) < 61 or bars.index.has_duplicates:
        raise ValueError('Need at least 61 unique daily bars')
    for col in ('open', 'high', 'low', 'close'):
        if bars[col].isna().any() or not bars[col].between(.000001, 1e12).all():
            raise ValueError('OHLC prices must be finite and positive')
    if ((bars.high < bars[['open', 'close', 'low']].max(axis=1)) |
            (bars.low > bars[['open', 'close', 'high']].min(axis=1))).any():
        raise ValueError('Invalid OHLC range')
    if starting_cash < 100 * bars.open.iloc[0] or not 0 <= spread < 1 or fee < 0:
        raise ValueError('Insufficient starting cash or invalid transaction costs')
    data = indicators(bars)
    volatility = bars.close.pct_change().rolling(20).std() * sqrt(252)
    stock_basis = float(bars.open.iloc[0])
    cash = starting_cash - stock_basis * 100
    benchmark_cash = cash
    shares, position, cooldown = 100, None, None
    trades, equity = [], []
    for i, (stamp, bar) in enumerate(bars.iterrows()):
        day = stamp.date()
        if (position is None and shares >= 100 and i >= 60 and i < len(bars) - 1
                and (cooldown is None or (day - cooldown).days >= settings.cooldown_days)
                and sideways(data.iloc[i - 1], settings)):
            sigma = float(volatility.iloc[i - 1])
            if pd.notna(sigma) and sigma > 0:
                dte = round((settings.min_dte + settings.max_dte) / 2)
                t = dte / 365
                strike = float(bar.open) * exp((.04 + sigma ** 2 / 2) * t
                                                - NORMAL.inv_cdf(settings.target_delta) * sigma * sqrt(t))
                strike = max(strike, stock_basis if settings.above_cost_basis else 0)
                credit = call_price(bar.open, strike, dte, sigma) * (1 - spread / 2)
                if (strike > bar.open and credit >= settings.min_credit
                        and credit / bar.open >= settings.min_yield
                        and bar.open * 100 <= settings.max_covered_value):
                    position = {'entry_date': day.isoformat(), 'expiration': day + pd.Timedelta(days=dte),
                                'strike': strike, 'credit': credit, 'sigma': sigma}
                    cash += credit * 100 - fee
        liability = 0
        if position:
            dte = (position['expiration'] - day).days
            ask = call_price(bar.close, position['strike'], dte, position['sigma']) * (1 + spread / 2)
            reason = exit_reason(position['credit'], ask, dte, settings)
            if i == len(bars) - 1:
                reason = 'end_of_data'
            if reason:
                if dte <= 0 and bar.close > position['strike']:
                    # A data gap can jump past expiry. Model assignment explicitly.
                    cash += position['strike'] * shares
                    shares = 0
                    option_pnl = position['credit'] * 100 - fee
                    reason = 'modeled_assignment_after_data_gap'
                else:
                    cash -= ask * 100 + fee
                    option_pnl = (position['credit'] - ask) * 100 - fee * 2
                trades.append({**position, 'exit_date': day.isoformat(), 'exit_ask': ask,
                               'reason': reason, 'option_pnl': option_pnl})
                position, cooldown = None, day
            else:
                liability = ask * 100
        equity.append({'date': day.isoformat(), 'equity': cash + shares * bar.close - liability,
                       'buy_hold_equity': benchmark_cash + 100 * bar.close,
                       'stock_value': shares * bar.close, 'cash': cash, 'call_liability': liability})
    curve = pd.DataFrame(equity)
    peak = curve.equity.cummax().clip(lower=starting_cash)
    summary = {'pricing': 'SYNTHETIC Black-Scholes; not historical option fills',
               'starting_equity': starting_cash, 'ending_equity': float(curve.equity.iloc[-1]),
               'total_pnl': float(curve.equity.iloc[-1] - starting_cash),
               'buy_hold_pnl': float(curve.buy_hold_equity.iloc[-1] - starting_cash),
               'option_pnl': sum(t['option_pnl'] for t in trades), 'closed_calls': len(trades),
               'max_drawdown_pct': float(((peak - curve.equity) / peak).max() * 100)}
    return summary, pd.DataFrame(trades), curve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--csv', type=Path, help='One underlying: date,open,high,low,close daily CSV')
    parser.add_argument('--starting-cash', type=float, default=100000)
    parser.add_argument('--spread', type=float, default=.10, help='Synthetic full bid/ask spread fraction')
    parser.add_argument('--fee', type=float, default=.65, help='Modeled fee per contract per side')
    parser.add_argument('--output', type=Path, default=Path('logs'))
    parser.add_argument('--paper-results', action='store_true')
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.paper_results:
        from analytics import Ledger
        print(json.dumps(Ledger(settings.ledger_path).report(), indent=2))
        return
    if args.csv is None:
        parser.error('--csv is required for a historical simulation')
    bars = pd.read_csv(args.csv, parse_dates=['date']).set_index('date')
    summary, trades, curve = simulate(bars, settings, args.starting_cash, args.spread, args.fee)
    args.output.mkdir(parents=True, exist_ok=True)
    trades.to_csv(args.output / 'covered_call_backtest_trades.csv', index=False)
    curve.to_csv(args.output / 'covered_call_equity_curve.csv', index=False)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
