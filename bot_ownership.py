"""Stable trader identity and independent shared-account isolation."""
from pathlib import Path
import sys
from alpaca.trading.client import TradingClient as _AlpacaTradingClient
TRADER_ID = 'options_covered'
# Resolve the shared trading package, never the monitoring application.
_root = next((p for p in Path(__file__).resolve().parents if (p / 'trading_ownership' / '__init__.py').is_file()), None)
if _root is None:
    raise RuntimeError('Trading ownership package is missing; trading refused')
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
from trading_ownership import guarded_client

def TradingClient(*args, **kwargs):
    return guarded_client(_AlpacaTradingClient(*args, **kwargs), TRADER_ID)
