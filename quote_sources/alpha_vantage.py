# quote_sources/alpha_vantage.py
from __future__ import annotations
import os
import re
import requests
from typing import Optional, Tuple, Dict

ALPHA_URL = "https://www.alphavantage.co/query"

def _f(x):
    """Coerce to float or None (handles '', None, 'None')."""
    try:
        return None if x in (None, "", "None") else float(x)
    except Exception:
        return None

def _i(x):
    """Coerce to int or None via float first (handles '', None, 'None')."""
    try:
        return None if x in (None, "", "None") else int(float(x))
    except Exception:
        return None

class AlphaVantageProvider:
    """
    Alpha Vantage quote provider.

    Rules:
      - Never invent values. If a field is unavailable, leave it as None.
      - Do not substitute prev_close=price when prev_close is missing.
      - Only derive change / change_percent when both price and prev_close exist.
    """
    name = "alpha_vantage"

    def __init__(self, api_key: Optional[str], timeout: int = 12):
        self.api_key = api_key
        self.timeout = timeout

    def is_ready(self) -> bool:
        return bool(self.api_key)

    # ---- internal helpers ----
    def _get(self, params: Dict) -> Tuple[Optional[dict], Optional[str]]:
        if not self.api_key:
            return None, "key_missing"
        try:
            r = requests.get(ALPHA_URL, params={**params, "apikey": self.api_key}, timeout=self.timeout)
            r.raise_for_status()
            j = r.json()
        except Exception:
            return None, "network_error"

        if isinstance(j, dict) and "Note" in j:
            return None, "rate_limited"
        if isinstance(j, dict) and "Error Message" in j:
            return None, "invalid_symbol"
        return j, None

    def _global_quote(self, symbol: str) -> Tuple[Optional[dict], Optional[str]]:
        j, err = self._get({"function": "GLOBAL_QUOTE", "symbol": symbol})
        if err:
            return None, err
        q = (j or {}).get("Global Quote", {}) if isinstance(j, dict) else {}
        if not q or ("05. price" not in q and "08. previous close" not in q):
            # treat as empty if neither price nor prev_close is present
            return None, "empty_quote"
        return q, None

    def _overview(self, symbol: str) -> Tuple[Optional[dict], Optional[str]]:
        j, err = self._get({"function": "OVERVIEW", "symbol": symbol})
        if err:
            return None, err
        return j or {}, None

    @staticmethod
    def _first_sentence(text: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        parts = re.split(r"(?<=[.!?])\s+", t, maxsplit=1)
        first = parts[0].strip()
        if first and first[-1] not in ".!?":
            first += "."
        return first

    @staticmethod
    def _compose_short_description(ov: dict) -> Optional[str]:
        if not isinstance(ov, dict):
            return None
        desc = (ov.get("Description") or "").strip()
        if desc:
            sent = AlphaVantageProvider._first_sentence(desc)
            return sent or None
        industry = (ov.get("Industry") or "").strip()
        sector = (ov.get("Sector") or "").strip()
        if industry and sector:
            return f"{industry} business in the {sector} sector."
        if industry:
            return f"{industry} business."
        if sector:
            return f"Operates in the {sector} sector."
        return None

    # ---- minimal price/prev_close (used by scheduler) ----
    def get_price_prev_close(self, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        q, err = self._global_quote(symbol)
        if err or not q:
            return None, None, err
        try:
            price = _f(q.get("05. price"))
            prev_close = _f(q.get("08. previous close"))
            # do NOT default prev_close to price; leave None if missing
            if price is None and prev_close is None:
                return None, None, "empty_quote"
            return price, prev_close, None
        except Exception:
            return None, None, "parse_error"

    # ---- full unified payload ----
    def get_full(self, symbol: str) -> Dict:
        s = symbol.upper().strip()
        q, q_err = self._global_quote(s)

        core = {
            "symbol": s,
            "open": None, "high": None, "low": None,
            "price": None, "volume": None,
            "latest_trading_day": None,
            "prev_close": None,
            "change": None,
            "change_percent": None,
            # fundamentals default blank
            "market_cap": None,
            "pe_ratio": None,
            "dividend_yield_percent": None,
            "fifty_two_week_high": None,
            "fifty_two_week_low": None,
            "quarterly_dividend_amount": None,
            # corporate events (Alpha Vantage free API does not provide forward earnings date)
            "next_earning_day": None,
            # company meta
            "description": None,
            "source": self.name,
            "error": None,
        }

        if q and not q_err:
            try:
                price = _f(q.get("05. price"))
                prev_close = _f(q.get("08. previous close"))

                core.update({
                    "symbol": q.get("01. symbol", s),
                    "open": _f(q.get("02. open")),
                    "high": _f(q.get("03. high")),
                    "low":  _f(q.get("04. low")),
                    "price": price,
                    "volume": _i(q.get("06. volume")),
                    "latest_trading_day": q.get("07. latest trading day"),
                    "prev_close": prev_close,
                })

                # Only derive when both exist
                if price is not None and prev_close is not None and prev_close != 0:
                    change = price - prev_close
                    core["change"] = change
                    core["change_percent"] = f"{(change / prev_close) * 100.0:.2f}%"
            except Exception:
                core["error"] = "parse_error"
        else:
            core["error"] = q_err or "unknown"
            # Continue to fill fundamentals best-effort

        # Fundamentals (best-effort; never invent)
        ov, ov_err = self._overview(s)
        if ov and not ov_err and core.get("error") != "parse_error":
            try:
                core["market_cap"] = _i(ov.get("MarketCapitalization"))
                core["pe_ratio"] = _f(ov.get("PERatio"))
                dy = _f(ov.get("DividendYield"))  # 0.0273 style
                core["dividend_yield_percent"] = (dy * 100.0) if dy is not None else None
                core["fifty_two_week_high"] = _f(ov.get("52WeekHigh"))
                core["fifty_two_week_low"]  = _f(ov.get("52WeekLow"))
                dps = _f(ov.get("DividendPerShare"))
                core["quarterly_dividend_amount"] = (dps / 4.0) if dps is not None else None
                core["description"] = self._compose_short_description(ov)
                # Alpha's free endpoints don't expose a forward earnings date reliably → keep next_earning_day=None
            except Exception:
                # Keep existing core values; fundamentals are optional.
                pass

        # If both price and prev_close are missing and no explicit error set, mark as empty
        if core["price"] is None and core["prev_close"] is None and core["error"] is None:
            core["error"] = "empty_quote"

        return core
