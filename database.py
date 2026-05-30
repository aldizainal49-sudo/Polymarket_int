"""
database.py
===========
SQLite persistence layer.

Tables
------
1. markets_scanned   - every market we looked at (full snapshot per scan).
2. weather_forecasts - forecast values fetched per market/city/date.
3. trade_decisions   - the decision (TRADE_* / SKIP_*) plus all the inputs.
4. orders            - orders we (would) place; DRY_RUN orders are flagged.
5. positions         - open/closed positions and their P&L.
6. daily_stats       - per-day counters (scanned, traded, skipped, pnl, loss).
7. rejected_markets  - convenience view of skips with the reason.

The DB is opened with ``check_same_thread=False`` and a short busy timeout so
the single-process bot can use it safely. All writes are best-effort and
wrapped so a logging failure never crashes the trading loop.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils import get_logger, utcnow

log = get_logger("database")


SCHEMA = """
CREATE TABLE IF NOT EXISTS markets_scanned (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT,
    token_id        TEXT,
    title           TEXT,
    city            TEXT,
    date            TEXT,
    market_type     TEXT,
    threshold       REAL,
    threshold_unit  TEXT,
    outcome         TEXT,
    forecast_value  REAL,
    temp_distance   REAL,
    bot_probability REAL,
    confidence_score REAL,
    market_price    REAL,
    spread          REAL,
    liquidity       REAL,
    decision        TEXT,
    skip_reason     TEXT,
    timestamp       TEXT
);

CREATE TABLE IF NOT EXISTS weather_forecasts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT,
    city            TEXT,
    date            TEXT,
    provider        TEXT,
    daily_high_c    REAL,
    daily_low_c     REAL,
    hourly_json     TEXT,
    forecast_update_time TEXT,
    data_quality    REAL,
    timestamp       TEXT
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT,
    token_id        TEXT,
    title           TEXT,
    outcome         TEXT,
    decision        TEXT,
    skip_reason     TEXT,
    bot_probability REAL,
    confidence_score REAL,
    market_price    REAL,
    forecast_value  REAL,
    threshold       REAL,
    temp_distance   REAL,
    components_json TEXT,
    timestamp       TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT,
    token_id        TEXT,
    title           TEXT,
    outcome         TEXT,
    side            TEXT,
    price           REAL,
    size            REAL,
    notional_usd    REAL,
    order_type      TEXT,
    status          TEXT,
    dry_run         INTEGER,
    exchange_order_id TEXT,
    error           TEXT,
    timestamp       TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT,
    token_id        TEXT,
    title           TEXT,
    outcome         TEXT,
    entry_price     REAL,
    size            REAL,
    notional_usd    REAL,
    status          TEXT,           -- OPEN | CLOSED
    exit_price      REAL,
    realized_pnl    REAL,
    dry_run         INTEGER,
    opened_at       TEXT,
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    day             TEXT PRIMARY KEY,   -- YYYY-MM-DD (UTC)
    markets_scanned INTEGER DEFAULT 0,
    trades_opened   INTEGER DEFAULT 0,
    skips           INTEGER DEFAULT 0,
    realized_pnl    REAL DEFAULT 0.0,
    daily_loss      REAL DEFAULT 0.0,
    trading_halted  INTEGER DEFAULT 0,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS rejected_markets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id       TEXT,
    title           TEXT,
    city            TEXT,
    date            TEXT,
    market_type     TEXT,
    threshold       REAL,
    outcome         TEXT,
    forecast_value  REAL,
    temp_distance   REAL,
    confidence_score REAL,
    market_price    REAL,
    skip_reason     TEXT,
    timestamp       TEXT
);

CREATE INDEX IF NOT EXISTS idx_markets_scanned_mid ON markets_scanned(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_rejected_reason ON rejected_markets(skip_reason);
"""


class Database:
    """Thin, defensive wrapper around a single SQLite connection."""

    def __init__(self, path: str = "weather_bot.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------ #
    # Generic helpers                                                    #
    # ------------------------------------------------------------------ #
    def _execute(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Cursor]:
        try:
            with self._lock:
                cur = self.conn.execute(sql, params)
                self.conn.commit()
                return cur
        except sqlite3.Error as exc:
            log.error("DB error on %s: %s", sql.split()[0], exc)
            return None

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        try:
            with self._lock:
                cur = self.conn.execute(sql, params)
                return cur.fetchall()
        except sqlite3.Error as exc:
            log.error("DB query error: %s", exc)
            return []

    # ------------------------------------------------------------------ #
    # markets_scanned                                                    #
    # ------------------------------------------------------------------ #
    def record_market_scan(self, data: Dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO markets_scanned (
                market_id, token_id, title, city, date, market_type, threshold,
                threshold_unit, outcome, forecast_value, temp_distance,
                bot_probability, confidence_score, market_price, spread,
                liquidity, decision, skip_reason, timestamp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("market_id"),
                data.get("token_id"),
                data.get("title"),
                data.get("city"),
                data.get("date"),
                data.get("market_type"),
                data.get("threshold"),
                data.get("threshold_unit"),
                data.get("outcome"),
                data.get("forecast_value"),
                data.get("temp_distance"),
                data.get("bot_probability"),
                data.get("confidence_score"),
                data.get("market_price"),
                data.get("spread"),
                data.get("liquidity"),
                data.get("decision"),
                data.get("skip_reason"),
                data.get("timestamp") or utcnow().isoformat(),
            ),
        )

    # ------------------------------------------------------------------ #
    # weather_forecasts                                                  #
    # ------------------------------------------------------------------ #
    def record_forecast(self, data: Dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO weather_forecasts (
                market_id, city, date, provider, daily_high_c, daily_low_c,
                hourly_json, forecast_update_time, data_quality, timestamp
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("market_id"),
                data.get("city"),
                data.get("date"),
                data.get("provider"),
                data.get("daily_high_c"),
                data.get("daily_low_c"),
                data.get("hourly_json"),
                data.get("forecast_update_time"),
                data.get("data_quality"),
                data.get("timestamp") or utcnow().isoformat(),
            ),
        )

    # ------------------------------------------------------------------ #
    # trade_decisions                                                    #
    # ------------------------------------------------------------------ #
    def record_decision(self, data: Dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO trade_decisions (
                market_id, token_id, title, outcome, decision, skip_reason,
                bot_probability, confidence_score, market_price, forecast_value,
                threshold, temp_distance, components_json, timestamp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("market_id"),
                data.get("token_id"),
                data.get("title"),
                data.get("outcome"),
                data.get("decision"),
                data.get("skip_reason"),
                data.get("bot_probability"),
                data.get("confidence_score"),
                data.get("market_price"),
                data.get("forecast_value"),
                data.get("threshold"),
                data.get("temp_distance"),
                data.get("components_json"),
                data.get("timestamp") or utcnow().isoformat(),
            ),
        )

    # ------------------------------------------------------------------ #
    # rejected_markets                                                   #
    # ------------------------------------------------------------------ #
    def record_rejection(self, data: Dict[str, Any]) -> None:
        self._execute(
            """
            INSERT INTO rejected_markets (
                market_id, title, city, date, market_type, threshold, outcome,
                forecast_value, temp_distance, confidence_score, market_price,
                skip_reason, timestamp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("market_id"),
                data.get("title"),
                data.get("city"),
                data.get("date"),
                data.get("market_type"),
                data.get("threshold"),
                data.get("outcome"),
                data.get("forecast_value"),
                data.get("temp_distance"),
                data.get("confidence_score"),
                data.get("market_price"),
                data.get("skip_reason"),
                data.get("timestamp") or utcnow().isoformat(),
            ),
        )

    def get_rejections(self, limit: int = 50) -> List[sqlite3.Row]:
        return self._query(
            "SELECT * FROM rejected_markets ORDER BY id DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------------ #
    # orders                                                             #
    # ------------------------------------------------------------------ #
    def record_order(self, data: Dict[str, Any]) -> int:
        cur = self._execute(
            """
            INSERT INTO orders (
                market_id, token_id, title, outcome, side, price, size,
                notional_usd, order_type, status, dry_run, exchange_order_id,
                error, timestamp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("market_id"),
                data.get("token_id"),
                data.get("title"),
                data.get("outcome"),
                data.get("side", "BUY"),
                data.get("price"),
                data.get("size"),
                data.get("notional_usd"),
                data.get("order_type", "LIMIT"),
                data.get("status", "SIMULATED"),
                1 if data.get("dry_run", True) else 0,
                data.get("exchange_order_id"),
                data.get("error"),
                data.get("timestamp") or utcnow().isoformat(),
            ),
        )
        return cur.lastrowid if cur else -1

    def get_orders(self, limit: int = 50) -> List[sqlite3.Row]:
        return self._query("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ #
    # positions                                                          #
    # ------------------------------------------------------------------ #
    def open_position(self, data: Dict[str, Any]) -> int:
        cur = self._execute(
            """
            INSERT INTO positions (
                market_id, token_id, title, outcome, entry_price, size,
                notional_usd, status, dry_run, opened_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("market_id"),
                data.get("token_id"),
                data.get("title"),
                data.get("outcome"),
                data.get("entry_price"),
                data.get("size"),
                data.get("notional_usd"),
                "OPEN",
                1 if data.get("dry_run", True) else 0,
                data.get("opened_at") or utcnow().isoformat(),
            ),
        )
        return cur.lastrowid if cur else -1

    def close_position(self, position_id: int, exit_price: float, realized_pnl: float) -> None:
        self._execute(
            """
            UPDATE positions
               SET status='CLOSED', exit_price=?, realized_pnl=?, closed_at=?
             WHERE id=?
            """,
            (exit_price, realized_pnl, utcnow().isoformat(), position_id),
        )

    def get_open_positions(self) -> List[sqlite3.Row]:
        return self._query("SELECT * FROM positions WHERE status='OPEN' ORDER BY id DESC")

    def count_open_positions(self) -> int:
        rows = self._query("SELECT COUNT(*) AS c FROM positions WHERE status='OPEN'")
        return int(rows[0]["c"]) if rows else 0

    def has_open_position_for_market(self, market_id: str) -> bool:
        rows = self._query(
            "SELECT 1 FROM positions WHERE market_id=? AND status='OPEN' LIMIT 1",
            (market_id,),
        )
        return bool(rows)

    def get_all_positions(self, limit: int = 100) -> List[sqlite3.Row]:
        return self._query("SELECT * FROM positions ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ #
    # daily_stats                                                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_today(self) -> None:
        day = self._today()
        self._execute(
            """
            INSERT OR IGNORE INTO daily_stats (day, updated_at) VALUES (?, ?)
            """,
            (day, utcnow().isoformat()),
        )

    def bump_daily(self, field: str, amount: float = 1) -> None:
        """Increment one of the integer/real counters for today.

        ``field`` must be one of the known column names (defensive whitelist).
        """
        allowed = {"markets_scanned", "trades_opened", "skips", "realized_pnl"}
        if field not in allowed:
            log.error("bump_daily called with invalid field %r", field)
            return
        self._ensure_today()
        self._execute(
            f"UPDATE daily_stats SET {field}={field}+?, updated_at=? WHERE day=?",
            (amount, utcnow().isoformat(), self._today()),
        )

    def add_realized_pnl(self, pnl: float) -> None:
        """Record realized P&L for the day and track loss separately."""
        self._ensure_today()
        self._execute(
            "UPDATE daily_stats SET realized_pnl=realized_pnl+?, updated_at=? WHERE day=?",
            (pnl, utcnow().isoformat(), self._today()),
        )
        if pnl < 0:
            self._execute(
                "UPDATE daily_stats SET daily_loss=daily_loss+?, updated_at=? WHERE day=?",
                (abs(pnl), utcnow().isoformat(), self._today()),
            )

    def get_daily_stats(self, day: Optional[str] = None) -> Optional[sqlite3.Row]:
        day = day or self._today()
        rows = self._query("SELECT * FROM daily_stats WHERE day=?", (day,))
        return rows[0] if rows else None

    def get_daily_loss(self, day: Optional[str] = None) -> float:
        row = self.get_daily_stats(day)
        return float(row["daily_loss"]) if row and row["daily_loss"] is not None else 0.0

    def set_trading_halted(self, halted: bool, day: Optional[str] = None) -> None:
        day = day or self._today()
        self._ensure_today()
        self._execute(
            "UPDATE daily_stats SET trading_halted=?, updated_at=? WHERE day=?",
            (1 if halted else 0, utcnow().isoformat(), day),
        )

    def is_trading_halted(self, day: Optional[str] = None) -> bool:
        row = self.get_daily_stats(day)
        return bool(row["trading_halted"]) if row else False

    # ------------------------------------------------------------------ #
    # P&L summary                                                        #
    # ------------------------------------------------------------------ #
    def pnl_summary(self) -> Dict[str, Any]:
        rows = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl),0) AS total "
            "FROM positions WHERE status='CLOSED'"
        )
        closed_n = int(rows[0]["n"]) if rows else 0
        total = float(rows[0]["total"]) if rows else 0.0

        wins = self._query(
            "SELECT COUNT(*) AS n FROM positions WHERE status='CLOSED' AND realized_pnl > 0"
        )
        losses = self._query(
            "SELECT COUNT(*) AS n FROM positions WHERE status='CLOSED' AND realized_pnl < 0"
        )
        open_rows = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(notional_usd),0) AS notional "
            "FROM positions WHERE status='OPEN'"
        )
        return {
            "closed_trades": closed_n,
            "wins": int(wins[0]["n"]) if wins else 0,
            "losses": int(losses[0]["n"]) if losses else 0,
            "total_realized_pnl": total,
            "open_positions": int(open_rows[0]["n"]) if open_rows else 0,
            "open_notional": float(open_rows[0]["notional"]) if open_rows else 0.0,
        }

    def close(self) -> None:
        try:
            with self._lock:
                self.conn.close()
        except sqlite3.Error:
            pass
