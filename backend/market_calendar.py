"""NSE cash-market trading calendar helpers.

Update this file when NSE publishes the next annual holiday circular.
The special Muhurat session is explicitly included even when it falls on Sunday.
"""
from __future__ import annotations

from datetime import date, timedelta

# NSE 2026 equity-market holidays (official annual exchange calendar).
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3),
    date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24),
    date(2026, 12, 25),
}

# Muhurat Trading: special session held on Sunday, 8 November 2026.
NSE_SPECIAL_SESSIONS_2026 = {date(2026, 11, 8)}


def is_nse_trading_session(day: date) -> bool:
    if day in NSE_SPECIAL_SESSIONS_2026:
        return True
    if day in NSE_HOLIDAYS_2026 or day.weekday() >= 5:
        return False
    return True


def next_nse_trading_session(day: date) -> date:
    candidate = day + timedelta(days=1)
    for _ in range(15):
        if is_nse_trading_session(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise RuntimeError("Could not find next NSE trading session in calendar window.")


def nse_session_calendar_note(day: date) -> str | None:
    if day == date(2026, 11, 10):
        return "NSE/BSE equity markets are closed for Diwali-Balipratipada; next regular session is 11-Nov-2026."
    return None
