#!/usr/bin/env python3
"""
main.py
=======
CLI entry point and orchestration for the Polymarket weather/temperature bot.

Commands
--------
    python main.py scan        One scan pass; prints what it would do. No orders.
    python main.py dry-run     Continuous loop in DRY_RUN (simulated orders).
    python main.py live        Continuous loop; sends REAL orders ONLY if
                               DRY_RUN=false AND LIVE_TRADING=true in .env.
    python main.py status      Show config summary + today's stats.
    python main.py positions   List open/closed positions.
    python main.py pnl         Show realized/unrealized P&L summary.
    python main.py rejected    Show recently rejected markets + reasons.
    python main.py logs         Tail the log file.

The bot is deliberately conservative: in a normal scan it is expected to SKIP
almost everything. If nothing qualifies, it simply waits for the next pass.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import List, Optional

from config import get_config
from database import Database
from orderbook_analyzer import OrderBookAnalyzer
from polymarket_client import PolymarketClient
from risk_manager import RiskManager
from temperature_predictor import Prediction, TemperaturePredictor
from trader import Trader
from utils import (
    SKIP_NO_ORDERBOOK,
    SKIP_NOT_TEMPERATURE,
    TRADE_HIGH_CONFIDENCE,
    fmt_money,
    fmt_pct,
    geometric_mean,
    get_logger,
    setup_logging,
    utcnow,
)
from weather_data_client import WeatherDataClient
from weather_market_scanner import WeatherMarketScanner

try:
    from tabulate import tabulate  # type: ignore
except Exception:  # pragma: no cover - tabulate optional
    tabulate = None


@dataclass
class ScanCounters:
    fetched: int = 0
    temperature: int = 0
    parsed_ok: int = 0
    evaluated: int = 0
    trades: int = 0
    skips: int = 0

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} temp_markets={self.temperature} "
            f"parsed_ok={self.parsed_ok} evaluated={self.evaluated} "
            f"TRADES={self.trades} skips={self.skips}"
        )


class WeatherBot:
    """Owns the long-lived components and runs the scan pipeline."""

    def __init__(self) -> None:
        self.config = get_config()
        self.log = setup_logging(self.config.log_path)
        self.db = Database(self.config.database_path)
        self.client = PolymarketClient(self.config)
        self.scanner = WeatherMarketScanner(self.config)
        self.weather = WeatherDataClient(self.config)
        self.predictor = TemperaturePredictor(self.config)
        self.book_analyzer = OrderBookAnalyzer(self.config)
        self.risk = RiskManager(self.config, self.db)
        self.trader = Trader(self.config, self.db, self.client)

    # ------------------------------------------------------------------ #
    # One full scan pass                                                 #
    # ------------------------------------------------------------------ #
    def scan_once(self) -> ScanCounters:
        counters = ScanCounters()
        self.log.info("=== Scan pass started (mode=%s) ===", self.config.mode_label())

        # Respect the daily circuit breaker before doing anything expensive.
        if self.risk.trading_halted():
            self.log.warning("Trading halted for today (daily loss limit). Scanning only, no trades.")

        raw_markets = self.client.get_active_markets()
        counters.fetched = len(raw_markets)

        temp_markets = self.scanner.filter_markets(raw_markets)
        counters.temperature = len(temp_markets)

        for raw in temp_markets:
            try:
                self._process_market(raw, counters)
            except Exception as exc:  # never let one market kill the loop
                self.log.exception("Error processing a market: %s", exc)

        self.db.bump_daily("markets_scanned", counters.temperature)
        self.log.info("=== Scan pass complete: %s ===", counters.summary())
        return counters

    # ------------------------------------------------------------------ #
    # Per-market pipeline                                                #
    # ------------------------------------------------------------------ #
    def _process_market(self, raw: dict, counters: ScanCounters) -> None:
        pm = self.scanner.parse_market(raw)

        # Always log the scan row (even skips), so the DB is a full audit trail.
        scan_row = {
            "market_id": pm.market_id,
            "title": pm.title,
            "city": pm.city,
            "date": pm.date,
            "market_type": pm.market_type,
            "threshold": pm.threshold_raw,
            "threshold_unit": pm.threshold_unit,
            "timestamp": utcnow().isoformat(),
        }

        if not pm.parse_ok:
            self._finalize_skip(pm, None, None, "SKIP_PARSE_FAILED", scan_row, counters)
            return
        counters.parsed_ok += 1

        # --- Weather forecast -----------------------------------------
        forecast = self.weather.get_forecast(pm.city, pm.date)
        self.db.record_forecast(
            {
                "market_id": pm.market_id,
                "city": pm.city,
                "date": pm.date,
                "provider": forecast.provider,
                "daily_high_c": forecast.daily_high_c,
                "daily_low_c": forecast.daily_low_c,
                "hourly_json": forecast.hourly_json(),
                "forecast_update_time": forecast.forecast_update_time,
                "data_quality": forecast.data_quality,
            }
        )

        # --- Prediction (forecast-only confidence) --------------------
        prediction = self.predictor.predict(pm, forecast)

        # --- Choose the token id for the predicted outcome ------------
        token_id = self._token_for_outcome(pm, prediction.target_outcome)

        # --- Order book + microstructure ------------------------------
        metrics = None
        if prediction.is_trade and token_id:
            book = self.client.get_order_book(token_id)
            metrics = self.book_analyzer.analyze(book)
            if not metrics.ok:
                self._finalize_skip(pm, prediction, metrics, SKIP_NO_ORDERBOOK, scan_row, counters)
                return
            # Fold liquidity/spread into the confidence and RECOMPUTE it.
            prediction.components["liquidity_score"] = metrics.liquidity_score
            prediction.components["spread_score"] = metrics.spread_score
            prediction.confidence_score = round(
                geometric_mean(list(prediction.components.values())), 4
            )
            # If the recomputed confidence now fails, downgrade to a skip.
            if prediction.confidence_score < self.config.min_confidence_score:
                prediction.decision = "SKIP_LOW_CONFIDENCE"
                prediction.skip_reason = "SKIP_LOW_CONFIDENCE"

        counters.evaluated += 1

        # --- If not a trade, record the skip --------------------------
        if not prediction.is_trade:
            self._finalize_skip(
                pm, prediction, metrics, prediction.skip_reason or "SKIP_LOW_CONFIDENCE",
                scan_row, counters,
            )
            return

        # --- Risk evaluation ------------------------------------------
        risk = self.risk.evaluate(prediction, metrics, pm.market_id)
        if not risk.approved:
            self._finalize_skip(
                pm, prediction, metrics, risk.skip_label or "SKIP_RISK_LIMIT", scan_row, counters
            )
            return

        # --- Execute (simulate or live) -------------------------------
        result = self.trader.execute(
            prediction, risk, metrics, token_id, pm.market_id, pm.title
        )
        self._finalize_trade(pm, prediction, metrics, result, scan_row, counters)

    # ------------------------------------------------------------------ #
    # Outcome -> token id                                                #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _token_for_outcome(pm, outcome: Optional[str]) -> Optional[str]:
        if not outcome:
            return None
        for o in pm.outcomes:
            if o.label.upper() == outcome.upper():
                return o.token_id
        # Fall back to the first outcome if labels don't match cleanly.
        return pm.outcomes[0].token_id if pm.outcomes else None

    # ------------------------------------------------------------------ #
    # Recording helpers                                                  #
    # ------------------------------------------------------------------ #
    def _finalize_skip(self, pm, prediction, metrics, skip_label, scan_row, counters) -> None:
        counters.skips += 1
        self.db.bump_daily("skips", 1)

        market_price = metrics.market_price if metrics else None
        spread = metrics.spread_percent if metrics else None
        liquidity = metrics.liquidity_usd if metrics else None
        bot_prob = prediction.bot_probability if prediction else None
        conf = prediction.confidence_score if prediction else None
        forecast_value = prediction.forecast_value_c if prediction else None
        temp_dist = prediction.temp_distance_c if prediction else None
        outcome = prediction.target_outcome if prediction else None

        scan_row.update(
            {
                "token_id": self._token_for_outcome(pm, outcome) if outcome else None,
                "outcome": outcome,
                "forecast_value": forecast_value,
                "temp_distance": temp_dist,
                "bot_probability": bot_prob,
                "confidence_score": conf,
                "market_price": market_price,
                "spread": spread,
                "liquidity": liquidity,
                "decision": skip_label,
                "skip_reason": skip_label,
            }
        )
        self.db.record_market_scan(scan_row)
        self.db.record_decision(
            {
                "market_id": pm.market_id,
                "token_id": scan_row.get("token_id"),
                "title": pm.title,
                "outcome": outcome,
                "decision": skip_label,
                "skip_reason": skip_label,
                "bot_probability": bot_prob,
                "confidence_score": conf,
                "market_price": market_price,
                "forecast_value": forecast_value,
                "threshold": pm.threshold_c,
                "temp_distance": temp_dist,
                "components_json": prediction.components_json() if prediction else "{}",
            }
        )
        self.db.record_rejection(
            {
                "market_id": pm.market_id,
                "title": pm.title,
                "city": pm.city,
                "date": pm.date,
                "market_type": pm.market_type,
                "threshold": pm.threshold_raw,
                "outcome": outcome,
                "forecast_value": forecast_value,
                "temp_distance": temp_dist,
                "confidence_score": conf,
                "market_price": market_price,
                "skip_reason": skip_label,
            }
        )
        rationale = prediction.rationale if prediction else "parse failed"
        self.log.info("SKIP [%s] %s | %s", skip_label, (pm.title or "")[:70], rationale)

    def _finalize_trade(self, pm, prediction, metrics, result, scan_row, counters) -> None:
        counters.trades += 1
        decision = TRADE_HIGH_CONFIDENCE
        scan_row.update(
            {
                "token_id": self._token_for_outcome(pm, prediction.target_outcome),
                "outcome": prediction.target_outcome,
                "forecast_value": prediction.forecast_value_c,
                "temp_distance": prediction.temp_distance_c,
                "bot_probability": prediction.bot_probability,
                "confidence_score": prediction.confidence_score,
                "market_price": metrics.market_price if metrics else None,
                "spread": metrics.spread_percent if metrics else None,
                "liquidity": metrics.liquidity_usd if metrics else None,
                "decision": decision,
                "skip_reason": None,
            }
        )
        self.db.record_market_scan(scan_row)
        self.db.record_decision(
            {
                "market_id": pm.market_id,
                "token_id": scan_row.get("token_id"),
                "title": pm.title,
                "outcome": prediction.target_outcome,
                "decision": decision,
                "skip_reason": None,
                "bot_probability": prediction.bot_probability,
                "confidence_score": prediction.confidence_score,
                "market_price": metrics.market_price if metrics else None,
                "forecast_value": prediction.forecast_value_c,
                "threshold": pm.threshold_c,
                "temp_distance": prediction.temp_distance_c,
                "components_json": prediction.components_json(),
            }
        )
        tag = "SIMULATED" if result.simulated else ("PLACED" if result.placed else "NOT-PLACED")
        self.log.info(
            "TRADE [%s] %s | buy %s @ %.3f x %.2f ($%.2f) | conf=%.3f prob=%.3f | %s",
            tag,
            (pm.title or "")[:60],
            prediction.target_outcome,
            result.price or 0.0,
            result.size or 0.0,
            result.notional_usd or 0.0,
            prediction.confidence_score,
            prediction.bot_probability,
            result.reason,
        )

    # ------------------------------------------------------------------ #
    # Loop                                                               #
    # ------------------------------------------------------------------ #
    def run_loop(self) -> None:
        interval = max(30, self.config.scan_interval_seconds)
        self.log.info(
            "Starting %s loop; interval=%ss. The bot will stay idle when nothing qualifies.",
            self.config.mode_label(),
            interval,
        )
        while True:
            try:
                self.scan_once()
            except KeyboardInterrupt:
                self.log.info("Interrupted by user; shutting down.")
                break
            except Exception as exc:
                # Conservative error handling: log and wait, never hammer.
                self.log.exception("Scan pass failed: %s", exc)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                self.log.info("Interrupted during sleep; shutting down.")
                break

    def close(self) -> None:
        self.db.close()


# ===================================================================== #
# CLI command implementations                                           #
# ===================================================================== #
def _print_table(rows: List[dict], headers: str = "keys") -> None:
    if not rows:
        print("(none)")
        return
    if tabulate:
        print(tabulate(rows, headers=headers))
    else:
        for r in rows:
            print(json.dumps(r, default=str))


def cmd_scan(_args) -> int:
    bot = WeatherBot()
    try:
        counters = bot.scan_once()
        print("Scan complete:", counters.summary())
        if counters.trades == 0:
            print("No markets met the high-confidence criteria. The bot stays idle - this is normal.")
        return 0
    finally:
        bot.close()


def cmd_dry_run(_args) -> int:
    cfg = get_config()
    if not cfg.dry_run:
        print("WARNING: DRY_RUN is not true in .env. Forcing simulation for the 'dry-run' command.")
    bot = WeatherBot()
    # Force-simulate regardless of .env for the explicit dry-run command.
    bot.config.dry_run = True
    bot.config.live_trading = False
    try:
        bot.run_loop()
        return 0
    finally:
        bot.close()


def cmd_live(_args) -> int:
    cfg = get_config()
    bot = WeatherBot()
    try:
        if not cfg.can_trade_live():
            print(
                "LIVE refused: requires DRY_RUN=false AND LIVE_TRADING=true in .env.\n"
                f"  current: DRY_RUN={cfg.dry_run} LIVE_TRADING={cfg.live_trading}\n"
                "Running in SIMULATION instead (no real orders)."
            )
        elif not cfg.has_live_credentials():
            print(
                "LIVE refused: Polymarket credentials are incomplete.\n"
                "Fill POLYMARKET_API_KEY/SECRET/PASSPHRASE and POLYMARKET_PRIVATE_KEY in .env.\n"
                "Running in SIMULATION instead (no real orders)."
            )
        else:
            print("LIVE mode active. Real limit orders may be placed within the configured limits.")
        bot.run_loop()
        return 0
    finally:
        bot.close()


def cmd_status(_args) -> int:
    cfg = get_config()
    bot = WeatherBot()
    try:
        print("=" * 60)
        print("Polymarket Weather/Temperature Bot - STATUS")
        print("=" * 60)
        print(f"Mode               : {cfg.mode_label()}")
        print(f"DRY_RUN            : {cfg.dry_run}")
        print(f"LIVE_TRADING       : {cfg.live_trading}")
        print(f"Weather provider   : {cfg.weather_provider}")
        print(f"Entry price window : {cfg.min_entry_price} - {cfg.max_entry_price}")
        print(f"Min bot prob       : {cfg.min_bot_probability}")
        print(f"Min confidence     : {cfg.min_confidence_score}")
        print(f"Min temp distance  : {cfg.min_temp_distance_c} C / {cfg.min_temp_distance_f} F")
        print(f"Max hours to res.  : {cfg.max_hours_to_resolution} (pref {cfg.preferred_hours_to_resolution})")
        print(f"Min liquidity      : {fmt_money(cfg.min_liquidity_usd)}")
        print(f"Max spread         : {cfg.max_spread_percent}%")
        print(f"Max pos/market     : {fmt_money(cfg.max_position_per_market_usd)}")
        print(f"Max open positions : {cfg.max_open_positions}")
        print(f"Max daily loss     : {fmt_money(cfg.max_daily_loss_usd)}")
        print(f"Allow exact temp   : {cfg.allow_exact_temp_markets}")
        print(f"Allow ambiguous    : {cfg.allow_ambiguous_markets}")
        print("-" * 60)

        for w in cfg.validate():
            print(f"  [config warning] {w}")

        stats = bot.db.get_daily_stats()
        if stats:
            print("Today (UTC):")
            print(f"  scanned={stats['markets_scanned']} trades={stats['trades_opened']} "
                  f"skips={stats['skips']}")
            print(f"  realized_pnl={fmt_money(stats['realized_pnl'])} "
                  f"daily_loss={fmt_money(stats['daily_loss'])} halted={bool(stats['trading_halted'])}")
        else:
            print("Today (UTC): no activity yet.")
        return 0
    finally:
        bot.close()


def cmd_positions(_args) -> int:
    bot = WeatherBot()
    try:
        rows = bot.db.get_all_positions(limit=100)
        out = [
            {
                "id": r["id"],
                "status": r["status"],
                "outcome": r["outcome"],
                "entry": r["entry_price"],
                "size": r["size"],
                "notional": fmt_money(r["notional_usd"]),
                "pnl": fmt_money(r["realized_pnl"]) if r["realized_pnl"] is not None else "-",
                "dry_run": bool(r["dry_run"]),
                "title": (r["title"] or "")[:45],
            }
            for r in rows
        ]
        _print_table(out)
        return 0
    finally:
        bot.close()


def cmd_pnl(_args) -> int:
    bot = WeatherBot()
    try:
        s = bot.db.pnl_summary()
        print("P&L summary")
        print("-" * 40)
        print(f"Closed trades   : {s['closed_trades']}")
        print(f"  wins / losses : {s['wins']} / {s['losses']}")
        print(f"Realized P&L    : {fmt_money(s['total_realized_pnl'])}")
        print(f"Open positions  : {s['open_positions']}")
        print(f"Open notional   : {fmt_money(s['open_notional'])}")
        if s["closed_trades"] > 0:
            win_rate = s["wins"] / s["closed_trades"]
            print(f"Win rate        : {fmt_pct(win_rate)}")
        print("\nNote: this is a record of activity, NOT a promise of future profit.")
        return 0
    finally:
        bot.close()


def cmd_rejected(_args) -> int:
    bot = WeatherBot()
    try:
        rows = bot.db.get_rejections(limit=getattr(_args, "limit", 50))
        out = [
            {
                "id": r["id"],
                "reason": r["skip_reason"],
                "city": r["city"],
                "type": r["market_type"],
                "thr": r["threshold"],
                "fc": round(r["forecast_value"], 1) if r["forecast_value"] is not None else "-",
                "dist": round(r["temp_distance"], 1) if r["temp_distance"] is not None else "-",
                "conf": round(r["confidence_score"], 3) if r["confidence_score"] is not None else "-",
                "price": r["market_price"],
                "title": (r["title"] or "")[:40],
            }
            for r in rows
        ]
        _print_table(out)
        return 0
    finally:
        bot.close()


def cmd_logs(_args) -> int:
    cfg = get_config()
    n = getattr(_args, "lines", 50)
    try:
        with open(cfg.log_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        for line in lines[-n:]:
            print(line.rstrip())
        return 0
    except FileNotFoundError:
        print(f"No log file yet at {cfg.log_path}. Run a scan first.")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Conservative Polymarket weather/temperature trading bot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="Run a single scan pass (no continuous loop).")
    sub.add_parser("dry-run", help="Continuous loop in DRY_RUN (simulated orders).")
    sub.add_parser("live", help="Continuous loop; live orders only if .env allows.")
    sub.add_parser("status", help="Show configuration and today's stats.")
    sub.add_parser("positions", help="List positions.")
    sub.add_parser("pnl", help="Show P&L summary.")
    p_rej = sub.add_parser("rejected", help="Show recently rejected markets.")
    p_rej.add_argument("--limit", type=int, default=50)
    p_logs = sub.add_parser("logs", help="Tail the log file.")
    p_logs.add_argument("--lines", type=int, default=50)
    return parser


COMMANDS = {
    "scan": cmd_scan,
    "dry-run": cmd_dry_run,
    "live": cmd_live,
    "status": cmd_status,
    "positions": cmd_positions,
    "pnl": cmd_pnl,
    "rejected": cmd_rejected,
    "logs": cmd_logs,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
