"""Pure account-wide collateral, contract and quote validation."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import floor, isfinite
import re


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def enum_value(obj):
    return str(getattr(obj, 'value', obj))


def number(raw):
    result = float(raw)
    if not isfinite(result):
        raise ValueError('Nonfinite broker value')
    return result


def parse_option(symbol):
    match = re.fullmatch(r'([A-Z.]+)(\d{6})([CP])(\d{8})', symbol or '')
    if not match:
        return None
    root, expiry, kind, strike = match.groups()
    try:
        return {'underlying': root, 'expiration': datetime.strptime(expiry, '%y%m%d').date(),
                'kind': kind, 'strike': int(strike) / 1000}
    except ValueError:
        return None


def free_contracts(underlying, positions, orders):
    """Never net long options or pending closes against short-call collateral."""
    shares = 0
    reserved = 0
    available = None
    for p in positions:
        symbol = value(p, 'symbol')
        qty = number(value(p, 'qty'))
        if symbol == underlying:
            shares = max(0, qty)
            raw = value(p, 'qty_available')
            available = max(0, number(raw)) if raw is not None else None
        parsed = parse_option(symbol)
        if enum_value(value(p, 'asset_class', '')) == 'us_option' and not parsed:
            raise ValueError('Unsupported option symbol; collateral cannot be proven')
        if parsed and parsed['underlying'] == underlying and parsed['kind'] == 'C' and qty < 0:
            reserved += abs(qty) * 100
    for o in orders:
        if value(o, 'legs'):
            raise ValueError('Open multi-leg order; collateral cannot be proven')
        symbol = value(o, 'symbol')
        remaining = max(0, number(value(o, 'qty')) - number(value(o, 'filled_qty', 0)))
        parsed = parse_option(symbol)
        if enum_value(value(o, 'asset_class', '')) == 'us_option' and not parsed:
            raise ValueError('Unsupported option order')
        if enum_value(value(o, 'side')) == 'sell':
            if symbol == underlying:
                reserved += remaining
            elif parsed and parsed['underlying'] == underlying and parsed['kind'] == 'C':
                reserved += remaining * 100
    free = max(0, shares - reserved)
    if available is not None:
        free = min(free, available)
    return floor(free / 100)


def quote_prices(quote, now, max_age):
    if quote is None:
        raise ValueError('Missing quote')
    bid, ask = number(value(quote, 'bid_price')), number(value(quote, 'ask_price'))
    stamp = value(quote, 'timestamp')
    if isinstance(stamp, str):
        stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if stamp is None or stamp.tzinfo is None:
        raise ValueError('Missing timezone-aware quote timestamp')
    age = (now.astimezone(timezone.utc) - stamp).total_seconds()
    if not -5 <= age <= max_age or bid < 0 or ask <= 0 or ask < bid:
        raise ValueError('Stale or invalid quote')
    return bid, ask


def limit_price(price, closing=False):
    # Conservative nickel/dime ticks also work for penny-eligible contracts.
    tick = Decimal('.05') if price < 3 else Decimal('.10')
    mode = ROUND_CEILING if closing else ROUND_FLOOR
    return float((Decimal(str(price)) / tick).to_integral_value(rounding=mode) * tick)


def candidate_score(contract, snapshot, volume, spot, cost_basis, today, now, settings):
    parsed = parse_option(value(contract, 'symbol'))
    if not parsed or parsed['kind'] != 'C':
        return None
    if (not value(contract, 'tradable', False) or number(value(contract, 'size', 0)) != 100
            or value(contract, 'underlying_symbol') != parsed['underlying']
            or enum_value(value(contract, 'type')) != 'call'):
        return None
    strike = number(value(contract, 'strike_price'))
    dte = (parsed['expiration'] - today).days
    if not settings.min_dte <= dte <= settings.max_dte or strike <= spot:
        return None
    if settings.above_cost_basis and (cost_basis <= 0 or strike < cost_basis):
        return None
    if number(value(contract, 'open_interest') or 0) < settings.min_open_interest or volume < settings.min_volume:
        return None
    try:
        bid, ask = quote_prices(value(snapshot, 'latest_quote'), now, settings.quote_age_seconds)
        delta = number(value(value(snapshot, 'greeks'), 'delta'))
    except (ValueError, TypeError):
        return None
    mid = (bid + ask) / 2
    credit = limit_price(mid)
    if (bid <= 0 or (ask - bid) / mid > settings.max_spread
            or not 0 < delta < 1 or abs(delta - settings.target_delta) > settings.delta_tolerance
            or credit < settings.min_credit or credit / spot < settings.min_yield):
        return None
    return (abs(delta - settings.target_delta), (ask - bid) / mid, -volume, abs(dte - 35))


def exit_reason(entry_credit, ask, dte, settings):
    if dte <= settings.exit_dte:
        return 'expiration_management'
    if ask <= entry_credit * (1 - settings.take_profit):
        return 'premium_profit_target'
    if ask >= entry_credit * settings.stop_multiple:
        return 'short_call_stop'
    return ''
