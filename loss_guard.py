from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

def latest_loss_dates(events, short=False):
    """Conservative FIFO loss detection across all strategies in this bot ledger.

    Each losing matched slice starts a shared block, including partial exits.
    This is an entry guard, not a tax-lot/wash-sale accounting calculation.
    """
    inventory = {}
    losses = {}
    for row in events:
        key = (row.get("strategy", ""), row.get("underlying", ""),
               row.get("option_symbol", ""))
        if row.get("event") == "POSITION_MISSING":
            inventory.pop(key, None)
            continue
        if row.get("event") not in {"ORDER_FILL", "ORDER_PARTIAL_FILL"}:
            continue
        qty, price = float(row.get("qty") or 0), float(row.get("price") or 0)
        if qty <= 0:
            continue
        lots = inventory.setdefault(key, deque())
        if row.get("order_side") == ("sell" if short else "buy"):
            lots.append([qty, price])
        elif row.get("order_side") == ("buy" if short else "sell"):
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            day = (stamp.astimezone(ZoneInfo("America/New_York")).date()
                   if stamp.tzinfo else stamp.date())
            while qty > 0 and lots:
                matched = min(qty, lots[0][0])
                if (price > lots[0][1] if short else price < lots[0][1]) and key[1]:
                    losses[key[1]] = max(losses.get(key[1], day), day)
                qty -= matched
                lots[0][0] -= matched
                if lots[0][0] <= 0:
                    lots.popleft()
    return losses



def blocked(underlying, losses, today=None):
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    day = losses.get(underlying)
    return day is not None and 0 <= (today-day).days <= 30
