"""Local, since-inception performance snapshots shared by the four sibling bots.

This module never places orders, reads credentials, or reads another bot's ledger.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

BOTS = ('options_direct', 'options_inverted', 'options_covered', 'options_secured')


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('Non-finite performance value')
    return value


def long_result(events, prices, capital, quantities=None):
    """All recorded confirmed long-option fills, retaining unresolved positions.

    Caller filters ownership. Missing quotes/unknown inventory never imply zero loss.
    """
    lots, missing = {}, set()
    realized = 0.0
    incomplete = False
    first = None
    for row in events:
        kind = row.get('event')
        key = (row.get('strategy', ''), row.get('underlying', ''), row.get('option_symbol', ''))
        if kind == 'POSITION_MISSING':
            missing.add(key)
        elif kind == 'POSITION_RECONCILED':
            missing.discard(key)
        if kind not in ('ORDER_FILL', 'EXPIRATION_CONFIRMED'):
            continue
        stamp = row.get('timestamp')
        if stamp:
            first = min(first, stamp) if first else stamp
        if row.get('price') in (None, ''):
            incomplete = True
            continue
        qty, price = number(row.get('qty') or 0), number(row['price'])
        if qty <= 0 or price < 0 or not key[2]:
            incomplete = True
            continue
        lot = lots.setdefault(key, [0.0, 0.0])
        if row.get('order_side') == 'buy':
            lot[0] += qty
            lot[1] += qty * price * 100
        elif row.get('order_side') == 'sell':
            if qty > lot[0] + 1e-8:
                incomplete = True
            closed = min(qty, lot[0])
            cost = lot[1] / lot[0] if lot[0] else 0
            realized += closed * (price * 100 - cost)
            lot[0] -= closed
            lot[1] -= closed * cost
            if lot[0] <= 1e-8:
                missing.discard(key)
        else:
            incomplete = True
    unrealized = 0.0
    owned_quantities = {}
    for key, (qty, cost) in lots.items():
        if qty <= 1e-8:
            continue
        owned_quantities[key[2]] = owned_quantities.get(key[2], 0) + qty
        price = prices.get(key[2])
        if price is None or number(price) < 0:
            incomplete = True
        else:
            unrealized += qty * number(price) * 100 - cost
    if quantities is not None:
        incomplete = incomplete or any(number(quantities.get(symbol, 0)) + 1e-8 < qty
                                       for symbol, qty in owned_quantities.items())
    incomplete = incomplete or bool(missing)
    return dict(starting_capital=capital, realized_pnl=realized,
                unrealized_pnl=None if incomplete else unrealized,
                inception=first,
                status='incomplete: missing marks or unresolved history' if incomplete else 'ok')


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, allow_nan=False)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def snapshot(bot, result, previous=None, paper=True, now=None):
    now = now or datetime.now(timezone.utc)
    previous = previous or {}
    capital = number(result['starting_capital'])
    if capital <= 0:
        raise ValueError('Starting capital must be positive')
    status = result.get('status', 'ok')
    realized = number(result['realized_pnl'])
    unrealized = result.get('unrealized_pnl')
    total = None if unrealized is None else realized + number(unrealized)
    if previous.get('starting_capital', capital) != capital:
        status = 'incomplete: starting allocation changed; restore original allocation'
        capital = previous['starting_capital']
        total = None
    inception = previous.get('inception')
    if not inception or inception == 'unknown':
        inception = result.get('inception') or now.isoformat()
    if result.get('inception'):
        inception = min(inception, result['inception'])
    return dict(schema_version=1, bot=bot, paper=paper, as_of=now.isoformat(),
                inception=inception, starting_capital=capital, realized_pnl=realized,
                unrealized_pnl=unrealized, total_pnl=total,
                since_inception_return_pct=None if total is None else total / capital * 100,
                status=status, basis='recorded bot P/L / original virtual allocation; before taxes and unrecorded fees')


def load_snapshot(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def dashboard(root, own, paper=True, now=None):
    now = now or datetime.now(timezone.utc)
    lines = ['SINCE INCEPTION | bot P/L / starting allocation | ' + ('PAPER' if paper else 'LIVE')]
    try:
        overrides = json.loads(os.getenv('PERFORMANCE_REPORT_PATHS', '{}'))
        if not isinstance(overrides, dict):
            raise ValueError('Expected an object')
    except ValueError:
        overrides = {}
        lines[0] += ' | invalid PERFORMANCE_REPORT_PATHS; using default paths'
    for name in BOTS:
        default = Path(root).parent / name / 'logs'
        if not paper:
            default /= 'live'
        try:
            row = own if name == own['bot'] else load_snapshot(overrides.get(name, default / 'since_inception.json'))
            if row is None:
                lines.append(f'{name:<17} N/A | no cycle report yet')
                continue
            if row.get('bot') != name or row.get('paper') != paper:
                raise ValueError('report identity/mode mismatch')
            age = max(0, (now - datetime.fromisoformat(row['as_of'])).total_seconds())
            pct = row['since_inception_return_pct']
            display = 'N/A' if pct is None else f'{number(pct):+.2f}%'
            freshness = f'STALE ({age / 60:.0f}m old)' if age > 180 else 'latest cycle'
            lines.append(f"{name:<17} {display:>9} | since {row['inception'][:10]} | "
                         f"as of {row['as_of']} | {freshness} | {row['status']}")
        except (OSError, ValueError, KeyError, TypeError):
            lines.append(f'{name:<17} N/A | unreadable or invalid cycle report')
    return lines


def publish(root, bot, result_factory, log=print, paper=True):
    """Reporting errors must never stop reconciliation or exit management."""
    root = Path(root)
    path = root / 'logs' / ('' if paper else 'live') / 'since_inception.json'
    try:
        previous = load_snapshot(path)
        try:
            result = result_factory()
            own = snapshot(bot, result, previous, paper)
        except Exception as exc:
            own = dict(previous or {}, bot=bot, paper=paper,
                       as_of=datetime.now(timezone.utc).isoformat(),
                       inception=(previous or {}).get('inception', 'unknown'),
                       since_inception_return_pct=None, total_pnl=None, unrealized_pnl=None,
                       status=f'calculation unavailable: {type(exc).__name__}')
        atomic_json(path, own)
        for line in dashboard(root, own, paper):
            log(line)
    except Exception as exc:
        log(f'SINCE INCEPTION | N/A | reporting unavailable: {type(exc).__name__}')


class completed_cycle:
    """Report before the next pause, including continue/exception paths."""
    def __init__(self, report, pause):
        self.report, self.pause = report, pause

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.report()
        if kind is None:
            self.pause()
        return False
