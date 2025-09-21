# main.py — Sticker/Stock Alert backend (refactored & de-duplicated)

import os
import sqlite3
import time
import logging
import threading
from pathlib import Path
from typing import Dict, Tuple, Optional, Any
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from quote_sources import get_provider


# =========================
# Logging / Configuration
# =========================

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stock-alert")

DB_PATH = os.getenv("DB_PATH", "/data/stocks.db")
Path(os.path.dirname(DB_PATH)).mkdir(parents=True, exist_ok=True)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
MARKET_TZ = os.getenv("MARKET_TZ", "America/New_York")
COOLDOWN_MINUTES = int(os.getenv("ALERT_COOLDOWN_MINUTES", "15"))
EARNINGS_NOTIFY_DEFAULT_DAYS = int(
    os.getenv("EARNINGS_NOTIFY_DEFAULT_DAYS", "1")
)  # default alert seed
ALERT_FIRE_TIMES = [
    t.strip()
    for t in os.getenv("ALERT_FIRE_TIMES", "09:35,12:00,15:55").split(",")
    if t.strip()
]
QUOTE_PROVIDER_NAME = (os.getenv("QUOTE_PROVIDER") or "alpha_vantage").lower().strip()
RUN_TIMEOUT_SECONDS = int(
    os.getenv("RUN_TIMEOUT_SECONDS", "600")
)  # auto-recover stuck "running"

# Provider pacing knobs (kept for future use if needed by provider impls)
PROVIDER_MIN_INTERVAL_MS = int(os.getenv("PROVIDER_MIN_INTERVAL_MS", "20000"))
PROVIDER_BACKOFF_BASE = float(os.getenv("PROVIDER_BACKOFF_BASE", "3.0"))
PROVIDER_BACKOFF_MAX = float(os.getenv("PROVIDER_BACKOFF_MAX", "30.0"))

# Symbol state required columns (idempotent migrations)
REQUIRED_SYMBOL_STATE_COLS: Dict[str, str] = {
    # bookkeeping
    "last_check_epoch": "INTEGER",
    "last_check_note": "TEXT",
    "window_open": "INTEGER",
    "cooldown_minutes": "INTEGER",
    "server_tz": "TEXT",
    # intraday / last
    "price": "REAL",
    "prev_close": "REAL",
    "open": "REAL",
    "high": "REAL",
    "low": "REAL",
    "volume": "INTEGER",
    "latest_trading_day": "TEXT",
    "change": "REAL",
    "change_percent": "TEXT",
    # fundamentals
    "market_cap": "INTEGER",
    "pe_ratio": "REAL",
    "dividend_yield_percent": "REAL",
    "fifty_two_week_high": "REAL",
    "fifty_two_week_low": "REAL",
    "quarterly_dividend_amount": "REAL",
    "source": "TEXT",
    # company meta
    "description": "TEXT",
    # corporate event
    "next_earning_day": "TEXT",  # ISO date "YYYY-MM-DD"
}


# =========================
# Utilities
# =========================


def now_et() -> datetime:
    return datetime.now(ZoneInfo(MARKET_TZ))


def is_trading_window(now_eastern: datetime) -> bool:
    """A practical 'notify' window (pre/regular/post over-broad by design)."""
    if now_eastern.weekday() >= 5:
        return False
    start = dtime(8, 30)
    end = dtime(17, 0)
    return start <= now_eastern.time() <= end


def _mask_key(k: Optional[str]) -> str:
    if not k:
        return "<MISSING>"
    return (k[:4] + "…" + k[-4:]) if len(k) > 8 else "****"


def _fmt_pct(val: Optional[float]) -> str:
    return "—" if val is None else f"{val:.2f}%"


# =========================
# DB Helpers / Migrations
# =========================


def get_conn() -> sqlite3.Connection:
    # Row factory gives dict-like access when needed
    conn = sqlite3.connect(DB_PATH, timeout=30)
    return conn


def init_db() -> None:
    with get_conn() as conn:
        c = conn.cursor()

        # Core tables
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS groups (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS symbols (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ticker TEXT NOT NULL,
          group_id INTEGER,
          note TEXT,
          rating INTEGER NOT NULL DEFAULT 0,
          last_edit_epoch INTEGER,
          FOREIGN KEY(group_id) REFERENCES groups(id)
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol_id INTEGER NOT NULL,
          type TEXT NOT NULL,
          value REAL NOT NULL,
          FOREIGN KEY(symbol_id) REFERENCES symbols(id)
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS alert_state (
          alert_id INTEGER PRIMARY KEY,
          last_sent_epoch INTEGER
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS symbol_state (
          symbol_id INTEGER PRIMARY KEY,
          last_check_epoch INTEGER,
          window_open INTEGER,
          note TEXT,
          last_price REAL,
          last_prev_close REAL
        )"""
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS run_status (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          phase TEXT,
          started_epoch INTEGER,
          finished_epoch INTEGER,
          status_code TEXT,
          message TEXT,
          ok_count INTEGER,
          err_count INTEGER
        )"""
        )

        # Seed run_status
        c.execute(
            "INSERT OR IGNORE INTO run_status (id, phase, message, ok_count, err_count) VALUES (1,'idle','No runs yet',0,0)"
        )
        # Seed groups
        c.execute("INSERT OR IGNORE INTO groups(name) VALUES ('watch')")
        c.execute("INSERT OR IGNORE INTO groups(name) VALUES ('archived')")
        conn.commit()

    _ensure_symbols_columns(
        ["rating INTEGER NOT NULL DEFAULT 0", "last_edit_epoch INTEGER"]
    )
    _ensure_alert_state_last_key()
    ensure_symbol_state_columns()


def _ensure_symbols_columns(column_defs: list[str]) -> None:
    """Idempotent 'ALTER TABLE symbols ADD COLUMN ...' for specified column definitions."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(symbols)")
        existing = {row[1] for row in c.fetchall()}
        for col_def in column_defs:
            col_name = col_def.split()[0]
            if col_name not in existing:
                try:
                    c.execute(f"ALTER TABLE symbols ADD COLUMN {col_def}")
                    conn.commit()
                    log.info(f"Added column to symbols: {col_def}")
                except sqlite3.OperationalError as e:
                    log.warning(f"Migration (symbols {col_def}) skipped: {e}")


def _ensure_alert_state_last_key() -> None:
    """Ensure 'last_sent_key' exists in alert_state to key earnings reminders by date."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(alert_state)")
        cols = {row[1] for row in c.fetchall()}
        if "last_sent_key" not in cols:
            c.execute("ALTER TABLE alert_state ADD COLUMN last_sent_key TEXT")
            conn.commit()
            log.info("Added last_sent_key to alert_state")


def ensure_symbol_state_columns() -> None:
    """Ensure wide symbol_state schema to support cache-first frontend."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("PRAGMA table_info(symbol_state)")
        existing = {row[1] for row in c.fetchall()}
        for col, typ in REQUIRED_SYMBOL_STATE_COLS.items():
            if col not in existing:
                c.execute(f"ALTER TABLE symbol_state ADD COLUMN {col} {typ}")
        conn.commit()


def get_group_id(name: str) -> int:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM groups WHERE name=?", (name,))
        row = c.fetchone()
    if not row:
        raise RuntimeError(f"Group '{name}' missing")
    return row[0]


# Initialize DB and derive group IDs
init_db()
WATCH_ID = get_group_id("watch")
ARCHIVE_ID = get_group_id("archived")


# =========================
# Run Status Helpers
# =========================


def set_run_status(
    phase: str,
    *,
    started: Optional[int] = None,
    finished: Optional[int] = None,
    status_code: Optional[str] = None,
    message: Optional[str] = None,
    ok_count: Optional[int] = None,
    err_count: Optional[int] = None,
) -> None:
    sets, vals = ["phase=?"], [phase]
    if started is not None:
        sets.append("started_epoch=?")
        vals.append(started)
    if finished is not None:
        sets.append("finished_epoch=?")
        vals.append(finished)
    if status_code is not None:
        sets.append("status_code=?")
        vals.append(status_code)
    if message is not None:
        sets.append("message=?")
        vals.append((message or "")[:400])
    if ok_count is not None:
        sets.append("ok_count=?")
        vals.append(ok_count)
    if err_count is not None:
        sets.append("err_count=?")
        vals.append(err_count)
    vals.append(1)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE run_status SET {', '.join(sets)} WHERE id=?", vals)
        conn.commit()


def get_run_status() -> Dict[str, Any]:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT phase, started_epoch, finished_epoch, status_code, message, ok_count, err_count
                     FROM run_status WHERE id=1"""
        )
        row = c.fetchone()
    if not row:
        return {"phase": "idle", "message": "No runs", "timezone": MARKET_TZ}
    return {
        "phase": row[0],
        "started_epoch": row[1],
        "finished_epoch": row[2],
        "status_code": row[3],
        "message": row[4],
        "ok_count": row[5],
        "err_count": row[6],
        "timezone": MARKET_TZ,
    }


def _force_finish_run_status(status_code: str, message: str) -> None:
    set_run_status(
        "finished", finished=int(time.time()), status_code=status_code, message=message
    )


def _auto_recover_run_status() -> None:
    """If phase == 'running' but started_epoch is too old, force-finish as interrupted."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT phase, started_epoch FROM run_status WHERE id=1")
        row = c.fetchone()
        if not row:
            return
        phase, started_epoch = row[0], row[1]
        if phase == "running" and isinstance(started_epoch, int):
            age = int(time.time()) - started_epoch
            if age >= RUN_TIMEOUT_SECONDS:
                c.execute(
                    """UPDATE run_status
                       SET phase='finished',
                           finished_epoch=?,
                           status_code='interrupted_timeout',
                           message='Auto-recovered: run exceeded timeout'
                     WHERE id=1""",
                    (int(time.time()),),
                )
                conn.commit()
                log.warning(
                    f"Auto-recovered stuck run (age={age}s >= {RUN_TIMEOUT_SECONDS}s)"
                )


def _recover_stuck_run_status() -> None:
    """On startup mark any lingering 'running' as interrupted."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT phase, started_epoch FROM run_status WHERE id=1")
        row = c.fetchone()
        if not row:
            return
        phase, started_epoch = row[0], row[1]
        if phase == "running":
            now = int(time.time())
            age = now - (started_epoch or now)
            c.execute(
                """UPDATE run_status
                         SET phase='finished',
                             finished_epoch=?,
                             status_code='interrupted',
                             message='Previous run did not finish (server restarted).'
                       WHERE id=1""",
                (now,),
            )
            conn.commit()
            log.info(f"Recovered 'running' on startup (age={age}s).")


def _is_run_already_running() -> bool:
    return (get_run_status() or {}).get("phase") == "running"


# =========================
# Provider & Notify
# =========================

PROVIDER = get_provider()
log.info(f"Quote provider: {QUOTE_PROVIDER_NAME}")
if not PROVIDER.is_ready():
    if QUOTE_PROVIDER_NAME == "alpha_vantage":
        log.warning(
            f"ALPHA_VANTAGE_KEY is not set or provider not ready: {_mask_key(os.getenv('ALPHA_VANTAGE_KEY'))}"
        )
    else:
        log.warning(f"Provider '{QUOTE_PROVIDER_NAME}' is not ready or unconfigured.")


def _notify_discord(content: str) -> bool:
    """Send a simple message to Discord webhook if configured."""
    if not DISCORD_WEBHOOK:
        log.debug("DISCORD_WEBHOOK not set; skipping Discord notify")
        return False
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
        if r.status_code // 100 != 2:
            log.warning(f"Discord webhook non-2xx: {r.status_code} {r.text[:180]}")
            return False
        return True
    except Exception as e:
        log.warning(f"Discord webhook failed: {e}")
        return False


# =========================
# Symbol-State Persistence
# =========================


def upsert_symbol_state(
    symbol_id: int,
    window_open: bool,
    note: str,
    last_price: Optional[float] = None,
    last_prev_close: Optional[float] = None,
) -> None:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO symbol_state (symbol_id, last_check_epoch, window_open, note, last_price, last_prev_close)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol_id) DO UPDATE SET
              last_check_epoch=excluded.last_check_epoch,
              window_open=excluded.window_open,
              note=excluded.note,
              last_price=excluded.last_price,
              last_prev_close=excluded.last_prev_close
            """,
            (
                symbol_id,
                int(time.time()),
                1 if window_open else 0,
                (note or "")[:300],
                last_price,
                last_prev_close,
            ),
        )
        conn.commit()


def upsert_symbol_state_full(
    symbol_id: int, window_open: bool, note: str, data: Optional[Dict[str, Any]]
) -> None:
    """Persist a wide row for cache-first UI; robust to missing fields (store NULLs)."""

    def v(key: str) -> Any:
        return None if not data else data.get(key)

    # Normalize earnings date into YYYY-MM-DD string
    next_day = None
    if data:
        for k in (
            "next_earning_day",
            "next_earnings_day",
            "earnings_date",
            "nextEarningsDate",
        ):
            val = data.get(k)
            if val:
                next_day = val
                break
        if isinstance(next_day, (int, float)) and next_day > 10_000:
            # epoch seconds -> date
            try:
                next_day = datetime.utcfromtimestamp(int(next_day)).strftime("%Y-%m-%d")
            except Exception:
                pass
        if isinstance(next_day, str) and len(next_day) >= 10:
            next_day = next_day[:10]

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO symbol_state
                (symbol_id, last_check_epoch, last_check_note, window_open,
                 price, prev_close, open, high, low, volume, latest_trading_day,
                 change, change_percent, market_cap, pe_ratio, dividend_yield_percent,
                 fifty_two_week_high, fifty_two_week_low, quarterly_dividend_amount, source,
                 description, next_earning_day)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol_id) DO UPDATE SET
                last_check_epoch=excluded.last_check_epoch,
                last_check_note=excluded.last_check_note,
                window_open=excluded.window_open,
                price=excluded.price,
                prev_close=excluded.prev_close,
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                volume=excluded.volume,
                latest_trading_day=excluded.latest_trading_day,
                change=excluded.change,
                change_percent=excluded.change_percent,
                market_cap=excluded.market_cap,
                pe_ratio=excluded.pe_ratio,
                dividend_yield_percent=excluded.dividend_yield_percent,
                fifty_two_week_high=excluded.fifty_two_week_high,
                fifty_two_week_low=excluded.fifty_two_week_low,
                quarterly_dividend_amount=excluded.quarterly_dividend_amount,
                source=excluded.source,
                description=excluded.description,
                next_earning_day=excluded.next_earning_day
            """,
            (
                symbol_id,
                int(time.time()),
                (note or "")[:300],
                1 if window_open else 0,
                v("price"),
                v("prev_close"),
                v("open"),
                v("high"),
                v("low"),
                v("volume"),
                v("latest_trading_day"),
                v("change"),
                v("change_percent"),
                v("market_cap"),
                v("pe_ratio"),
                v("dividend_yield_percent"),
                v("fifty_two_week_high"),
                v("fifty_two_week_low"),
                v("quarterly_dividend_amount"),
                v("source"),
                v("description"),
                next_day,
            ),
        )
        conn.commit()


# =========================
# Alerts / Evaluation
# =========================


def _cooldown_ok(last_sent_epoch: Optional[int], now_epoch: int) -> bool:
    return (
        True
        if last_sent_epoch is None
        else (now_epoch - last_sent_epoch) >= COOLDOWN_MINUTES * 60
    )


def _build_alert_msg(
    ticker: str,
    price: Optional[float],
    open_: Optional[float],
    pct_from_open: Optional[float],
    trigger: str,
) -> str:
    pr = "—" if price is None else f"{price:.2f}"
    op = "—" if open_ is None else f"{open_:.2f}"
    pf = _fmt_pct(pct_from_open)
    return f"**{ticker}** {trigger}\nPrice: {pr} | Open: {op} | From open: {pf}"


def _days_until_earnings(next_earning_day: Optional[str], tz: str) -> Optional[int]:
    if not next_earning_day:
        return None
    try:
        d = str(next_earning_day)[:10]
        earn_date = datetime.strptime(d, "%Y-%m-%d").date()
        now_local = datetime.now(ZoneInfo(tz)).date()
        return (earn_date - now_local).days
    except Exception:
        return None


def get_last_sent_epoch(alert_id: int) -> Optional[int]:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT last_sent_epoch FROM alert_state WHERE alert_id=?", (alert_id,)
        )
        row = c.fetchone()
    return row[0] if row and row[0] is not None else None


def set_last_sent_epoch(alert_id: int, epoch: int) -> None:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO alert_state(alert_id, last_sent_epoch)
               VALUES (?, ?)
               ON CONFLICT(alert_id) DO UPDATE SET last_sent_epoch=excluded.last_sent_epoch
            """,
            (alert_id, epoch),
        )
        conn.commit()


def set_last_sent(alert_id: int, epoch: int, key: Optional[str] = None) -> None:
    with get_conn() as conn:
        c = conn.cursor()
        if key is None:
            c.execute(
                """INSERT INTO alert_state(alert_id, last_sent_epoch)
                   VALUES (?, ?)
                   ON CONFLICT(alert_id) DO UPDATE SET last_sent_epoch=excluded.last_sent_epoch
                """,
                (alert_id, epoch),
            )
        else:
            c.execute(
                """INSERT INTO alert_state(alert_id, last_sent_epoch, last_sent_key)
                   VALUES (?, ?, ?)
                   ON CONFLICT(alert_id) DO UPDATE SET
                       last_sent_epoch=excluded.last_sent_epoch,
                       last_sent_key=excluded.last_sent_key
                """,
                (alert_id, epoch, key),
            )
        conn.commit()


def get_last_sent_key(alert_id: int) -> Optional[str]:
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT last_sent_key FROM alert_state WHERE alert_id=?", (alert_id,))
        row = c.fetchone()
    return row[0] if row and row[0] is not None else None


def _evaluate_and_notify(
    symbol_id: int, ticker: str, quote: Dict[str, Any], now_epoch: int
) -> int:
    """Returns number of notifications sent for this symbol."""
    # Pull alert rules
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, type, value FROM alerts WHERE symbol_id=?", (symbol_id,))
        alerts = c.fetchall()
    if not alerts:
        return 0

    price = quote.get("price")
    open_ = quote.get("open")
    pct_from_open = None
    if open_ not in (None, 0) and price is not None:
        pct_from_open = (price - open_) / open_ * 100.0

    # Earnings date (normalized search across possible keys)
    next_earn = None
    for k in (
        "next_earning_day",
        "next_earnings_day",
        "earnings_date",
        "nextEarningsDate",
    ):
        if quote.get(k):
            next_earn = str(quote.get(k))[:10]
            break

    sent = 0
    for alert_id, a_type, a_val in alerts:
        try:
            trigger = None

            if a_type == "earnings_days":
                days_left = _days_until_earnings(next_earn, MARKET_TZ)
                if days_left is not None and int(days_left) == int(a_val):
                    last_key = get_last_sent_key(alert_id)
                    if last_key != (next_earn or ""):
                        msg = f"🔔 **{ticker}** **earnings in {int(a_val)} day(s)** on `{next_earn}`"
                        if _notify_discord(msg):
                            set_last_sent(alert_id, now_epoch, key=next_earn or "")
                            sent += 1
                # No cooldown for earnings (keyed by date)
                continue

            # Price/percent alerts respect cooldown
            last_sent = get_last_sent_epoch(alert_id)
            if not _cooldown_ok(last_sent, now_epoch):
                continue

            if a_type == "above" and (price is not None) and price >= a_val:
                trigger = f"crossed **above {a_val:.2f}**"
            elif a_type == "below" and (price is not None) and price <= a_val:
                trigger = f"fell **below {a_val:.2f}**"
            elif (
                a_type == "pct_drop"
                and (pct_from_open is not None)
                and pct_from_open <= -abs(a_val)
            ):
                trigger = f"**{abs(a_val):.0f}% drop** from open"
            elif (
                a_type == "pct_jump"
                and (pct_from_open is not None)
                and pct_from_open >= abs(a_val)
            ):
                trigger = f"**{abs(a_val):.0f}% jump** from open"

            if trigger:
                msg = _build_alert_msg(ticker, price, open_, pct_from_open, trigger)
                if _notify_discord(msg):
                    set_last_sent_epoch(alert_id, now_epoch)
                    sent += 1

        except Exception as e:
            log.warning(f"Alert eval failed for {ticker} alert_id={alert_id}: {e}")

    return sent


# =========================
# Bulk Update (Scheduler)
# =========================


def check_alerts() -> None:
    started = int(time.time())
    set_run_status(
        "running",
        started=started,
        status_code=None,
        message="Updating quotes…",
        ok_count=0,
        err_count=0,
    )

    ok_count = 0
    err_count = 0
    rate_limited_seen = False
    total_notified = 0

    try:
        with get_conn() as conn:
            c = conn.cursor()
            # bulk update ONLY pulls from WATCH group (archived excluded)
            c.execute(
                "SELECT id, ticker FROM symbols WHERE group_id=? ORDER BY ticker",
                (WATCH_ID,),
            )
            all_symbols = c.fetchall()

        if not all_symbols:
            set_run_status(
                "finished",
                finished=int(time.time()),
                status_code="ok",
                message="No symbols in watchlist",
                ok_count=0,
                err_count=0,
            )
            return

        et_now = now_et()
        window_ok = is_trading_window(et_now)

        for symbol_id, ticker in all_symbols:
            prefix = f"{et_now.strftime('%Y-%m-%d %H:%M:%S %Z')} | Window: {'OPEN' if window_ok else 'CLOSED'}"
            try:
                full = PROVIDER.get_full(ticker)
            except Exception as e:
                full = {"error": "network_error"}
                log.warning(f"Provider exception for {ticker}: {e}")

            err = (full or {}).get("error")
            if err:
                if err == "rate_limited":
                    rate_limited_seen = True
                err_count += 1
                upsert_symbol_state_full(
                    symbol_id, window_ok, f"{prefix} | {err}", data=None
                )
            else:
                ok_count += 1
                upsert_symbol_state_full(
                    symbol_id, window_ok, f"{prefix} | Price check ok", data=full
                )
                total_notified += _evaluate_and_notify(
                    symbol_id, ticker, full, int(time.time())
                )

    except Exception as e:
        log.exception(f"check_alerts fatal error: {e}")
    finally:
        finished = int(time.time())
        status_code = "ok"
        if err_count and rate_limited_seen:
            status_code = "rate_limited"
        elif err_count:
            status_code = "partial"
        msg = f"Updated {ok_count} symbol(s), {err_count} error(s); notified {total_notified}"
        set_run_status(
            "finished",
            finished=finished,
            status_code=status_code,
            message=msg,
            ok_count=ok_count,
            err_count=err_count,
        )


def _parse_fire_time(hhmm: str) -> Tuple[int, int]:
    hh, mm = hhmm.split(":")
    return int(hh), int(mm)


def schedule_jobs(scheduler: BackgroundScheduler) -> None:
    for t in ALERT_FIRE_TIMES:
        try:
            hh, mm = _parse_fire_time(t)
        except Exception:
            log.error(f"Invalid ALERT_FIRE_TIMES entry '{t}', expected HH:MM")
            continue
        trig = CronTrigger(hour=hh, minute=mm, timezone=ZoneInfo(MARKET_TZ))
        scheduler.add_job(check_alerts, trig, name=f"check_alerts@{t}")
        log.info(f"Scheduled check_alerts at {t} ({MARKET_TZ})")


scheduler = BackgroundScheduler()
schedule_jobs(scheduler)
scheduler.start()


# =========================
# FastAPI App / Routes
# =========================

app = FastAPI(title="Stock Alert")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
def root():
    return FileResponse("frontend/index.html")


def _row_to_symbol(row: Tuple[Any, ...]) -> Dict[str, Any]:
    # SELECT id, ticker, note, rating, last_edit_epoch
    return {
        "id": row[0],
        "ticker": row[1],
        "note": row[2],
        "rating": row[3],
        "last_edit_epoch": row[4],
    }


@app.get("/api/health")
def health():
    _auto_recover_run_status()
    return {
        "ok": True,
        "provider": QUOTE_PROVIDER_NAME,
        "provider_ready": PROVIDER.is_ready(),
        "alpha_key_present": bool(
            os.getenv("ALPHA_VANTAGE_KEY")
        ),  # backward-compat status hint
        "market_tz": MARKET_TZ,
        "cooldown_minutes": COOLDOWN_MINUTES,
    }


@app.get("/api/symbols_by_group")
def symbols_by_group():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, ticker, note, rating, last_edit_epoch FROM symbols WHERE group_id=? ORDER BY ticker",
            (WATCH_ID,),
        )
        watch = [_row_to_symbol(r) for r in c.fetchall()]
        c.execute(
            "SELECT id, ticker, note, rating, last_edit_epoch FROM symbols WHERE group_id=? ORDER BY ticker",
            (ARCHIVE_ID,),
        )
        archived = [_row_to_symbol(r) for r in c.fetchall()]
    return {"watch": watch, "archived": archived}


@app.get("/api/symbols")
def list_symbols(
    q: Optional[str] = None, scope: Optional[str] = "watch", min_rating: int = 0
):
    """
    scope: 'watch' | 'archived' | 'all'
    """
    scope = (scope or "watch").lower()
    groups = {
        "watch": [WATCH_ID],
        "archived": [ARCHIVE_ID],
        "all": [WATCH_ID, ARCHIVE_ID],
    }.get(scope, [WATCH_ID])

    query = f"SELECT id, ticker, note, rating, last_edit_epoch FROM symbols WHERE group_id IN ({','.join('?'*len(groups))})"
    params: list[Any] = list(groups)

    if q:
        like = f"%{(q or '').upper().strip()}%"
        query += " AND UPPER(ticker) LIKE ?"
        params.append(like)
    if isinstance(min_rating, int) and min_rating > 0:
        query += " AND rating >= ?"
        params.append(min_rating)

    query += " ORDER BY ticker"
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
    return [_row_to_symbol(r) for r in rows]


@app.post("/api/symbols")
async def add_symbol(request: Request):
    data = await request.json()
    ticker = data["ticker"].upper().strip()
    group_name = (data.get("group") or "watch").lower()
    group_id = WATCH_ID if group_name != "archived" else ARCHIVE_ID

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO symbols (ticker, note, group_id, rating, last_edit_epoch) VALUES (?, ?, ?, ?, ?)",
            (ticker, "", group_id, 0, None),
        )
        symbol_id = c.lastrowid

        # Seed default earnings reminder if configured
        try:
            if (
                isinstance(EARNINGS_NOTIFY_DEFAULT_DAYS, int)
                and EARNINGS_NOTIFY_DEFAULT_DAYS >= 0
            ):
                c.execute(
                    "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                    (symbol_id, "earnings_days", int(EARNINGS_NOTIFY_DEFAULT_DAYS)),
                )
        except Exception as e:
            log.warning(f"Failed to seed default earnings alert for {ticker}: {e}")

        conn.commit()

    return {"status": "ok"}


@app.post("/api/symbols/{symbol_id}/move")
async def move_symbol(symbol_id: int, request: Request):
    data = await request.json()
    target = (data.get("group") or "").lower()
    if target not in ("watch", "archived"):
        return JSONResponse({"error": "invalid_group"}, status_code=400)
    target_id = WATCH_ID if target == "watch" else ARCHIVE_ID
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE symbols SET group_id=? WHERE id=?", (target_id, symbol_id))
        conn.commit()
    return {"status": "ok", "moved_to": target}


@app.post("/api/note/{symbol_id}")
async def update_note(symbol_id: int, request: Request):
    data = await request.json()
    note = data.get("note", "")
    now_epoch = int(time.time())
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE symbols SET note=?, last_edit_epoch=? WHERE id=?",
            (note, now_epoch, symbol_id),
        )
        conn.commit()
    return {"status": "saved", "last_edit_epoch": now_epoch}


@app.post("/api/rating/{symbol_id}")
async def update_rating(symbol_id: int, request: Request):
    data = await request.json()
    try:
        rating = int(data.get("rating", 0))
    except Exception:
        return JSONResponse({"error": "invalid_rating"}, status_code=400)
    if (rating < 0) or (rating > 5):
        return JSONResponse({"error": "invalid_rating"}, status_code=400)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE symbols SET rating=? WHERE id=?", (rating, symbol_id))
        conn.commit()
    return {"status": "ok", "rating": rating}


@app.get("/api/alerts/{symbol_id}")
def get_alerts(symbol_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, type, value FROM alerts WHERE symbol_id=?", (symbol_id,))
        rows = c.fetchall()
    return [{"id": r[0], "type": r[1], "value": r[2]} for r in rows]


@app.post("/api/alerts/{symbol_id}")
async def save_alerts(symbol_id: int, request: Request):
    data = await request.json()
    above = data.get("above")
    below = data.get("below")
    pct_drop = data.get("pct_drop", [])
    pct_jump = data.get("pct_jump", [])
    # Optional: explicit earnings-days from UI; if omitted we keep a default
    earn_days = data.get("earn_days")

    with get_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM alerts WHERE symbol_id=?", (symbol_id,))

        if isinstance(above, (int, float)):
            c.execute(
                "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                (symbol_id, "above", float(above)),
            )
        if isinstance(below, (int, float)):
            c.execute(
                "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                (symbol_id, "below", float(below)),
            )
        for p in pct_drop:
            c.execute(
                "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                (symbol_id, "pct_drop", float(p)),
            )
        for p in pct_jump:
            c.execute(
                "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                (symbol_id, "pct_jump", float(p)),
            )

        if isinstance(earn_days, (int, float)):
            c.execute(
                "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                (symbol_id, "earnings_days", int(earn_days)),
            )
        else:
            if (
                isinstance(EARNINGS_NOTIFY_DEFAULT_DAYS, int)
                and EARNINGS_NOTIFY_DEFAULT_DAYS >= 0
            ):
                c.execute(
                    "INSERT INTO alerts (symbol_id, type, value) VALUES (?, ?, ?)",
                    (symbol_id, "earnings_days", int(EARNINGS_NOTIFY_DEFAULT_DAYS)),
                )

        # stamp last edit time when alert settings change
        now_epoch = int(time.time())
        c.execute(
            "UPDATE symbols SET last_edit_epoch=? WHERE id=?", (now_epoch, symbol_id)
        )
        conn.commit()

    return {"status": "saved", "last_edit_epoch": now_epoch}


@app.get("/api/quote/{symbol_id}")
def get_quote(symbol_id: int):
    # resolve ticker
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT ticker FROM symbols WHERE id=?", (symbol_id,))
        r = c.fetchone()
        if not r:
            return JSONResponse(
                {"error": "not_found", "error_detail": "Symbol not found"},
                status_code=404,
            )
        ticker = r[0]

        # pull cached row (scheduler writes)
        c.execute(
            """
            SELECT
                last_check_epoch, last_check_note, window_open,
                price, prev_close, open, high, low, volume, latest_trading_day,
                change, change_percent,
                market_cap, pe_ratio, dividend_yield_percent,
                fifty_two_week_high, fifty_two_week_low, quarterly_dividend_amount,
                description, next_earning_day
            FROM symbol_state
            WHERE symbol_id=?
            """,
            (symbol_id,),
        )
        st = c.fetchone()

    payload: Dict[str, Any] = {
        "symbol": ticker,
        "source": "cache_only",
        "server_tz": MARKET_TZ,
        "cooldown_minutes": COOLDOWN_MINUTES,
    }

    if not st:
        payload.update(
            {
                "last_check_epoch": None,
                "last_check_note": None,
                "window_open": None,
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
                "description": None,
                "next_earning_day": None,
            }
        )
        return payload

    (
        last_check_epoch,
        last_check_note,
        window_open,
        price,
        prev_close,
        open_,
        high,
        low,
        volume,
        ltd,
        change,
        change_pct,
        mcap,
        pe,
        div_yld,
        wk52h,
        wk52l,
        qdiv,
        description,
        next_earning_day,
    ) = st

    # derive change fields if scheduler left them null
    if change is None and price is not None and prev_close is not None:
        change = price - prev_close
    if change_pct is None and change is not None and prev_close not in (None, 0):
        change_pct = f"{(change / prev_close) * 100:.2f}%"

    payload.update(
        {
            "last_check_epoch": last_check_epoch,
            "last_check_note": last_check_note,
            "window_open": bool(window_open) if window_open is not None else None,
            "price": price,
            "prev_close": prev_close,
            "open": open_,
            "high": high,
            "low": low,
            "volume": volume,
            "latest_trading_day": ltd,
            "change": change,
            "change_percent": change_pct,
            "market_cap": mcap,
            "pe_ratio": pe,
            "dividend_yield_percent": div_yld,
            "fifty_two_week_high": wk52h,
            "fifty_two_week_low": wk52l,
            "quarterly_dividend_amount": qdiv,
            "description": description,
            "next_earning_day": next_earning_day,
        }
    )
    return payload


@app.get("/api/quote_by_ticker/{ticker}")
def quote_by_ticker_cached(ticker: str):
    """
    Return the latest cached quote for this ticker, NEVER hitting providers.
    This makes all fetches occur only in the scheduler at the specified times.
    """
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT id FROM symbols WHERE UPPER(ticker)=UPPER(?)", (ticker,))
        row = cur.fetchone()
        if not row:
            return JSONResponse({"error": "unknown_symbol"}, status_code=404)
        symbol_id = row["id"]
    return get_quote(symbol_id)


@app.get("/api/last_update")
def last_update():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT MAX(last_check_epoch) FROM symbol_state")
        row = c.fetchone()

    epoch = row[0] if row and row[0] else None
    if epoch is None:
        return {"epoch": None, "text": "—", "timezone": MARKET_TZ}

    dt = datetime.fromtimestamp(epoch, ZoneInfo(MARKET_TZ))
    return {
        "epoch": epoch,
        "text": dt.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "timezone": MARKET_TZ,
    }


@app.delete("/api/symbols/{symbol_id}")
def delete_symbol(symbol_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "DELETE FROM alert_state WHERE alert_id IN (SELECT id FROM alerts WHERE symbol_id=?)",
            (symbol_id,),
        )
        c.execute("DELETE FROM alerts WHERE symbol_id=?", (symbol_id,))
        c.execute("DELETE FROM symbol_state WHERE symbol_id=?", (symbol_id,))
        c.execute("DELETE FROM symbols WHERE id=?", (symbol_id,))
        conn.commit()
    return {"status": "deleted"}


@app.get("/api/symbols/{symbol_id}")
def get_symbol(symbol_id: int):
    """
    Returns core symbol fields PLUS latest company description and next_earning_day (from symbol_state),
    so the frontend can render immediately when selecting a symbol.
    """
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT s.id, s.ticker, s.note, s.rating, s.last_edit_epoch,
                   st.description, st.next_earning_day
            FROM symbols s
            LEFT JOIN symbol_state st ON st.symbol_id = s.id
            WHERE s.id = ?
            """,
            (symbol_id,),
        )
        row = c.fetchone()
    if not row:
        return JSONResponse(
            {"error": "not_found", "error_detail": "Symbol not found"}, status_code=404
        )
    return {
        "id": row[0],
        "ticker": row[1],
        "note": row[2],
        "rating": row[3],
        "last_edit_epoch": row[4],
        "description": row[5] if len(row) > 5 else None,
        "next_earning_day": row[6] if len(row) > 6 else None,
    }


@app.get("/api/run_status")
def api_run_status():
    _auto_recover_run_status()
    st = get_run_status()

    def _fmt(epoch: Optional[int]) -> Optional[str]:
        if not epoch:
            return None
        return datetime.fromtimestamp(epoch, ZoneInfo(MARKET_TZ)).strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )

    st["started_text"] = _fmt(st.get("started_epoch"))
    st["finished_text"] = _fmt(st.get("finished_epoch"))
    return st


# ========== On-demand update endpoints ==========


@app.post("/api/update_symbol/{symbol_id}")
def api_update_symbol(symbol_id: int):
    """Update a single symbol on demand (works for both watch and archived)."""
    _auto_recover_run_status()

    # Resolve ticker
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT ticker FROM symbols WHERE id=?", (symbol_id,))
        row = c.fetchone()
    if not row:
        return JSONResponse({"status": "error", "error": "not_found"}, status_code=404)

    ticker = row[0]
    et_now = now_et()
    window_ok = is_trading_window(et_now)
    note_prefix = f"{et_now.strftime('%Y-%m-%d %H:%M:%S %Z')} | Window: {'OPEN' if window_ok else 'CLOSED'}"

    try:
        full = PROVIDER.get_full(ticker)
    except Exception as e:
        log.warning(f"Provider exception for {ticker} (single update): {e}")
        full = {"error": "network_error"}

    err = (full or {}).get("error")
    if err:
        upsert_symbol_state_full(
            symbol_id, window_ok, f"{note_prefix} | {err}", data=None
        )
        return {"status": "error", "error": err}

    # Persist state
    upsert_symbol_state_full(
        symbol_id, window_ok, f"{note_prefix} | Price check ok", data=full
    )

    # Evaluate alerts immediately for this symbol
    notified = _evaluate_and_notify(symbol_id, ticker, full, int(time.time()))

    return {"status": "ok", "notified": notified}


@app.post("/api/update_all")
def api_update_all():
    """Trigger a background bulk update for WATCH list only (archived excluded)."""
    _auto_recover_run_status()
    if _is_run_already_running():
        return {"status": "already_running"}
    try:
        threading.Thread(target=check_alerts, daemon=True).start()
        return {"status": "started"}
    except Exception as e:
        log.error(f"Failed to start bulk update: {e}")
        return JSONResponse(
            {"status": "error", "error": "start_failed"}, status_code=500
        )


@app.post("/api/run_status/reset")
def api_run_status_reset():
    _force_finish_run_status("manual_reset", "Manually reset by user")
    return {"status": "ok"}


# =========================
# Startup Hook
# =========================


@app.on_event("startup")
def _on_startup_recover():
    _recover_stuck_run_status()
