"""Alpaca adapter. Constructed only by the CLI, never at import time."""
from datetime import datetime, timedelta
import logging
import time
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, OptionSnapshotRequest, StockBarsRequest, StockLatestTradeRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from bot_ownership import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType, OrderSide, PositionIntent, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOptionContractsRequest, GetOrdersRequest, LimitOrderRequest
from risk import parse_option

NY = ZoneInfo('America/New_York')
ALLOWED_COVERED_CALL_INTENTS = frozenset({'sell_to_open', 'buy_to_close'})
LOG = logging.getLogger('options_covered')


class MarketDataUnavailable(ValueError):
    """A symbol cannot be evaluated safely from the current broker data."""


class AlpacaBroker:
    def __init__(self, key, secret, settings):
        self.settings = settings
        self.trading = TradingClient(key, secret, paper=settings.paper)
        self.stocks = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)
        self.feed = OptionsFeed(settings.option_feed)

    def account(self):
        return self.trading.get_account()

    def clock(self):
        """Read the clock with bounded retry for transient Alpaca 5xx responses.

        A failed clock read is never treated as open or closed. After retries,
        the caller pauses the cycle and tries again on the normal interval.
        """
        delays = (2, 5, 10)
        for attempt, delay in enumerate(delays, start=1):
            try:
                return self.trading.get_clock()
            except Exception as exc:
                if attempt == len(delays):
                    LOG.warning('Alpaca clock unavailable after %d attempts: %s', attempt, exc)
                    raise
                LOG.warning('Alpaca clock attempt %d/%d failed: %s; retrying in %ss',
                            attempt, len(delays), exc, delay)
                time.sleep(delay)

    def positions(self):
        return self.trading.get_all_positions()

    def open_orders(self):
        # Fail closed at the API cap instead of silently missing reserved shares.
        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True))
        if len(orders) >= 500:
            raise RuntimeError('Open order response reached cap; cannot verify collateral')
        return orders

    def order(self, row):
        if row['broker_id']:
            return self.trading.get_order_by_id(row['broker_id'])
        return self.trading.get_order_by_client_id(row['client_id'])

    def cancel(self, order_id):
        return self.trading.cancel_order_by_id(order_id)

    def submit(self, client_id, symbol, qty, intent, price):
        parsed = parse_option(symbol)
        if not parsed or parsed['kind'] != 'C':
            raise ValueError('CoveredCallBot can submit call options only; puts and stock orders are forbidden')
        if intent not in ALLOWED_COVERED_CALL_INTENTS:
            raise ValueError('CoveredCallBot permits only sell_to_open and buy_to_close intents')
        if qty <= 0 or qty != int(qty) or (intent == 'sell_to_open' and qty != 1):
            raise ValueError('CoveredCallBot opens exactly one contract per trade')
        return self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.SELL if intent == 'sell_to_open' else OrderSide.BUY,
            position_intent=PositionIntent(intent), limit_price=price, time_in_force=TimeInForce.DAY,
            client_order_id=client_id))

    def stock_quote(self, symbol, now):
        from risk import quote_prices
        asset = self.trading.get_asset(symbol)
        if (not asset.tradable or str(getattr(asset.asset_class, 'value', asset.asset_class)) != 'us_equity'):
            raise MarketDataUnavailable('Underlying is not a tradable equity/ETF')
        quote = self.stocks.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
        return quote_prices(quote, now, self.settings.quote_age_seconds)

    def submit_stock(self, client_id, symbol, price):
        from math import isfinite
        import re
        if not (self.settings.paper and self.settings.auto_buy_shares
                and self.settings.enable_new_entries and not self.settings.dry_run):
            raise ValueError('Stock acquisition requires enabled paper buy-write mode')
        if (not client_id.startswith('covered_stock_') or not re.fullmatch(r'[A-Z][A-Z.]{0,9}', symbol)
                or parse_option(symbol) or not isfinite(price) or price <= 0):
            raise ValueError('Invalid stock acquisition order')
        return self.trading.submit_order(LimitOrderRequest(
            symbol=symbol, qty=100, side=OrderSide.BUY, type='limit',
            limit_price=price, time_in_force=TimeInForce.DAY,
            extended_hours=False, client_order_id=client_id))

    def option_activities(self, since):
        """SDK has no trading activity wrapper; use its authenticated REST GET.

        Include exercise events too, to detect ambiguous stock-disposition matches
        with another strategy (for example a long put exercised the same day).
        """
        params = {'activity_types': 'OPASN,OPEXP,OPTRD,OPEXC', 'after': since,
                  'direction': 'asc', 'page_size': 100}
        result, seen = [], set()
        while True:
            page = self.trading.get('/account/activities', data=params)
            if not isinstance(page, list):
                raise RuntimeError('Invalid account activity response')
            result.extend(page)
            if len(page) < 100:
                return result
            token = page[-1]['id']
            if token in seen:
                raise RuntimeError('Repeated account activity page')
            seen.add(token)
            params['page_token'] = token

    def previous_session(self, today):
        sessions = self.trading.get_calendar(GetCalendarRequest(start=today - timedelta(days=14),
                                                                end=today - timedelta(days=1)))
        if not sessions:
            raise RuntimeError('No prior trading session found')
        return max(s.date for s in sessions)

    def bars(self, symbol, now):
        result = self.stocks.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=now - timedelta(days=200),
            end=now, feed=DataFeed.IEX))
        bars = result.data.get(symbol, [])
        return pd.DataFrame([{'high': b.high, 'low': b.low, 'close': b.close} for b in bars],
                            index=pd.DatetimeIndex([b.timestamp.astimezone(NY) for b in bars]))

    def spot(self, symbol, now):
        trade = self.stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol]
        if not -5 <= (now - trade.timestamp).total_seconds() <= self.settings.quote_age_seconds:
            raise MarketDataUnavailable('Stale underlying trade')
        if not 0 < float(trade.price) < float('inf'):
            raise MarketDataUnavailable('Invalid underlying price')
        return float(trade.price)

    def contracts(self, symbol, spot, today):
        request = GetOptionContractsRequest(
            underlying_symbols=[symbol], status=AssetStatus.ACTIVE, type=ContractType.CALL,
            expiration_date_gte=today + timedelta(days=self.settings.min_dte),
            expiration_date_lte=today + timedelta(days=self.settings.max_dte),
            strike_price_gte=str(spot), strike_price_lte=str(spot * 1.20), limit=1000)
        contracts, seen = [], set()
        while True:
            result = self.trading.get_option_contracts(request)
            contracts.extend(result.option_contracts or [])
            token = result.next_page_token
            if not token:
                return contracts
            if token in seen:
                raise RuntimeError('Repeated option-contract pagination token')
            seen.add(token)
            request.page_token = token

    def snapshots(self, symbols):
        results = {}
        for offset in range(0, len(symbols), 100):
            results.update(self.options.get_option_snapshot(OptionSnapshotRequest(
                symbol_or_symbols=symbols[offset:offset + 100], feed=self.feed)))
        return results

    def volumes(self, symbols, today):
        results = {}
        for offset in range(0, len(symbols), 100):
            data = self.options.get_option_bars(OptionBarsRequest(
                symbol_or_symbols=symbols[offset:offset + 100], timeframe=TimeFrame.Day,
                start=datetime.combine(today, datetime.min.time(), tzinfo=NY), feed=self.feed))
            results.update({s: sum(b.volume for b in bars) for s, bars in data.data.items()})
        return results
