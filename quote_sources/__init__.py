# quote_sources/__init__.py
from __future__ import annotations
import os
from typing import Optional

# Default provider if not set
os.environ.setdefault("QUOTE_PROVIDER", "alpha_vantage")

from .base import QuoteProvider
from .alpha_vantage import AlphaVantageProvider
from .chatgpt_search_preview_provider import ChatGPTSearchPreviewQuoteProvider
from .gemini_search_provider import GeminiSearchQuoteProvider


def _fallback_provider(primary: QuoteProvider, secondary: QuoteProvider) -> QuoteProvider:
    """
    Composite provider: try `primary`; if it errors or yields no usable numbers,
    try `secondary`. Keeps each provider's own `source` field.
    """
    class FallbackQuoteProvider:
        name = f"{getattr(primary, 'name', 'primary')}_then_{getattr(secondary, 'name', 'secondary')}"

        def is_ready(self) -> bool:
            p_ready = False
            s_ready = False
            try:
                p_ready = bool(primary.is_ready())
            except Exception:
                p_ready = False
            try:
                s_ready = bool(secondary.is_ready())
            except Exception:
                s_ready = False
            return p_ready or s_ready

        def get_price_prev_close(self, symbol):
            try:
                price, prev_close, err = primary.get_price_prev_close(symbol)
            except Exception:
                price, prev_close, err = (None, None, "network_error")

            if err or (price is None and prev_close is None):
                try:
                    price2, prev2, err2 = secondary.get_price_prev_close(symbol)
                except Exception:
                    price2, prev2, err2 = (None, None, "network_error")
                if (price2 is not None or prev2 is not None) or (not err and err2):
                    return price2, prev2, err2
            return price, prev_close, err

        def get_full(self, symbol: str):
            try:
                d = primary.get_full(symbol)
            except Exception:
                d = {
                    "symbol": symbol.upper().strip(),
                    "price": None,
                    "prev_close": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "latest_trading_day": None,
                    "change": None,
                    "change_percent": None,
                    "market_cap": None,
                    "pe_ratio": None,
                    "dividend_yield_percent": None,
                    "fifty_two_week_high": None,
                    "fifty_two_week_low": None,
                    "quarterly_dividend_amount": None,
                    "next_earning_day": None,
                    "description": None,
                    "source": getattr(primary, "name", "primary"),
                    "error": "network_error",
                }

            primary_err = (d or {}).get("error")
            primary_has_data = bool(d and ((d.get("price") is not None) or (d.get("prev_close") is not None)))
            if not primary_err and primary_has_data:
                return d

            try:
                d2 = secondary.get_full(symbol)
            except Exception:
                d2 = {
                    "symbol": symbol.upper().strip(),
                    "price": None,
                    "prev_close": None,
                    "open": None,
                    "high": None,
                    "low": None,
                    "volume": None,
                    "latest_trading_day": None,
                    "change": None,
                    "change_percent": None,
                    "market_cap": None,
                    "pe_ratio": None,
                    "dividend_yield_percent": None,
                    "fifty_two_week_high": None,
                    "fifty_two_week_low": None,
                    "quarterly_dividend_amount": None,
                    "next_earning_day": None,
                    "description": None,
                    "source": getattr(secondary, "name", "secondary"),
                    "error": "network_error",
                }

            secondary_err = (d2 or {}).get("error")
            secondary_has_data = bool(d2 and ((d2.get("price") is not None) or (d2.get("prev_close") is not None)))

            if secondary_has_data and not primary_has_data:
                return d2
            if primary_has_data and not secondary_has_data:
                return d
            if not primary_err and secondary_err:
                return d
            return d2

    return FallbackQuoteProvider()


def get_provider() -> QuoteProvider:
    name = (os.getenv("QUOTE_PROVIDER") or "alpha_vantage").lower().strip()

    if name == "alpha_vantage":
        return AlphaVantageProvider(api_key=os.getenv("ALPHA_VANTAGE_KEY"))

    if name in ("gemini", "google", "gemini_search"):
        # Primary: Gemini; Fallback: Alpha Vantage (if configured)
        gemini_p = GeminiSearchQuoteProvider()
        alpha_s  = AlphaVantageProvider(api_key=os.getenv("ALPHA_VANTAGE_KEY"))
        return _fallback_provider(gemini_p, alpha_s) if alpha_s.is_ready() else gemini_p

    if name in ("chatgpt", "openai"):
        # Primary: OpenAI; Fallback: Alpha Vantage (if configured)
        openai_p = ChatGPTSearchPreviewQuoteProvider()
        alpha_s  = AlphaVantageProvider(api_key=os.getenv("ALPHA_VANTAGE_KEY"))
        return _fallback_provider(openai_p, alpha_s) if alpha_s.is_ready() else openai_p

    # Fallback dummy (no scope leak)
    class _Dummy:
        def __init__(self, provider_name: str):
            self.name = provider_name or "unconfigured"
        def is_ready(self) -> bool:
            return False
        def get_price_prev_close(self, symbol):
            return (None, None, "unconfigured")
        def get_full(self, symbol):
            return {
                "symbol": symbol.upper().strip(),
                "price": None,
                "prev_close": None,
                "open": None,
                "high": None,
                "low": None,
                "volume": None,
                "latest_trading_day": None,
                "change": None,
                "change_percent": None,
                "market_cap": None,
                "pe_ratio": None,
                "dividend_yield_percent": None,
                "fifty_two_week_high": None,
                "fifty_two_week_low": None,
                "quarterly_dividend_amount": None,
                "next_earning_day": None,
                "description": None,
                "source": self.name,
                "error": "unconfigured",
            }

    return _Dummy(name)
