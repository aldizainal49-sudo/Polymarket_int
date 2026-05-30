"""
config.py
=========
Central configuration for the Polymarket weather/temperature bot.

All tunables come from environment variables (loaded from a local ``.env``
file via python-dotenv). The :class:`Config` dataclass exposes them as typed
attributes with conservative defaults, so the bot behaves safely even if a
value is missing.

Safety rule (enforced in :meth:`Config.can_trade_live`):
    Real orders are ONLY allowed when DRY_RUN=false AND LIVE_TRADING=true.
    In every other case the bot operates in simulation mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv()  # loads .env from the current working directory if present
except Exception:  # pragma: no cover - dotenv is optional at runtime
    # If python-dotenv isn't installed we silently fall back to os.environ.
    pass


# --------------------------------------------------------------------------- #
# Small typed parsers (defensive: never raise on bad input, use the default)  #
# --------------------------------------------------------------------------- #
def _get_str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return value.strip() if value is not None else default


def _get_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _get_list(key: str, default: List[str]) -> List[str]:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


# --------------------------------------------------------------------------- #
# Configuration dataclass                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    """Typed view over the environment configuration."""

    # --- Polymarket credentials ---
    api_key: str = field(default_factory=lambda: _get_str("POLYMARKET_API_KEY"))
    api_secret: str = field(default_factory=lambda: _get_str("POLYMARKET_API_SECRET"))
    api_passphrase: str = field(
        default_factory=lambda: _get_str("POLYMARKET_API_PASSPHRASE")
    )
    private_key: str = field(default_factory=lambda: _get_str("POLYMARKET_PRIVATE_KEY"))
    proxy_address: str = field(
        default_factory=lambda: _get_str("POLYMARKET_PROXY_ADDRESS")
    )

    # --- Safety switches ---
    dry_run: bool = field(default_factory=lambda: _get_bool("DRY_RUN", True))
    live_trading: bool = field(default_factory=lambda: _get_bool("LIVE_TRADING", False))

    # --- Scanning ---
    enabled_categories: List[str] = field(
        default_factory=lambda: _get_list("ENABLED_CATEGORIES", ["weather", "temperature"])
    )
    scan_interval_seconds: int = field(
        default_factory=lambda: _get_int("SCAN_INTERVAL_SECONDS", 300)
    )

    # --- Entry price window ---
    min_entry_price: float = field(
        default_factory=lambda: _get_float("MIN_ENTRY_PRICE", 0.95)
    )
    max_entry_price: float = field(
        default_factory=lambda: _get_float("MAX_ENTRY_PRICE", 0.985)
    )

    # --- Resolution timing ---
    max_hours_to_resolution: float = field(
        default_factory=lambda: _get_float("MAX_HOURS_TO_RESOLUTION", 12.0)
    )
    preferred_hours_to_resolution: float = field(
        default_factory=lambda: _get_float("PREFERRED_HOURS_TO_RESOLUTION", 6.0)
    )
    # Hard ceiling: never trade something resolving beyond this even if "very clear".
    absolute_max_hours_to_resolution: float = 24.0

    # --- Confidence gates ---
    min_bot_probability: float = field(
        default_factory=lambda: _get_float("MIN_BOT_PROBABILITY", 0.95)
    )
    min_confidence_score: float = field(
        default_factory=lambda: _get_float("MIN_CONFIDENCE_SCORE", 0.95)
    )

    # --- Forecast distance from threshold ---
    min_temp_distance_c: float = field(
        default_factory=lambda: _get_float("MIN_TEMP_DISTANCE_C", 3.0)
    )
    min_temp_distance_f: float = field(
        default_factory=lambda: _get_float("MIN_TEMP_DISTANCE_F", 5.0)
    )

    # --- Market microstructure ---
    min_liquidity_usd: float = field(
        default_factory=lambda: _get_float("MIN_LIQUIDITY_USD", 500.0)
    )
    max_spread_percent: float = field(
        default_factory=lambda: _get_float("MAX_SPREAD_PERCENT", 1.0)
    )

    # --- Risk limits ---
    max_position_per_market_usd: float = field(
        default_factory=lambda: _get_float("MAX_POSITION_PER_MARKET_USD", 1.0)
    )
    max_open_positions: int = field(
        default_factory=lambda: _get_int("MAX_OPEN_POSITIONS", 3)
    )
    max_daily_loss_usd: float = field(
        default_factory=lambda: _get_float("MAX_DAILY_LOSS_USD", 3.0)
    )

    # --- Feature flags ---
    allow_longshot: bool = field(
        default_factory=lambda: _get_bool("ALLOW_LONGSHOT", False)
    )
    allow_exact_temp_markets: bool = field(
        default_factory=lambda: _get_bool("ALLOW_EXACT_TEMP_MARKETS", False)
    )
    allow_ambiguous_markets: bool = field(
        default_factory=lambda: _get_bool("ALLOW_AMBIGUOUS_MARKETS", False)
    )

    # --- Weather provider ---
    weather_provider: str = field(
        default_factory=lambda: _get_str("WEATHER_PROVIDER", "open_meteo").lower()
    )
    weather_api_key: str = field(default_factory=lambda: _get_str("WEATHER_API_KEY"))

    # --- API endpoints (rarely changed; not exposed in .env to avoid mistakes) ---
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    chain_id: int = 137  # Polygon mainnet

    # --- Local paths ---
    database_path: str = field(
        default_factory=lambda: _get_str("DATABASE_PATH", "weather_bot.db")
    )
    log_path: str = field(default_factory=lambda: _get_str("LOG_PATH", "weather_bot.log"))

    # ------------------------------------------------------------------ #
    # Derived safety helpers                                             #
    # ------------------------------------------------------------------ #
    def can_trade_live(self) -> bool:
        """Real orders are allowed ONLY when DRY_RUN is off and LIVE_TRADING is on."""
        return (not self.dry_run) and self.live_trading

    def has_live_credentials(self) -> bool:
        """True only if the minimum credential set for live trading is present."""
        return bool(self.private_key) and bool(
            self.api_key and self.api_secret and self.api_passphrase
        )

    def mode_label(self) -> str:
        if self.can_trade_live():
            return "LIVE"
        if self.dry_run:
            return "DRY_RUN"
        return "SIMULATION"

    def validate(self) -> List[str]:
        """Return a list of human-readable warnings about the current config.

        This never raises - the bot prefers to keep running safely (skipping)
        rather than crash. Warnings are surfaced in the logs / status output.
        """
        warnings: List[str] = []

        if self.min_entry_price < 0 or self.max_entry_price > 1:
            warnings.append("Entry price window must sit inside [0, 1].")
        if self.min_entry_price > self.max_entry_price:
            warnings.append(
                "MIN_ENTRY_PRICE is greater than MAX_ENTRY_PRICE; no trade can ever match."
            )
        if self.min_bot_probability < 0.95:
            warnings.append(
                "MIN_BOT_PROBABILITY below 0.95 weakens the high-confidence mandate."
            )
        if self.min_confidence_score < 0.95:
            warnings.append(
                "MIN_CONFIDENCE_SCORE below 0.95 weakens the high-confidence mandate."
            )
        if self.preferred_hours_to_resolution > self.max_hours_to_resolution:
            warnings.append(
                "PREFERRED_HOURS_TO_RESOLUTION should be <= MAX_HOURS_TO_RESOLUTION."
            )
        if self.can_trade_live() and not self.has_live_credentials():
            warnings.append(
                "LIVE mode requested but Polymarket credentials are incomplete; "
                "the bot will refuse to place real orders."
            )
        if self.weather_provider != "open_meteo" and not self.weather_api_key:
            warnings.append(
                f"WEATHER_PROVIDER={self.weather_provider} usually needs WEATHER_API_KEY."
            )
        return warnings


# Singleton-style accessor so modules share one config instance.
_CONFIG: Config | None = None


def get_config(reload: bool = False) -> Config:
    """Return the shared :class:`Config` instance (lazily constructed)."""
    global _CONFIG
    if _CONFIG is None or reload:
        _CONFIG = Config()
    return _CONFIG
