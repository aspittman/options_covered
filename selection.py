"""Read-only candidate checks; execution guards still recheck before submission."""
import math
from decimal import Decimal, ROUND_HALF_UP


def volume_status(value, minimum):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 'volume_data_unavailable'
    if not math.isfinite(value) or value < 0:
        return 'volume_data_unavailable'
    return 'volume_below_minimum' if value < minimum else ''

