"""
utils.py
========
Shared helpers used across the bot:

* Logging setup (console + rotating file).
* Temperature unit conversions and parsing.
* Decision label constants (the single source of truth for skip reasons).
* Small math helpers for scoring and clamping.
* Safe datetime parsing for resolution windows.

Nothing here talks to the network or the database; keep it dependency-light.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

# --------------------------------------------------------------------------- #
# Decision labels - the canonical set used by predictor / risk / trader.       #
# Keeping them as constants avoids typos and makes DB queries reliable.        #
# --------------------------------------------------------------------------- #
TRADE_HIGH_CONFIDENCE = "TRADE_HIGH_CONFIDENCE"
SKIP_PRICE_OUT_OF_RANGE = "SKIP_PRICE_OUT_OF_RANGE"
SKIP_LOW_CONFIDENCE = "SKIP_LOW_CONFIDENCE"
SKIP_FORECAST_TOO_CLOSE = "SKIP_FORECAST_TOO_CLOSE"
SKIP_LOW_LIQUIDITY = "SKIP_LOW_LIQUIDITY"
SKIP_WIDE_SPREAD = "SKIP_WIDE_SPREAD"
SKIP_RULES_AMBIGUOUS = "SKIP_RULES_AMBIGUOUS"
SKIP_PARSE_FAILED = "SKIP_PARSE_FAILED"
SKIP_TOO_FAR_FROM_RESOLUTION = "SKIP_TOO_FAR_FROM_RESOLUTION"
SKIP_ALREADY_HAS_POSITION = "SKIP_ALREADY_HAS_POSITION"
SKIP_RISK_LIMIT = "SKIP_RISK_LIMIT"
# Extra defensive labels (still "skip" family, used internally):
SKIP_NOT_TEMPERATURE = "SKIP_NOT_TEMPERATURE"
SKIP_EXACT_TEMP_DISABLED = "SKIP_EXACT_TEMP_DISABLED"
SKIP_WEATHER_DATA_INCOMPLETE = "SKIP_WEATHER_DATA_INCOMPLETE"
SKIP_PREDICTION_CONFLICT = "SKIP_PREDICTION_CONFLICT"
SKIP_NO_ORDERBOOK = "SKIP_NO_ORDERBOOK"

ALL_SKIP_LABELS = {
    SKIP_PRICE_OUT_OF_RANGE,
    SKIP_LOW_CONFIDENCE,
    SKIP_FORECAST_TOO_CLOSE,
    SKIP_LOW_LIQUIDITY,
    SKIP_WIDE_SPREAD,
    SKIP_RULES_AMBIGUOUS,
    SKIP_PARSE_FAILED,
    SKIP_TOO_FAR_FROM_RESOLUTION,
    SKIP_ALREADY_HAS_POSITION,
    SKIP_RISK_LIMIT,
    SKIP_NOT_TEMPERATURE,
    SKIP_EXACT_TEMP_DISABLED,
    SKIP_WEATHER_DATA_INCOMPLETE,
    SKIP_PREDICTION_CONFLICT,
    SKIP_NO_ORDERBOOK,
}


# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #
_LOGGER_CONFIGURED = False


def setup_logging(log_path: str = "weather_bot.log", level: int = logging.INFO) -> logging.Logger:
    """Configure the root 'weatherbot' logger once and return it.

    Logs go to both stdout (for systemd/journald) and a rotating file.
    """
    global _LOGGER_CONFIGURED
    logger = logging.getLogger("weatherbot")
    if _LOGGER_CONFIGURED:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        # If the log file can't be opened (read-only fs etc.), keep console logging.
        logger.warning("Could not open log file %s; logging to console only.", log_path)

    _LOGGER_CONFIGURED = True
    return logger


def get_logger(name: str = "weatherbot") -> logging.Logger:
    """Return a child logger under the configured 'weatherbot' root."""
    if name == "weatherbot":
        return logging.getLogger("weatherbot")
    return logging.getLogger(f"weatherbot.{name}")


# --------------------------------------------------------------------------- #
# Math helpers                                                                 #
# --------------------------------------------------------------------------- #
def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp ``value`` into the inclusive [low, high] range."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def safe_float(value, default: Optional[float] = None) -> Optional[float]:
    """Best-effort float conversion that never raises."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Temperature conversions                                                      #
# --------------------------------------------------------------------------- #
def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def f_to_c(fahrenheit: float) -> float:
    return (fahrenheit - 32.0) * 5.0 / 9.0


def normalize_temp_to_c(value: float, unit: str) -> float:
    """Normalize a temperature value to Celsius given its unit ('C' or 'F')."""
    unit = (unit or "C").strip().upper()
    if unit.startswith("F"):
        return f_to_c(value)
    return value


# --------------------------------------------------------------------------- #
# Temperature string parsing                                                   #
# --------------------------------------------------------------------------- #
# Matches things like: 30C, 30°C, 86F, 86 °F, -3.5C, 72 degrees, 50°
_TEMP_RE = re.compile(
    r"(?P<num>-?\d{1,3}(?:\.\d+)?)\s*"
    r"(?:°|\u00b0)?\s*"
    r"(?P<unit>deg(?:rees)?|celsius|fahrenheit|c|f)?",
    re.IGNORECASE,
)


def parse_temperature(text: str) -> Optional[tuple[float, str]]:
    """Extract the first temperature mention from a string.

    Returns ``(value, unit)`` where unit is 'C' or 'F', or ``None`` if no
    temperature can be confidently parsed. When no explicit unit is present
    we return 'C' but callers should treat unit-less values cautiously.
    """
    if not text:
        return None
    match = _TEMP_RE.search(text)
    if not match:
        return None
    value = safe_float(match.group("num"))
    if value is None:
        return None
    raw_unit = (match.group("unit") or "").lower()
    if raw_unit.startswith("f"):
        unit = "F"
    elif raw_unit.startswith("c"):
        unit = "C"
    else:
        # No explicit C/F. Caller decides; default to C but flag via separate logic.
        unit = "C"
    return value, unit


def detect_temp_unit(text: str) -> Optional[str]:
    """Return 'C' or 'F' if the text explicitly mentions a unit, else None."""
    if not text:
        return None
    lowered = text.lower()
    if re.search(r"°?\s*f\b|fahrenheit", lowered):
        return "F"
    if re.search(r"°?\s*c\b|celsius", lowered):
        return "C"
    return None


# --------------------------------------------------------------------------- #
# Datetime helpers                                                             #
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_datetime(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (handles trailing 'Z') into aware UTC."""
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        # Fall back to dateutil if available (handles odd formats).
        try:
            from dateutil import parser as _dateutil_parser  # type: ignore

            dt = _dateutil_parser.parse(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None


def hours_until(target: Optional[datetime], reference: Optional[datetime] = None) -> Optional[float]:
    """Hours from ``reference`` (default now) until ``target``. None if unknown."""
    if target is None:
        return None
    ref = reference or utcnow()
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = target - ref
    return delta.total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Scoring helpers                                                              #
# --------------------------------------------------------------------------- #
def distance_score(distance: float, required: float) -> float:
    """Score how comfortably a forecast clears the required safety margin.

    * distance < required        -> 0.0   (inside the danger zone, must skip)
    * distance == required       -> ~0.6  (just barely clears)
    * distance >= 2x required    -> 1.0   (very comfortable)
    """
    if required <= 0:
        return 1.0 if distance > 0 else 0.0
    if distance < required:
        return 0.0
    ratio = distance / required
    # Map ratio in [1, 2] -> [0.6, 1.0], saturating at 1.0 beyond 2x.
    return clamp(0.6 + 0.4 * (ratio - 1.0), 0.0, 1.0)


def time_score(hours: Optional[float], preferred: float, maximum: float) -> float:
    """Score the time-to-resolution: nearer (within window) is better.

    * hours <= preferred -> 1.0
    * preferred < hours <= maximum -> linear decay 1.0 -> 0.7
    * hours > maximum -> 0.0 (handled as a hard skip elsewhere too)
    * unknown -> 0.0
    """
    if hours is None or hours < 0:
        return 0.0
    if hours <= preferred:
        return 1.0
    if hours <= maximum:
        span = max(maximum - preferred, 1e-9)
        return clamp(1.0 - 0.3 * (hours - preferred) / span, 0.7, 1.0)
    return 0.0


def liquidity_score(liquidity_usd: Optional[float], minimum: float) -> float:
    """Score available liquidity relative to the minimum requirement."""
    if liquidity_usd is None or liquidity_usd <= 0:
        return 0.0
    if liquidity_usd < minimum:
        return 0.0
    # 1x minimum -> 0.7, 3x minimum or more -> 1.0
    ratio = liquidity_usd / max(minimum, 1e-9)
    return clamp(0.7 + 0.15 * (ratio - 1.0), 0.7, 1.0)


def spread_score(spread_percent: Optional[float], max_spread_percent: float) -> float:
    """Score the bid/ask spread: tighter is better."""
    if spread_percent is None or spread_percent < 0:
        return 0.0
    if spread_percent > max_spread_percent:
        return 0.0
    if max_spread_percent <= 0:
        return 1.0 if spread_percent == 0 else 0.0
    # 0% spread -> 1.0, at the max allowed -> 0.6
    return clamp(1.0 - 0.4 * (spread_percent / max_spread_percent), 0.6, 1.0)


def geometric_mean(values: list[float]) -> float:
    """Geometric mean of a list of scores in [0, 1].

    The geometric mean is intentionally harsh: if ANY single component is
    near zero, the whole score collapses toward zero. That matches the
    bot's mandate - one weak signal should force a skip.
    """
    cleaned = [clamp(v) for v in values if v is not None]
    if not cleaned:
        return 0.0
    if any(v <= 0.0 for v in cleaned):
        return 0.0
    log_sum = sum(math.log(v) for v in cleaned)
    return math.exp(log_sum / len(cleaned))


def fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"
