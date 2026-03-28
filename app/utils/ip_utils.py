from __future__ import annotations
from ipaddress import IPv4Address


def ip_to_int(ip: str | IPv4Address) -> int:
    """Convert an IPv4 address (string or IPv4Address) to an integer for numeric sorting."""
    if isinstance(ip, IPv4Address):
        return int(ip)
    return int(IPv4Address(ip))


def parse_timespan_days(ts: str) -> int:
    """Parse a PowerShell TimeSpan string to days.

    Handles:
      "8.00:00:00"  -> 8   (days.HH:MM:SS)
      "8:00:00"     -> 0   (no days component — treat as 0 days)
    """
    if "." in ts:
        return int(ts.split(".")[0])
    return 0


def parse_timespan_minutes(ts: str) -> int:
    """Parse a PowerShell TimeSpan string to total minutes.

    Handles:
      "1:00:00"  -> 60
      "0:30:00"  -> 30
      "1:30:00"  -> 90
    """
    parts = ts.split(":")
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        return hours * 60 + minutes
    return 0
