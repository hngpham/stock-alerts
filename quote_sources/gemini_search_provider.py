# quote_sources/gemini_search_provider.py
from __future__ import annotations
import os, json, logging
from typing import Optional, Tuple, Dict

log = logging.getLogger("stock-alert")

# Google GenAI SDK (Gemini API)
try:
    from google import genai
    from google.genai import types
except Exception:
    genai = None  # type: ignore
    types = None  # type: ignore


# ---- helpers to coerce types (kept local to avoid importing your other module) ----
def _f(x):
    try:
        return None if x in (None, "", "null") else float(x)
    except Exception:
        return None


def _i(x):
    try:
        return None if x in (None, "", "null") else int(float(x))
    except Exception:
        return None


def _normalize_err(e: Optional[str]) -> Optional[str]:
    if not e:
        return None
    e = str(e).lower()
    if "rate" in e and "limit" in e:
        return "rate_limited"
    if "api key" in e or "unauth" in e or "key_missing" in e:
        return "key_missing"
    if "invalid_symbol" in e:
        return "invalid_symbol"
    if "parse" in e:
        return "parse_error"
    if "empty" in e or "not found" in e:
        return "empty_quote"
    return e


def _strip_fences(s: str) -> str:
    """Remove common markdown fences like ```json ... ``` and leading/trailing whitespace."""
    t = s.strip()
    if t.startswith("```") and t.endswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json\n"):
            t = t[5:]
    return t.strip()


def _first_json_object(s: str) -> Optional[str]:
    """Return the first complete JSON object {...} from s (handles nested braces & strings)."""
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    i = start
    while i < len(s):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        i += 1
    return None


class GeminiSearchQuoteProvider:
    """
    Quote provider using Google's Gemini API with Grounding via Google Search.

    Env:
      - GEMINI_API_KEY (required)
      - GEMINI_MODEL (optional; default: 'gemini-2.5-flash-lite')
      - GEMINI_TIMEOUT (optional seconds; default 20)

    Returns a dict matching the unified schema in base.py.
    """

    name = "gemini_search"

    def __init__(self, timeout: int | None = None):
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
        self.timeout = int(os.getenv("GEMINI_TIMEOUT", str(timeout or 20)))
        self._client: Optional[genai.Client] = None
        if genai is not None and os.getenv("GEMINI_API_KEY"):
            try:
                self._client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
            except Exception as e:
                log.warning(f"Gemini client init failed: {e}")
                self._client = None

    # ---- protocol-required methods ----
    def is_ready(self) -> bool:
        return self._client is not None and bool(os.getenv("GEMINI_API_KEY"))

    def get_price_prev_close(
        self, symbol: str
    ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        d = self._ask(symbol)
        if d is None:
            return None, None, "network_error"
        try:
            price = _f(d.get("price"))
            prev_close = _f(d.get("prev_close"))
            err = _normalize_err(d.get("error"))
            if price is None and prev_close is None and err is None:
                err = "empty_quote"
            return price, prev_close, err
        except Exception:
            return None, None, "parse_error"

    def get_full(self, symbol: str) -> Dict:
        s = symbol.upper().strip()
        core = {
            "symbol": s,
            "open": None,
            "high": None,
            "low": None,
            "price": None,
            "volume": None,
            "latest_trading_day": None,
            "prev_close": None,
            "change": None,
            "change_percent": None,
            "market_cap": None,
            "pe_ratio": None,
            "dividend_yield_percent": None,
            "fifty_two_week_high": None,
            "fifty_two_week_low": None,
            "quarterly_dividend_amount": None,
            "next_earning_day": None,  # <-- NEW FIELD
            "description": None,
            "source": self.name,
            "error": None,
        }

        d = self._ask(s)
        if d is None:
            core["error"] = "network_error"
            return core

        try:
            core.update(
                {
                    "symbol": d.get("symbol") or s,
                    "open": _f(d.get("open")),
                    "high": _f(d.get("high")),
                    "low": _f(d.get("low")),
                    "price": _f(d.get("price")),
                    "volume": _i(d.get("volume")),
                    "latest_trading_day": d.get("latest_trading_day"),
                    "prev_close": _f(d.get("prev_close")),
                    "change": _f(d.get("change")),
                    "change_percent": d.get("change_percent"),
                    "market_cap": _i(d.get("market_cap")),
                    "pe_ratio": _f(d.get("pe_ratio")),
                    "dividend_yield_percent": _f(d.get("dividend_yield_percent")),
                    "fifty_two_week_high": _f(d.get("fifty_two_week_high")),
                    "fifty_two_week_low": _f(d.get("fifty_two_week_low")),
                    "quarterly_dividend_amount": _f(d.get("quarterly_dividend_amount")),
                    "next_earning_day": d.get(
                        "next_earning_day"
                    ),  # pass through as string like "YYYY-MM-DD"
                    "description": (
                        d.get("description")
                        if isinstance(d.get("description"), str)
                        else None
                    ),
                    "source": self.name,
                    "error": _normalize_err(d.get("error")),
                }
            )

            if (
                core["change"] is None
                and core["price"] is not None
                and core["prev_close"] is not None
            ):
                core["change"] = core["price"] - core["prev_close"]
            if (
                core["change_percent"] is None
                and core["change"] is not None
                and core["prev_close"] not in (None, 0)
            ):
                pct = (core["change"] / core["prev_close"]) * 100.0
                core["change_percent"] = f"{pct:.2f}%"

            if (
                core["price"] is None
                and core["prev_close"] is None
                and core["error"] is None
            ):
                core["error"] = "empty_quote"
        except Exception:
            core["error"] = "parse_error"

        return core

    # ---- internal: ask Gemini with Search Grounding (no response_mime_type when tools are used) ----
    def _ask(self, symbol: str) -> Optional[dict]:
        s = symbol.upper().strip()
        if not self.is_ready():
            return {"symbol": s, "error": "key_missing"}

        text = ""
        try:
            # Enable Google Search grounding tool
            grounding_tool = types.Tool(google_search=types.GoogleSearch())

            # IMPORTANT: Tools + response_mime_type('application/json') is unsupported.
            # We rely on instruction + robust parsing instead.
            schema = {
                "symbol": s,
                "price": None,
                "prev_close": None,
                "open": None,
                "high": None,
                "low": None,
                "volume": None,
                "latest_trading_day": None,  # "YYYY-MM-DD"
                "change": None,
                "change_percent": None,  # "12.34%"
                "market_cap": None,
                "pe_ratio": None,
                "dividend_yield_percent": None,
                "fifty_two_week_high": None,
                "fifty_two_week_low": None,
                "quarterly_dividend_amount": None,
                "next_earning_day": None,  # <-- NEW FIELD, "YYYY-MM-DD" if known/upcoming
                "description": None,
                "source": self.name,
                "error": None,
            }

            config = types.GenerateContentConfig(
                tools=[grounding_tool],
                temperature=0,
                system_instruction=(
                    "Act as a finance quote extractor. Use Google Search tool to ground answers. "
                    "Return ONE and only ONE JSON object with keys:\n"
                    "symbol, price, prev_close, open, high, low, volume, latest_trading_day, "
                    "change, change_percent, market_cap, pe_ratio, dividend_yield_percent, "
                    "fifty_two_week_high, fifty_two_week_low, quarterly_dividend_amount, "
                    "next_earning_day, description, source, error.\n"
                    "No markdown, no code fences, no commentary. If unsure, use null and set "
                    "error to 'no_realtime_access' when data cannot be verified as fresh."
                ),
            )

            prompt = {
                "task": (
                    "Fetch current price, basic fundamentals, and the NEXT scheduled earnings date "
                    "(as an ISO date 'YYYY-MM-DD' if available) for a US stock ticker from reputable "
                    "sources (Nasdaq, exchange site, SEC, Yahoo Finance, company IR page). Return JSON."
                ),
                "ticker": s,
                "return_schema_keys": list(schema.keys()),
                "notes": [
                    "Description: one concise sentence (business focus + any near-term catalyst).",
                    "If next earnings date is not announced or unclear, return null for next_earning_day.",
                    "No financial advice. If unsure about a field, use null.",
                    "Return JSON only—no extra text.",
                ],
                "examples": [
                    {
                        "symbol": "AAPL",
                        "price": 227.79,
                        "prev_close": 228.32,
                        "open": 229.01,
                        "high": 230.15,
                        "low": 226.80,
                        "volume": 48231512,
                        "latest_trading_day": "2025-09-13",
                        "change": -0.53,
                        "change_percent": "-0.23%",
                        "market_cap": "3.49T",
                        "pe_ratio": 32.4,
                        "dividend_yield_percent": 0.47,
                        "fifty_two_week_high": 238.56,
                        "fifty_two_week_low": 161.79,
                        "quarterly_dividend_amount": 0.25,
                        "next_earning_day": "2025-10-30",
                        "description": "Apple designs consumer electronics and services; shares trade near record highs with steady demand for iPhone and Services revenue growth.",
                        "source": "google_search_tool",
                        "error": None,
                    }
                ],
            }

            resp = self._client.models.generate_content(  # type: ignore[union-attr]
                model=self.model,
                contents=json.dumps(prompt),
                config=config,
            )

            text = (getattr(resp, "text", "") or "").strip()
            log.warning(f"Response {text}")
            candidate = _first_json_object(_strip_fences(text)) or _strip_fences(text)
            data = json.loads(candidate)

            # enforce source and symbol
            data["source"] = self.name
            if not data.get("symbol"):
                data["symbol"] = s
            # ensure the new key always exists
            if "next_earning_day" not in data:
                data["next_earning_day"] = None
            return data

        except Exception as e:
            preview = ""
            try:
                preview = (text[:300] + "…") if text else ""
            except Exception:
                pass
            log.warning(f"Gemini request failed for {s}: {e} :: preview={preview!r}")
            # Return a structured error so the loop continues and UI counters update
            return {
                "symbol": s,
                "source": self.name,
                "next_earning_day": None,
                "error": "parse_error",
            }
