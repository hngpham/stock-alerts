# quote_sources/base.py
from __future__ import annotations
from typing import Protocol, Tuple, Optional, Dict

# Standardized error codes across providers:
# "key_missing" | "network_error" | "rate_limited" | "invalid_symbol" |
# "empty_quote" | "parse_error" | "unconfigured" | "unknown"


class QuoteProvider(Protocol):
    name: str

    def is_ready(self) -> bool: ...

    def get_price_prev_close(
        self, symbol: str
    ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """Returns: (price, prev_close, error_code_or_None)"""

    def get_full(self, symbol: str) -> Dict:
        """
        Unified payload (all providers should adhere to these keys):

        {
          # core intraday/last price data
          "symbol": str,
          "price": Optional[float],
          "prev_close": Optional[float],
          "open": Optional[float],
          "high": Optional[float],
          "low": Optional[float],
          "volume": Optional[int],
          "latest_trading_day": Optional[str],  # e.g. "2025-09-05"

          # derived
          "change": Optional[float],
          "change_percent": Optional[str],     # e.g. "1.23%"

          # fundamentals (optional; blank if provider can’t supply)
          "market_cap": Optional[int],         # in dollars
          "pe_ratio": Optional[float],
          "dividend_yield_percent": Optional[float],     # e.g. 2.73 for 2.73%
          "fifty_two_week_high": Optional[float],
          "fifty_two_week_low": Optional[float],
          "quarterly_dividend_amount": Optional[float],  # approx; may be inferred

          # corporate events (optional)
          "next_earning_day": Optional[str],   # "YYYY-MM-DD" if known/upcoming

          # company meta (optional)
          "description": Optional[str],        # one sentence: industry + business focus

          # bookkeeping
          "source": str,
          "error": Optional[str],
        }
        """
        ...
