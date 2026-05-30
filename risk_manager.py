"""
risk_manager.py
===============
The capital-protection layer. It runs AFTER the predictor and order-book
analyzer have agreed a trade looks high-confidence, and it has the final say
on whether an order may be sized and sent.

Philosophy (in priority order, per the spec):
    1. Avoid losses.
    2. Skip doubtful markets.
    3. Take small profits.
    4. Protect capital.
    5. Only trade very clear temperature markets.

Hard rules enforced here:
    * Entry price must sit inside [MIN_ENTRY_PRICE, MAX_ENTRY_PRICE].
    * Spread <= MAX_SPREAD_PERCENT and liquidity >= MIN_LIQUIDITY_USD.
    * No second position in a market we already hold.
    * Open positions < MAX_OPEN_POSITIONS.
    * Daily loss has not hit MAX_DAILY_LOSS_USD (else trading is halted for
      the rest of the UTC day).
    * Position size capped at MAX_POSITION_PER_MARKET_USD (never averages,
      never martingales, never goes all-in).
There is intentionally no retry/averaging logic anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import Config, get_config
from database import Database
from orderbook_analyzer import OrderBookMetrics
from temperature_predictor import Prediction
from utils import (
    SKIP_ALREADY_HAS_POSITION,
    SKIP_LOW_LIQUIDITY,
    SKIP_PRICE_OUT_OF_RANGE,
    SKIP_RISK_LIMIT,
    SKIP_WIDE_SPREAD,
    get_logger,
)

log = get_logger("risk")


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    size_shares: float = 0.0
    notional_usd: float = 0.0
    entry_price: Optional[float] = None
    skip_label: Optional[str] = None


class RiskManager:
    def __init__(self, config: Optional[Config] = None, db: Optional[Database] = None) -> None:
        self.config = config or get_config()
        self.db = db

    # ------------------------------------------------------------------ #
    # Daily loss circuit-breaker                                         #
    # ------------------------------------------------------------------ #
    def trading_halted(self) -> bool:
        """True if the daily loss limit has been hit (halt until next UTC day)."""
        if self.db is None:
            return False
        if self.db.is_trading_halted():
            return True
        loss = self.db.get_daily_loss()
        if loss >= self.config.max_daily_loss_usd:
            self.db.set_trading_halted(True)
            log.warning(
                "Daily loss $%.2f reached limit $%.2f - halting trading for the day.",
                loss,
                self.config.max_daily_loss_usd,
            )
            return True
        return False

    # ------------------------------------------------------------------ #
    # Main evaluation                                                    #
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        prediction: Prediction,
        metrics: OrderBookMetrics,
        market_id: str,
    ) -> RiskDecision:
        """Final risk gate. Returns a :class:`RiskDecision`.

        Assumes ``prediction.decision`` is already TRADE_HIGH_CONFIDENCE; if not,
        it refuses immediately (defensive double-check).
        """
        if not prediction.is_trade:
            return RiskDecision(False, "prediction is not a trade", skip_label=prediction.skip_reason)

        # 0) Daily loss circuit breaker --------------------------------
        if self.trading_halted():
            return RiskDecision(
                False, "daily loss limit reached; trading halted", skip_label=SKIP_RISK_LIMIT
            )

        # 1) Price window ----------------------------------------------
        price = metrics.market_price
        if price is None:
            return RiskDecision(False, "no market price", skip_label=SKIP_PRICE_OUT_OF_RANGE)
        if price < self.config.min_entry_price or price > self.config.max_entry_price:
            return RiskDecision(
                False,
                f"price {price:.3f} outside [{self.config.min_entry_price}, "
                f"{self.config.max_entry_price}]",
                skip_label=SKIP_PRICE_OUT_OF_RANGE,
            )

        # 2) Spread -----------------------------------------------------
        if metrics.spread_percent is None or metrics.spread_percent > self.config.max_spread_percent:
            return RiskDecision(
                False,
                f"spread {metrics.spread_percent}% > {self.config.max_spread_percent}%",
                skip_label=SKIP_WIDE_SPREAD,
            )

        # 3) Liquidity --------------------------------------------------
        if metrics.liquidity_usd < self.config.min_liquidity_usd:
            return RiskDecision(
                False,
                f"liquidity ${metrics.liquidity_usd:.0f} < ${self.config.min_liquidity_usd:.0f}",
                skip_label=SKIP_LOW_LIQUIDITY,
            )

        # 4) No duplicate position -------------------------------------
        if self.db is not None and self.db.has_open_position_for_market(market_id):
            return RiskDecision(
                False, "already holding a position in this market", skip_label=SKIP_ALREADY_HAS_POSITION
            )

        # 5) Max open positions ----------------------------------------
        if self.db is not None:
            open_count = self.db.count_open_positions()
            if open_count >= self.config.max_open_positions:
                return RiskDecision(
                    False,
                    f"open positions {open_count} >= max {self.config.max_open_positions}",
                    skip_label=SKIP_RISK_LIMIT,
                )

        # 6) Size the position (capped, never scaled up) ---------------
        notional = self._affordable_notional()
        if notional <= 0:
            return RiskDecision(
                False, "no remaining risk budget for a new position", skip_label=SKIP_RISK_LIMIT
            )
        # shares = notional / price (each share pays $1 at resolution).
        size_shares = round(notional / price, 2)
        if size_shares <= 0:
            return RiskDecision(False, "computed size is zero", skip_label=SKIP_RISK_LIMIT)
        actual_notional = round(size_shares * price, 2)

        return RiskDecision(
            approved=True,
            reason="approved",
            size_shares=size_shares,
            notional_usd=actual_notional,
            entry_price=price,
        )

    # ------------------------------------------------------------------ #
    # Sizing helpers                                                     #
    # ------------------------------------------------------------------ #
    def _affordable_notional(self) -> float:
        """Per-market cap, further reduced by the remaining daily-loss budget.

        We never risk more on a single new position than what remains before
        the daily loss limit would be breached in the worst case.
        """
        per_market = self.config.max_position_per_market_usd
        if self.db is None:
            return per_market
        remaining_budget = self.config.max_daily_loss_usd - self.db.get_daily_loss()
        if remaining_budget <= 0:
            return 0.0
        # The most a single high-confidence position can lose is its notional.
        return round(min(per_market, remaining_budget), 2)

    # ------------------------------------------------------------------ #
    # Realised P&L bookkeeping (called by trader on close)               #
    # ------------------------------------------------------------------ #
    def record_realized_pnl(self, pnl: float) -> None:
        if self.db is not None:
            self.db.add_realized_pnl(pnl)
            # Re-check the breaker immediately after a loss.
            self.trading_halted()
