"""Configuration for the covered-call adaptation of OptionsDirect."""
import os
from math import isfinite
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')


def flag(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {'true', 'false', '1', '0', 'yes', 'no'}:
        raise ValueError(f'{name} must be true or false')
    return value in {'true', '1', 'yes'}


# Named constants make the research allocation auditable and easy for reports
# and sibling bot implementations to import without reading Alpaca account data.
VIRTUAL_STARTING_CAPITAL = 25000
MAX_CONTRACTS_PER_TRADE = 1
MAX_UNDERLYING_VALUE_PER_POSITION = 25000


@dataclass(frozen=True)
class Settings:
    paper: bool = True
    dry_run: bool = True
    enable_new_entries: bool = False
    underlyings: tuple = ('SPY', 'QQQ', 'IWM', 'DIA')
    market_symbol: str = 'SPY'
    market_filter: bool = True
    min_dte: int = 30
    max_dte: int = 45
    exit_dte: int = 7
    target_delta: float = .25
    delta_tolerance: float = .10
    max_spread: float = .10
    min_open_interest: int = 500
    min_volume: int = 100
    min_credit: float = .20
    min_yield: float = .002
    max_contracts: int = 2
    # Fixed strategy allocation, never derived from shared-account buying power.
    virtual_starting_capital: float = VIRTUAL_STARTING_CAPITAL
    max_contracts_per_trade: int = MAX_CONTRACTS_PER_TRADE
    max_underlying_value_per_position: float = MAX_UNDERLYING_VALUE_PER_POSITION
    # Aggregate covered stock exposure cannot exceed this strategy's virtual allocation.
    max_covered_value: float = 25000
    above_cost_basis: bool = True
    take_profit: float = .50
    stop_multiple: float = 2.0
    max_adx: float = 20
    max_ma_slope: float = .015
    max_ma_distance: float = .04
    min_rsi: float = 40
    max_rsi: float = 60
    quote_age_seconds: int = 120
    entry_timeout_minutes: int = 15
    exit_timeout_minutes: int = 2
    cooldown_days: int = 5
    interval: int = 300
    option_feed: str = 'indicative'
    ledger_path: Path = ROOT / 'logs/trades.sqlite3'
    events_path: Path = ROOT / 'events.json'

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            item = getattr(self, name)
            if isinstance(item, (float, int)) and not isfinite(item):
                raise ValueError(f'{name} must be finite')
        if self.max_contracts_per_trade != 1:
            raise ValueError('MAX_CONTRACTS_PER_TRADE must remain 1 for this research strategy')
        if self.virtual_starting_capital <= 0 or self.max_underlying_value_per_position <= 0:
            raise ValueError('Virtual capital and underlying position limit must be positive')
        if not 0 <= self.exit_dte < self.min_dte <= self.max_dte:
            raise ValueError(
                f'Invalid expiration settings: EXIT_DTE={self.exit_dte}, '
                f'MIN_DTE={self.min_dte}, MAX_DTE={self.max_dte}. '
                'Require 0 <= EXIT_DTE < MIN_DTE <= MAX_DTE. '
                'Covered-call defaults are EXIT_DTE=7, MIN_DTE=30, MAX_DTE=45. '
                'Check .env and exported environment variables (exports take precedence).'
            )
        for name in ('max_contracts', 'max_covered_value',
                     'quote_age_seconds', 'interval', 'entry_timeout_minutes',
                     'exit_timeout_minutes', 'min_credit', 'max_adx'):
            if getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        for name in ('target_delta', 'delta_tolerance', 'max_spread', 'min_yield',
                     'take_profit', 'max_ma_slope', 'max_ma_distance'):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f'{name} must be between zero and one')
        if not 0 <= self.min_rsi < self.max_rsi <= 100 or self.stop_multiple <= 1:
            raise ValueError('Invalid RSI range or stop multiple')
        if min(self.min_open_interest, self.min_volume, self.cooldown_days) < 0:
            raise ValueError('Liquidity and cooldown settings cannot be negative')
        if self.option_feed not in {'indicative', 'opra'}:
            raise ValueError('OPTION_FEED must be indicative or opra')

    @classmethod
    def from_env(cls):
        defaults = cls()
        values = {}
        aliases = {'paper': 'ALPACA_PAPER'}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            key = aliases.get(name, name.upper())
            if key not in os.environ:
                continue
            raw = os.environ[key]
            if isinstance(default, bool):
                values[name] = flag(key)
            elif isinstance(default, tuple):
                values[name] = tuple(s.strip().upper() for s in raw.split(',') if s.strip())
            elif isinstance(default, Path):
                values[name] = Path(raw).expanduser().resolve()
            else:
                values[name] = type(default)(raw)
        return cls(**values)


def credentials():
    def first(names):
        return next((os.environ[n].strip() for n in names if os.getenv(n, '').strip()), None)
    key = first(('APCA_API_KEY_ID', 'ALPACA_API_KEY', 'API_KEY'))
    secret = first(('APCA_API_SECRET_KEY', 'ALPACA_SECRET_KEY', 'SECRET_KEY'))
    if not key or not secret:
        raise RuntimeError('Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env')
    return key, secret
