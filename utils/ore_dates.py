"""
utils/ore_dates.py
------------------
Helpers to convert between Python date strings and ORE Date objects.
"""

from __future__ import annotations


def parse_ore_date(date_str: str):
    """
    Convert a "YYYY-MM-DD" string to an ORE/QuantLib Date object.

    Parameters
    ----------
    date_str : e.g. "2026-03-15"

    Returns
    -------
    ORE Date object
    """
    from ORE import (
        Date,
        January, February, March, April, May, June,
        July, August, September, October, November, December,
    )

    _month_map = {
        1: January,  2: February,  3: March,
        4: April,    5: May,       6: June,
        7: July,     8: August,    9: September,
        10: October, 11: November, 12: December,
    }
    y, m, d = [int(x) for x in date_str.split("-")]
    return Date(d, _month_map[m], y)
