import argparse
import fcntl
import logging
import time

from analytics import Ledger
from broker import AlpacaBroker
from config import Settings, credentials
from options_trader import CoveredCallBot


def main():
    parser = argparse.ArgumentParser(description='OptionsCovered: Alpaca covered-call bot')
    parser.add_argument('--once', action='store_true', help='Reconcile and scan one cycle')
    parser.add_argument('--paper-results', action='store_true', help='Read local confirmed-fill analytics without API access')
    parser.add_argument('--allocate-shares', metavar='SYMBOL', help='Assign an existing 100-share lot to this strategy; places no orders')
    parser.add_argument('--share-cost', type=float, help='Verified per-share cost basis for the specifically assigned lot')
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    settings.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if args.paper_results:
        import json
        print(json.dumps(Ledger(settings.ledger_path).research_report(settings), indent=2))
        return
    # Same-path lock prevents two processes from racing collateral checks.
    with open(str(settings.ledger_path) + '.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another bot instance is using this ledger')
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                            handlers=[logging.StreamHandler(), logging.FileHandler(settings.ledger_path.parent / 'options_bot.log')])
        broker = AlpacaBroker(*credentials(), settings)
        ledger = Ledger(settings.ledger_path)
        bot = CoveredCallBot(broker, ledger, settings)
        if args.allocate_shares:
            from datetime import datetime, timezone
            from risk import free_contracts, value, number
            if args.share_cost is None:
                parser.error('--allocate-shares requires --share-cost for the specific lot')
            symbol = args.allocate_shares.upper()
            ledger.bind_account(str(value(broker.account(), 'id')), settings.paper)
            positions, orders = broker.positions(), broker.open_orders()
            if free_contracts(symbol, positions, orders) < 1:
                parser.error('No unreserved 100-share broker lot available')
            if any(lot['underlying'] == symbol for lot in ledger.report()['lots'].values()):
                parser.error('Existing legacy call needs manual stock-allocation reconciliation before allocation')
            try:
                ledger.allocate_shares(symbol, 100, args.share_cost,
                                       broker.spot(symbol, datetime.now(timezone.utc)), settings)
            except ValueError as exc:
                parser.error(str(exc))
            print(f'Allocated 100 existing {symbol} shares to covered_call; no broker orders placed.')
            return
        logging.info('OptionsCovered paper=%s dry_run=%s new_entries=%s', settings.paper,
                     settings.dry_run, settings.enable_new_entries)
        while True:
            try:
                bot.cycle()
            except Exception:
                logging.exception('Cycle stopped; retrying reconciliation before any further order')
                if args.once:
                    raise
            finally:
                ledger.export_fills(settings.ledger_path.parent / 'trade_analytics.csv')
                ledger.export_rejections(settings.ledger_path.parent / 'rejected_trades.csv')
            if args.once:
                return
            time.sleep(min(settings.interval, 60))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
