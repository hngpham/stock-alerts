# quote_sources/chatgpt_search_preview_provider.py
from __future__ import annotations
import os
import json
import re
import logging
from typing import Optional, Tuple, Dict

log = logging.getLogger("stock-alert")

# OpenAI SDK v1+
try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class ChatGPTSearchPreviewQuoteProvider:
    """
    LLM-backed quote provider that uses OpenAI's web-enabled model
    to fetch a *current* price and basic fundamentals, returning the
    same unified schema as other providers.

    Minimal config:
      - Env: OPENAI_API_KEY (required by OpenAI SDK)
      - Env: OPENAI_MODEL (optional; defaults to 'gpt-4o-mini-search-preview')

    Notes:
      - We DO NOT use response_format='json_object' because it is not
        supported with web_search.
      - We allow the model to return a JSON *string* in content; we then
        parse it here. If the model can’t verify live data, it should set
        numbers to null and `error: "no_realtime_access"` as instructed.
      - Includes a short "description" (one sentence: industry + business focus).
      - NEW: Supports `next_earning_day` to match base.py schema.
    """
    name = "chatgpt_search_preview"

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini-search-preview")
        self._client: Optional[OpenAI] = None
        if OpenAI is not None:
            # Defer real network use to call time; constructing the client is cheap.
            try:
                self._client = OpenAI(max_retries=0)
            except Exception as e:
                log.warning(f"OpenAI client init failed: {e}")
                self._client = None

    # -------- Protocol-required helpers (aligns with base.py) --------
    def is_ready(self) -> bool:
        # Match alpha_vantage style: return True only when configured.
        return (self._client is not None) and bool(os.getenv("OPENAI_API_KEY"))

    def get_price_prev_close(self, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        d = self._ask(symbol)
        if d is None:
            return None, None, "network_error"
        try:
            price = _coerce_float(d.get("price"))
            prev_close = _coerce_float(d.get("prev_close"))
            err = _normalize_error_code(d.get("error"))
            if price is None and prev_close is None and err is None:
                err = "empty_quote"
            return price, prev_close, err
        except Exception:
            return None, None, "parse_error"

    def get_full(self, symbol: str) -> Dict:
        s = symbol.upper().strip()
        core = {
            "symbol": s,
            "open": None, "high": None, "low": None,
            "price": None, "volume": None,
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
            "next_earning_day": None,             # NEW (compat with base.py & Gemini)
            "description": None,
            "source": self.name,
            "error": None,
        }

        d = self._ask(s)
        if d is None:
            core["error"] = "network_error"
            return core

        try:
            # Fill in whatever the model provided (best-effort)
            core.update({
                "symbol": (d.get("symbol") or s),
                "open": _coerce_float(d.get("open")),
                "high": _coerce_float(d.get("high")),
                "low": _coerce_float(d.get("low")),
                "price": _coerce_float(d.get("price")),
                "volume": _coerce_int(d.get("volume")),
                "latest_trading_day": d.get("latest_trading_day"),
                "prev_close": _coerce_float(d.get("prev_close")),
                "change": _coerce_float(d.get("change")),
                "change_percent": d.get("change_percent"),
                "market_cap": _coerce_int(d.get("market_cap")),
                "pe_ratio": _coerce_float(d.get("pe_ratio")),
                "dividend_yield_percent": _coerce_float(d.get("dividend_yield_percent")),
                "fifty_two_week_high": _coerce_float(d.get("fifty_two_week_high")),
                "fifty_two_week_low": _coerce_float(d.get("fifty_two_week_low")),
                "quarterly_dividend_amount": _coerce_float(d.get("quarterly_dividend_amount")),
                "next_earning_day": d.get("next_earning_day"),   # NEW
                "description": (d.get("description") if isinstance(d.get("description"), str) else None),
                "source": self.name,
                "error": _normalize_error_code(d.get("error")),
            })

            # If the model omitted derived fields but gave price/prev_close, compute them.
            if core["change"] is None and core["price"] is not None and core["prev_close"] is not None:
                core["change"] = core["price"] - core["prev_close"]
            if core["change_percent"] is None and core["change"] is not None and core["prev_close"] not in (None, 0):
                pct = (core["change"] / core["prev_close"]) * 100.0
                core["change_percent"] = f"{pct:.2f}%"

            # If *everything* critical is missing and no explicit error was set, align with alpha provider
            if core["price"] is None and core["prev_close"] is None and core["error"] is None:
                core["error"] = "empty_quote"
        except Exception:
            core["error"] = "parse_error"

        return core

    # -------- internals --------
    def _ask(self, symbol: str) -> Optional[dict]:
        """
        Ask the web-enabled model for a single JSON object. Returns parsed dict
        or structured error dict on failure (never raises to caller).
        """
        s = symbol.upper().strip()

        if not self.is_ready():
            # Mirror other providers
            return {"symbol": s, "source": self.name, "next_earning_day": None, "error": "key_missing"}

        content: Optional[str] = None
        try:
            system_msg = (
                "You are a finance quote extractor. Use web search to fetch a *current* price "
                "and basic fundamentals for the given ticker from reputable finance sites "
                "(exchange site, Nasdaq, Yahoo Finance, Bloomberg, company IR, etc.). "
                "Return ONLY a JSON object with the exact schema below. "
                "If you cannot verify fresh data, set all numeric fields to null and error='no_realtime_access'. "
                "Description part: 1 concise sentence: business/industry + any recent catalyst or momentum note. "
                "Never guess or fabricate numbers. "
                "Do not output markdown, code fences, or extra commentary."
            )

            # Schema (single line) — includes next_earning_day to match base.py
            schema = (
                '{"symbol":"<TICKER>",'
                '"price":<number or null>,"prev_close":<number or null>,'
                '"open":<number or null>,"high":<number or null>,"low":<number or null>,'
                '"volume":<integer or null>,"latest_trading_day":<YYYY-MM-DD or null>,'
                '"next_earning_day":<YYYY-MM-DD or null>,'
                '"change":<number or null>,"change_percent":<string or null>,'
                '"market_cap":<integer or null>,"pe_ratio":<number or null>,'
                '"dividend_yield_percent":<number or null>,'
                '"fifty_two_week_high":<number or null>,"fifty_two_week_low":<number or null>,'
                '"quarterly_dividend_amount":<number or null>,'
                '"description":<string or null>,'
                '"source":"chatgpt_search_preview","error":<null or short error string>}'
            )

            user_msg = (
                f"Ticker: {s}\n"
                f"Return ONLY this JSON object:\n"
                f"{schema.replace('<TICKER>', s)}"
            )

            completion = self._client.chat.completions.create(  # type: ignore[union-attr]
                model=self.model,
                web_search_options={},  # keep present to enable web search
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                timeout=self.timeout,
            )

            content = completion.choices[0].message.content if completion.choices else None  # type: ignore[index]
            if not content:
                return {"symbol": s, "source": self.name, "next_earning_day": None, "error": "network_error"}

            # Some models return fenced code blocks or extra prose. Strip & extract first JSON object.
            cleaned = _strip_md_fences(content)
            candidate = _first_json_object(cleaned) or cleaned
            data = json.loads(candidate)

            # Enforce required bookkeeping & key presence
            if isinstance(data, dict):
                data.setdefault("source", self.name)
                data.setdefault("symbol", s)
                if "next_earning_day" not in data:
                    data["next_earning_day"] = None
                return data

            return {"symbol": s, "source": self.name, "next_earning_day": None, "error": "parse_error"}

        except Exception as e:
            preview = ""
            try:
                if content:
                    preview = (content[:300] + "…")
            except Exception:
                pass
            log.warning(f"OpenAI request failed for {s}: {e} :: preview={preview!r}")
            # Return structured error so the app can continue gracefully
            return {"symbol": s, "source": self.name, "next_earning_day": None, "error": "parse_error"}


# -------- small parsing helpers --------
_num_re = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*$")

def _coerce_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("%"):
            try:
                return float(s[:-1])
            except Exception:
                return None
        if _num_re.match(s):
            try:
                return float(s)
            except Exception:
                return None
    return None

def _coerce_int(v):
    f = _coerce_float(v)
    return int(f) if f is not None else None

def _strip_md_fences(text: str) -> str:
    """
    Return the JSON-ish content without Markdown fences or trailing prose.
    Handles:
      - ```json\n{...}\n```
      - {...}\n```\nSome prose...
      - ```\n{...}  (no closing fence)
    """
    t = (text or "").strip()

    # 1) Preferred: extract the first fenced block anywhere.
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", t, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 2) If content contains a closing fence later (JSON first, then ``` + prose), cut at the fence.
    fence_idx = t.find("```")
    if fence_idx != -1:
        return t[:fence_idx].strip()

    # 3) If it starts with an opening fence but no closing one, drop the first fence line.
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n?", "", t, flags=re.IGNORECASE)

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
                    return s[start:i + 1]
        i += 1
    return None

def _normalize_error_code(err) -> Optional[str]:
    if not err:
        return None
    s = str(err).strip().lower()
    # Map common/model-provided errors into our standardized set (base.py)
    if s in {"no_realtime_access", "no-real-time-access"}:
        return "no_realtime_access"
    if s in {"rate_limited", "rate-limit", "rate limit"}:
        return "rate_limited"
    if s in {"invalid_symbol", "invalid-sym", "bad_symbol"}:
        return "invalid_symbol"
    if s in {"key_missing", "no_api_key"}:
        return "key_missing"
    if s in {"network_error", "network"}:
        return "network_error"
    if s in {"empty_quote", "empty"}:
        return "empty_quote"
    if s in {"parse_error", "parse"}:
        return "parse_error"
    if s in {"unconfigured"}:
        return "unconfigured"
    # default
    return s
