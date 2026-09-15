"""Read-only onboarding check. Never submits/cancels orders or creates a ledger."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--broker', action='store_true', help='Also read the PAPER account using your credentials')
    args = parser.parse_args()
    if not Path('.env').is_file():
        raise SystemExit('Run from the bot folder and create .env from .env.example first.')
    import config
    if hasattr(config, 'Settings'):
        settings = config.Settings.from_env()
        paper = settings.paper
        entries = getattr(settings, 'enable_new_entries', getattr(settings, 'enable_entries', False))
        keys = config.credentials()
    else:
        paper, entries = config.ALPACA_PAPER, config.ENABLE_NEW_ENTRIES
        keys = config.require_alpaca_credentials()
    if not paper or entries:
        raise SystemExit('Initial setup requires ALPACA_PAPER=true and ENABLE_NEW_ENTRIES=false. Check shell overrides too.')
    if any(not key.strip() or key.startswith('your_') for key in keys):
        raise SystemExit('Replace the example credentials with your own PAPER account API keys.')
    from zoneinfo import ZoneInfo
    ZoneInfo('America/New_York')
    print('PASS: paper mode, new entries disabled, credentials present, market timezone available.')
    if hasattr(config, 'Settings') and hasattr(settings, 'dry_run'):
        print(f'DRY_RUN={settings.dry_run}; true disables exit orders as well as entries.')
    if args.broker:
        from alpaca.trading.client import TradingClient
        try:
            account = TradingClient(*keys, paper=True).get_account()
        except Exception as exc:
            raise SystemExit(f'PAPER account read failed ({type(exc).__name__}); check paper keys, network and Alpaca status. No orders placed.') from None
        print(f'PAPER account reachable: status={account.status}; options level={getattr(account, "options_trading_level", "unknown")}')
    print('No orders submitted or canceled. Existing positions still need their normal risk management.')


if __name__ == '__main__':
    main()
