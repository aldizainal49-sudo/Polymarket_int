"""
trader.py
=========
Order execution layer.

Safety model (this is the most important file for capital protection):
    * ONLY limit orders. Market orders are never used.
    * A real order is sent ONLY when ALL of these are true:
        - config.can_trade_live()  (DRY_RUN=false AND LIVE_TRADING=true)
        - full live credentials are present
        - the price is inside [MIN_ENTRY_PRICE, MAX_ENTRY_PRICE]
        - the risk manager approved the trade
      This is the "double safety check": the gate is verified once when the
      trade is assembled and AGAIN immediately before the order is sent.
    * Never chase: if the live ask has moved above MAX_ENTRY_PRICE between the
      decision and execution, the order is abandoned (no market order, no bump).
    * In DRY_RUN / SIMULATION the order is recorded in the DB and a simulated
      position is opened, but nothing is sent to Polymarket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import Config, get_config
from database import Database
from orderbook_analyzer import OrderBookMetrics
from polymarket_client import PolymarketClient
from risk_manager import RiskDecision
from temperature_predictor import Prediction
from utils import get_logger, utcnow

log = get_logger("trader")


@dataclass
class TradeResult:
    placed: bool
    simulated: bool
    reason: str
    order_db_id: Optional[int] = None
    position_db_id: Optional[int] = None
    exchange_order_id: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    notional_usd: Optional[float] = None


class Trader:
    def __init__(
        self,
        config: Optional[Config] = None,
        db: Optional[Database] = None,
        client: Optional[PolymarketClient] = None,
    ) -> None:
        self.config = config or get_config()
        self.db = db
        self.client = client or PolymarketClient(self.config)

    # ------------------------------------------------------------------ #
    # The single public method                                           #
    # ------------------------------------------------------------------ #
    def execute(
        self,
        prediction: Prediction,
        risk: RiskDecision,
        metrics: OrderBookMetrics,
        token_id: str,
        market_id: str,
        title: str,
    ) -> TradeResult:
        """Place (or simulate) a limit buy for the approved outcome."""
        # ---- Safety check #1: revalidate the whole chain --------------
        ok, why = self._first_safety_check(prediction, risk, metrics)
        if not ok:
            log.info("Trade blocked at safety check #1: %s", why)
            return TradeResult(placed=False, simulated=False, reason=why)

        price = risk.entry_price
        size = risk.size_shares
        notional = risk.notional_usd
        outcome = prediction.target_outcome or "?"

        live = self.config.can_trade_live() and self.config.has_live_credentials()

        # ---- DRY_RUN / SIMULATION path --------------------------------
        if not live:
            return self._simulate(
                token_id, market_id, title, outcome, price, size, notional
            )

        # ---- LIVE path: re-fetch the book and run safety check #2 ------
        fresh_book = self.client.get_order_book(token_id)
        ok2, why2, fresh_price = self._second_safety_check(fresh_book, price)
        if not ok2:
            log.warning("Trade blocked at safety check #2 (anti-chase): %s", why2)
            self._record_order(
                token_id, market_id, title, outcome, fresh_price or price, size,
                notional, status="ABORTED", dry_run=False, error=why2,
            )
            return TradeResult(placed=False, simulated=False, reason=why2)

        # Use the fresh price but never above the cap (already verified).
        exec_price = min(fresh_price, self.config.max_entry_price)
        log.info(
            "LIVE limit BUY %s | %s | price=%.3f size=%.2f notional=$%.2f",
            outcome, title[:60], exec_price, size, notional,
        )
        resp = self.client.place_limit_order(token_id, exec_price, size, side="BUY")

        if resp.get("success"):
            order_id = self._record_order(
                token_id, market_id, title, outcome, exec_price, size, notional,
                status="LIVE_PLACED", dry_run=False,
                exchange_order_id=resp.get("order_id"),
            )
            pos_id = self._open_position(
                token_id, market_id, title, outcome, exec_price, size, notional, dry_run=False
            )
            if self.db is not None:
                self.db.bump_daily("trades_opened", 1)
            return TradeResult(
                placed=True, simulated=False, reason="live order placed",
                order_db_id=order_id, position_db_id=pos_id,
                exchange_order_id=resp.get("order_id"),
                price=exec_price, size=size, notional_usd=notional,
            )

        err = resp.get("error") or "unknown error"
        log.error("Live order failed: %s", err)
        self._record_order(
            token_id, market_id, title, outcome, exec_price, size, notional,
            status="FAILED", dry_run=False, error=err,
        )
        return TradeResult(placed=False, simulated=False, reason=f"live order failed: {err}")

    # ------------------------------------------------------------------ #
    # Safety checks                                                      #
    # ------------------------------------------------------------------ #
    def _first_safety_check(
        self, prediction: Prediction, risk: RiskDecision, metrics: OrderBookMetrics
    ) -> tuple[bool, str]:
        if not prediction.is_trade:
            return False, "prediction is not TRADE_HIGH_CONFIDENCE"
        if not risk.approved:
            return False, f"risk not approved: {risk.reason}"
        if risk.entry_price is None:
            return False, "no entry price"
        if risk.size_shares <= 0:
            return False, "non-positive size"
        # Re-assert the price window (defense in depth).
        if not (self.config.min_entry_price <= risk.entry_price <= self.config.max_entry_price):
            return False, "entry price outside the allowed window"
        # Re-assert probability / confidence floors.
        if prediction.bot_probability < self.config.min_bot_probability:
            return False, "bot_probability below floor"
        if prediction.confidence_score < self.config.min_confidence_score:
            return False, "confidence_score below floor"
        return True, "ok"

    def _second_safety_check(
        self, fresh_book, decision_price: Optional[float]
    ) -> tuple[bool, str, Optional[float]]:
        """Anti-chase check using a freshly fetched order book."""
        if fresh_book is None or fresh_book.is_empty() or not fresh_book.best_ask:
            return False, "order book vanished or empty at execution time", None
        fresh_ask = fresh_book.best_ask.price
        if fresh_ask > self.config.max_entry_price:
            return False, (
                f"ask {fresh_ask:.3f} moved above cap {self.config.max_entry_price} - not chasing"
            ), fresh_ask
        if fresh_ask < self.config.min_entry_price:
            return False, (
                f"ask {fresh_ask:.3f} dropped below floor {self.config.min_entry_price} - "
                "confidence assumption broken"
            ), fresh_ask
        return True, "ok", fresh_ask

    # ------------------------------------------------------------------ #
    # Simulation                                                         #
    # ------------------------------------------------------------------ #
    def _simulate(
        self, token_id, market_id, title, outcome, price, size, notional
    ) -> TradeResult:
        log.info(
            "[DRY_RUN] would place limit BUY %s | %s | price=%.3f size=%.2f notional=$%.2f",
            outcome, title[:60], price, size, notional,
        )
        order_id = self._record_order(
            token_id, market_id, title, outcome, price, size, notional,
            status="SIMULATED", dry_run=True,
        )
        pos_id = self._open_position(
            token_id, market_id, title, outcome, price, size, notional, dry_run=True
        )
        if self.db is not None:
            self.db.bump_daily("trades_opened", 1)
        return TradeResult(
            placed=False, simulated=True, reason="dry-run simulated order",
            order_db_id=order_id, position_db_id=pos_id,
            price=price, size=size, notional_usd=notional,
        )

    # ------------------------------------------------------------------ #
    # DB helpers                                                         #
    # ------------------------------------------------------------------ #
    def _record_order(
        self, token_id, market_id, title, outcome, price, size, notional,
        status, dry_run, exchange_order_id=None, error=None,
    ) -> Optional[int]:
        if self.db is None:
            return None
        return self.db.record_order(
            {
                "market_id": market_id,
                "token_id": token_id,
                "title": title,
                "outcome": outcome,
                "side": "BUY",
                "price": price,
                "size": size,
                "notional_usd": notional,
                "order_type": "LIMIT",
                "status": status,
                "dry_run": dry_run,
                "exchange_order_id": exchange_order_id,
                "error": error,
                "timestamp": utcnow().isoformat(),
            }
        )

    def _open_position(
        self, token_id, market_id, title, outcome, price, size, notional, dry_run
    ) -> Optional[int]:
        if self.db is None:
            return None
        return self.db.open_position(
            {
                "market_id": market_id,
                "token_id": token_id,
                "title": title,
                "outcome": outcome,
                "entry_price": price,
                "size": size,
                "notional_usd": notional,
                "dry_run": dry_run,
                "opened_at": utcnow().isoformat(),
            }
        )
