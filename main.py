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
    args = parser.parse_args()
    settings = Settings.from_env()
    settings.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if args.paper_results:
        import json
        print(json.dumps(Ledger(settings.ledger_path).report(), indent=2))
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
        logging.info('OptionsCovered paper=%s dry_run=%s new_entries=%s', settings.paper,
                     settings.dry_run, settings.enable_new_entries)
        while True:
            try:
                bot.cycle()
                ledger.export_fills(settings.ledger_path.parent / 'trade_analytics.csv')
            except Exception:
                logging.exception('Cycle stopped; retrying reconciliation before any further order')
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(settings.interval)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
