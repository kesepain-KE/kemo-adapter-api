"""Fixed reference rates for display conversion; billing records keep their currency."""
from __future__ import annotations

from collections.abc import Mapping


EXCHANGE_RATE_DATE = "2026-11-06"
# ECB reference rates, currency units per EUR, captured on the date above.
# https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml
EXCHANGE_RATES = {
    "USD": 1.1165,
    "CNY": 7.4485,
    "EUR": 1.0,
    "GBP": 0.83415,
    "JPY": 176.77,
    "HKD": 8.7505,
    "AUD": 1.6250,
    "CAD": 1.5705,
    "CHF": 0.9143,
    "SGD": 1.4309,
    "KRW": 1517.02,
    "NZD": 2.0141,
}


def display_cost(costs: Mapping[str, float], currency: str, *, approximate: bool = False) -> dict:
    unavailable = sorted(costs.keys() - EXCHANGE_RATES.keys())
    amount = None
    if costs and not unavailable:
        amount = sum(
            value if source == currency else value * EXCHANGE_RATES[currency] / EXCHANGE_RATES[source]
            for source, value in costs.items()
        )
    return {
        "amount": amount,
        "currency": currency,
        "approximate": approximate or any(source != currency for source in costs),
        "unconverted_currencies": unavailable,
    }
