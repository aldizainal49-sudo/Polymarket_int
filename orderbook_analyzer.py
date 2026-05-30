"""
orderbook_analyzer.py
=====================
Turns a raw :class:`OrderBook` into the microstructure metrics the bot needs:

* best bid / best ask
* spread (absolute and percent)
* available liquidity (USD) near the touch
* market price (the ask we'd actually pay when buying)
* implied probability (= price, since Polymarket shares pay $1)
* liquidity_score / spread_score sub-scores for the confidence model

All checks are conservative: missing/empty books yield metrics that force a
SKIP downstream rather than an optimistic guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from config import Config, get_config
from polymarket_client import OrderBook
from utils import get_logger, liquidity_score, spread_score

log = get_logger("orderbook")


@dataclass
class OrderBookMetrics:
    token_id: str
    ok: bool = False
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    mid_price: Optional[float] = None
    spread_abs: Optional[float] = None
    spread_percent: Optional[float] = None
    market_price: Optional[float] = None         # ask price a buyer pays
    implied_probability: Optional[float] = None  # == market_price
    bid_liquidity_usd: float = 0.0
    ask_liquidity_usd: float = 0.0
    liquidity_usd: float = 0.0                    # usable (ask-side) liquidity
    liquidity_score: float = 0.0
    spread_score: float = 0.0
    note: str = ""


class OrderBookAnalyzer:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()

    def analyze(self, book: Optional[OrderBook], depth_levels: int = 5) -> OrderBookMetrics:
        if book is None:
            return OrderBookMetrics(token_id="", ok=False, note="no order book")

        metrics = OrderBookMetrics(token_id=book.token_id)

        if book.is_empty() or not book.best_ask or not book.best_bid:
            metrics.note = "empty or one-sided book"
            return metrics

        best_bid = book.best_bid.price
        best_ask = book.best_ask.price
        metrics.best_bid = best_bid
        metrics.best_ask = best_ask
        metrics.mid_price = round((best_bid + best_ask) / 2.0, 6)

        # Spread.
        metrics.spread_abs = round(best_ask - best_bid, 6)
        # Percent spread relative to the mid (in percent units, e.g. 1.0 = 1%).
        if metrics.mid_price and metrics.mid_price > 0:
            metrics.spread_percent = round(100.0 * metrics.spread_abs / metrics.mid_price, 4)
        else:
            metrics.spread_percent = None

        # Market price for a BUYER = best ask. Implied prob = price.
        metrics.market_price = best_ask
        metrics.implied_probability = best_ask

        # Liquidity (USD) summed across the top N levels.
        # Notional per level = price * size (size is in shares; each pays $1).
        ask_liq = 0.0
        for lvl in book.asks[:depth_levels]:
            ask_liq += lvl.price * lvl.size
        bid_liq = 0.0
        for lvl in book.bids[:depth_levels]:
            bid_liq += lvl.price * lvl.size
        metrics.ask_liquidity_usd = round(ask_liq, 2)
        metrics.bid_liquidity_usd = round(bid_liq, 2)
        # The liquidity that matters for us (we buy) is the ask side.
        metrics.liquidity_usd = metrics.ask_liquidity_usd

        # Sub-scores.
        metrics.liquidity_score = liquidity_score(
            metrics.liquidity_usd, self.config.min_liquidity_usd
        )
        metrics.spread_score = spread_score(
            metrics.spread_percent, self.config.max_spread_percent
        )

        metrics.ok = True
        return metrics

    # ------------------------------------------------------------------ #
    # Convenience gates (mirrors the config thresholds)                  #
    # ------------------------------------------------------------------ #
    def passes_microstructure(self, metrics: OrderBookMetrics) -> tuple[bool, str]:
        """Return (ok, reason) for the spread/liquidity gates only."""
        if not metrics.ok:
            return False, "no usable order book"
        if metrics.spread_percent is None:
            return False, "spread unknown"
        if metrics.spread_percent > self.config.max_spread_percent:
            return False, (
                f"spread {metrics.spread_percent:.2f}% > {self.config.max_spread_percent}%"
            )
        if metrics.liquidity_usd < self.config.min_liquidity_usd:
            return False, (
                f"liquidity ${metrics.liquidity_usd:.0f} < ${self.config.min_liquidity_usd:.0f}"
            )
        return True, "ok"
