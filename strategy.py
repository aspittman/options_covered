"""Daily sideways signals shared by the runtime and historical simulator."""
import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator


def indicators(bars):
    bars = bars.sort_index().copy()
    if len(bars) < 60:
        return pd.DataFrame()
    close = bars.close
    ma = close.rolling(50).mean()
    result = pd.DataFrame(index=bars.index)
    result['close'] = close
    result['adx'] = ADXIndicator(bars.high, bars.low, close, window=14).adx()
    result['rsi'] = RSIIndicator(close, window=14).rsi()
    result['slope'] = ma / ma.shift(5) - 1
    result['distance'] = close / ma - 1
    return result


def sideways(row, settings):
    values = [row.get(k, float('nan')) for k in ('close', 'adx', 'rsi', 'slope', 'distance')]
    if not all(np.isfinite(v) for v in values) or values[0] <= 0:
        return False
    return (row.adx <= settings.max_adx
            and settings.min_rsi <= row.rsi <= settings.max_rsi
            and abs(row.slope) <= settings.max_ma_slope
            and abs(row.distance) <= settings.max_ma_distance)


def signal(bars, settings, expected_date):
    completed = bars.loc[[stamp.date() <= expected_date for stamp in bars.index]]
    data = indicators(completed)
    if data.empty or data.index[-1].date() != expected_date:
        return {'eligible': False, 'reason': 'missing_or_stale_daily_bars'}
    row = data.iloc[-1]
    return {'eligible': sideways(row, settings), 'date': expected_date.isoformat(),
            'reason': 'sideways' if sideways(row, settings) else 'outside_sideways_regime',
            'indicators': row.to_dict()}
