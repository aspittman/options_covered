"""Explicit, dated earnings and ex-dividend calendar; missing coverage blocks entry."""
import json
from datetime import date, timedelta


def event_block(path, symbol, today, expiration):
    try:
        calendar = json.loads(path.read_text())
        row = calendar[symbol]
        if not date.fromisoformat(row['verified_on']) <= today <= date.fromisoformat(row['valid_through']):
            return 'event_calendar_stale'
        if (today - date.fromisoformat(row['verified_on'])).days > 7:
            return 'event_calendar_verification_older_than_7_days'
        if date.fromisoformat(row['valid_through']) < expiration:
            return 'event_calendar_does_not_cover_expiration'
        for kind in ('earnings', 'ex_dividend'):
            for stamp in row[kind]:
                day = date.fromisoformat(stamp)
                if today <= day <= expiration + timedelta(days=1):
                    return f'{kind}_before_expiration'
    except (OSError, KeyError, ValueError, TypeError):
        return 'event_calendar_unavailable'
    return ''
