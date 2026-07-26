"""Gateway statistics storage.

Runtime databases live below :mod:`storage.daily`; this package only exposes
the normalized, provider-agnostic statistics boundary.
"""

from storage.statistics import InvocationHandle, StatisticsStore

__all__ = ["InvocationHandle", "StatisticsStore"]
