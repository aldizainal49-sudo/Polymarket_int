"""
polymarket_client.py
====================
Thin client around the two Polymarket APIs the bot needs:

* Gamma API (https://gamma-api.polymarket.com) - discover active markets.
* CLOB API  (https://clob.polymarket.com) - order books and order execution.

Order placement uses the official ``py-clob-client`` when it is installed and
live credentials are present. If the library is missing or we are in DRY_RUN,
the client stays in "read-only" mode and order placement is simulated by the
:mod:`trader` module instead (this file never sends a live order on its own
unless explicitly asked AND credentials are valid).

Everything is defensive: network errors return empty/None rather than raising,
so the scan loop keeps running and simply skips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from config import Config, get_config
from utils import get_logger, safe_float

log = get_logger("polymarket")

_HTTP_TIMEOUT = 20


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[OrderBookLevel] = field(default_factory=list)  # sorted desc by price
    asks: List[OrderBookLevel] = field(default_factory=list)  # sorted asc by price

    @property
    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None

    def is_empty(self) -> bool:
        return not self.bids and not self.asks


class PolymarketClient:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "polymarket-weather-bot/1.0"})
        self._clob = None  # lazily initialised py-clob-client (live only)

    # ================================================================== #
    # Gamma API - market discovery                                       #
    # ================================================================== #
    def get_active_markets(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Fetch active, non-closed markets from the Gamma API.

        Returns a list of raw market dicts. On any error returns []. We page
        through results conservatively (a few pages) to find weather markets
        without hammering the API.
        """
        markets: List[Dict[str, Any]] = []
        page_size = 100
        offset = 0
        max_pages = max(1, limit // page_size)

        for _ in range(max_pages):
            try:
                resp = self.session.get(
                    f"{self.config.gamma_base_url}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "archived": "false",
                        "limit": page_size,
                        "offset": offset,
                        "order": "endDate",
                        "ascending": "true",
                    },
                    timeout=_HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                batch = resp.json()
                if isinstance(batch, dict):
                    # Some Gamma responses wrap data in {"data": [...]}.
                    batch = batch.get("data") or batch.get("markets") or []
                if not batch:
                    break
                markets.extend(batch)
                offset += page_size
                if len(batch) < page_size:
                    break
            except requests.RequestException as exc:
                log.warning("Gamma markets fetch failed (offset=%s): %s", offset, exc)
                break
            except ValueError as exc:
                log.warning("Gamma markets JSON decode failed: %s", exc)
                break

        log.info("Fetched %d active markets from Gamma.", len(markets))
        return markets

    def search_markets(self, query: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Optional keyword search against Gamma (best-effort)."""
        try:
            resp = self.session.get(
                f"{self.config.gamma_base_url}/markets",
                params={"active": "true", "closed": "false", "limit": limit, "search": query},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("data") or data.get("markets") or []
            return data or []
        except (requests.RequestException, ValueError) as exc:
            log.warning("Gamma search failed for %r: %s", query, exc)
            return []

    def get_market(self, market_id: str) -> Dict[str, Any]:
        """Fetch a single market by its Gamma id (best-effort).

        Used for position reconciliation (checking if a market has resolved).
        Returns {} on any error so callers can simply skip.
        """
        if not market_id:
            return {}
        try:
            resp = self.session.get(
                f"{self.config.gamma_base_url}/markets/{market_id}",
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data[0] if data else {}
            if isinstance(data, dict):
                # Some responses wrap the object under "data".
                return data.get("data") or data
            return {}
        except (requests.RequestException, ValueError) as exc:
            log.debug("Gamma get_market failed for %s: %s", market_id, exc)
            return {}

    # ================================================================== #
    # CLOB API - order book                                              #
    # ================================================================== #
    def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch the order book for a CLOB token (outcome) id."""
        if not token_id:
            return None
        try:
            resp = self.session.get(
                f"{self.config.clob_base_url}/book",
                params={"token_id": token_id},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("CLOB book fetch failed for %s: %s", token_id, exc)
            return None

        book = OrderBook(token_id=token_id)
        for raw in data.get("bids", []) or []:
            price = safe_float(raw.get("price"))
            size = safe_float(raw.get("size"))
            if price is not None and size is not None:
                book.bids.append(OrderBookLevel(price=price, size=size))
        for raw in data.get("asks", []) or []:
            price = safe_float(raw.get("price"))
            size = safe_float(raw.get("size"))
            if price is not None and size is not None:
                book.asks.append(OrderBookLevel(price=price, size=size))

        # Normalize ordering: best bid = highest price, best ask = lowest price.
        book.bids.sort(key=lambda lvl: lvl.price, reverse=True)
        book.asks.sort(key=lambda lvl: lvl.price)
        return book

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            resp = self.session.get(
                f"{self.config.clob_base_url}/midpoint",
                params={"token_id": token_id},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            return safe_float(resp.json().get("mid"))
        except (requests.RequestException, ValueError) as exc:
            log.debug("CLOB midpoint fetch failed for %s: %s", token_id, exc)
            return None

    # ================================================================== #
    # CLOB API - live order client (only initialised for LIVE trading)   #
    # ================================================================== #
    def _ensure_clob_client(self):
        """Lazily build the py-clob-client; returns None if unavailable.

        This is only used when the bot is genuinely allowed to trade live and
        full credentials exist. The actual safety gate lives in trader.py.
        """
        if self._clob is not None:
            return self._clob
        if not self.config.has_live_credentials():
            log.warning("Cannot init CLOB client: incomplete credentials.")
            return None
        try:
            from py_clob_client.client import ClobClient  # type: ignore
            from py_clob_client.clob_types import ApiCreds  # type: ignore

            creds = ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            )
            client = ClobClient(
                self.config.clob_base_url,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                creds=creds,
                signature_type=2 if self.config.proxy_address else 0,
                funder=self.config.proxy_address or None,
            )
            self._clob = client
            log.info("Initialised py-clob-client for live trading.")
            return self._clob
        except ImportError:
            log.error("py-clob-client not installed; live order placement disabled.")
            return None
        except Exception as exc:  # defensive
            log.error("Failed to init CLOB client: %s", exc)
            return None

    def place_limit_order(
        self, token_id: str, price: float, size: float, side: str = "BUY"
    ) -> Dict[str, Any]:
        """Place a REAL limit order via py-clob-client.

        IMPORTANT: this method assumes the caller (trader.py) has already
        verified that live trading is permitted and safe. It still re-checks
        credentials and returns a structured result dict; it never raises.
        """
        result: Dict[str, Any] = {"success": False, "order_id": None, "error": None}

        client = self._ensure_clob_client()
        if client is None:
            result["error"] = "CLOB client unavailable (missing lib or credentials)"
            return result

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
            from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore

            order_args = OrderArgs(
                token_id=token_id,
                price=round(float(price), 3),
                size=float(size),
                side=BUY if side.upper() == "BUY" else SELL,
            )
            signed = client.create_order(order_args)
            # GTC limit order - explicitly NOT a market order.
            resp = client.post_order(signed, OrderType.GTC)
            result["success"] = bool(resp)
            result["order_id"] = (resp or {}).get("orderID") or (resp or {}).get("orderId")
            result["raw"] = resp
        except ImportError as exc:
            result["error"] = f"py-clob-client missing components: {exc}"
        except Exception as exc:  # defensive: surface but never crash
            result["error"] = str(exc)
            log.error("Live order placement failed: %s", exc)
        return result
