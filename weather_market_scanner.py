"""
weather_market_scanner.py
=========================
Identifies and parses Polymarket weather/temperature markets.

Responsibilities
----------------
1. Filter the raw Gamma market list down to weather/temperature markets only.
   Anything that smells like sports/politics/crypto/finance/culture/news is
   rejected up front.
2. Parse each weather market's title + description + rules into a structured
   :class:`ParsedMarket`:
     - city
     - date (local YYYY-MM-DD)
     - market_type  (HIGH_ABOVE / HIGH_BELOW / LOW_BELOW / LOW_ABOVE / EXACT / RANGE)
     - threshold (+ unit C/F)
     - traded outcome (YES / NO) and its CLOB token id
     - close / resolution time
     - resolution source clarity
3. Attach parse-confidence sub-scores (city, date, rules clarity) so the
   predictor can fold them into the final confidence. If parsing the essentials
   fails, the market is flagged ``parse_ok = False`` and the bot skips.

This module is pure parsing - it never fetches forecasts or order books.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import Config, get_config
from utils import (
    detect_temp_unit,
    get_logger,
    parse_iso_datetime,
    safe_float,
)
from weather_data_client import CITY_COORDS

log = get_logger("scanner")


# --------------------------------------------------------------------------- #
# Market type constants                                                       #
# --------------------------------------------------------------------------- #
HIGH_ABOVE = "HIGH_ABOVE"   # highest temperature >= threshold
HIGH_BELOW = "HIGH_BELOW"   # highest temperature < threshold
LOW_BELOW = "LOW_BELOW"     # lowest temperature <= threshold
LOW_ABOVE = "LOW_ABOVE"     # lowest temperature > threshold
EXACT = "EXACT"             # exact temperature / narrow band (skipped by default)
RANGE = "RANGE"             # "between X and Y" (treated like EXACT / ambiguous)
UNKNOWN = "UNKNOWN"


# Keywords that strongly indicate a temperature market.
_TEMP_KEYWORDS = (
    "temperature",
    "temp ",
    "highest temp",
    "lowest temp",
    "high temp",
    "low temp",
    "degrees",
    "°c",
    "°f",
    "warmest",
    "coldest",
)
_WEATHER_KEYWORDS = (
    "weather",
    "temperature",
    "rain",
    "snow",
    "wind",
    "hurricane",
    "climate",
)

# Categories / keywords that must NEVER be traded by this bot.
_FORBIDDEN_KEYWORDS = (
    "election", "president", "senate", "congress", "trump", "biden", "politic",
    "bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "doge",
    "nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis", "ufc", "boxing",
    "premier league", "champions league", "world cup", "super bowl", "olympics",
    "stock", "nasdaq", "s&p", "fed ", "interest rate", "gdp", "cpi", "earnings",
    "oscar", "grammy", "movie", "album", "celebrity", "twitter", "tweet",
)


# --------------------------------------------------------------------------- #
# Parsed market container                                                     #
# --------------------------------------------------------------------------- #
@dataclass
class OutcomeToken:
    label: str          # "YES" / "NO" (or the raw outcome label)
    token_id: str       # CLOB token id
    price: Optional[float] = None  # last/initial price if Gamma provides it


@dataclass
class ParsedMarket:
    market_id: str
    title: str
    description: str = ""
    rules: str = ""

    city: Optional[str] = None
    date: Optional[str] = None              # local YYYY-MM-DD
    market_type: str = UNKNOWN
    threshold_c: Optional[float] = None     # normalized to Celsius
    threshold_raw: Optional[float] = None   # as written in the market
    threshold_unit: str = "C"               # 'C' or 'F'

    outcomes: List[OutcomeToken] = field(default_factory=list)
    close_time: Optional[datetime] = None

    # Parse-confidence sub-scores (0..1)
    city_parse_confidence: float = 0.0
    date_parse_confidence: float = 0.0
    rules_clarity_score: float = 0.0

    parse_ok: bool = False
    is_exact: bool = False
    resolution_source: str = ""
    notes: List[str] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        self.notes.append(note)


class WeatherMarketScanner:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()
        # Cities the bot knows coordinates for (from the weather client table).
        self._known_cities = set(CITY_COORDS.keys())

    # ================================================================== #
    # Filtering                                                          #
    # ================================================================== #
    @staticmethod
    def _market_text(market: Dict[str, Any]) -> str:
        """Concatenate the human-readable fields of a raw Gamma market."""
        parts = [
            str(market.get("question") or ""),
            str(market.get("title") or ""),
            str(market.get("description") or ""),
            str(market.get("resolutionSource") or ""),
            str(market.get("rules") or ""),
        ]
        # Some Gamma payloads nest the question under an "events" or "tags" list.
        tags = market.get("tags")
        if isinstance(tags, list):
            parts.append(" ".join(str(t) for t in tags))
        return " \n".join(p for p in parts if p).strip()

    def is_temperature_market(self, market: Dict[str, Any]) -> bool:
        """True only for clearly temperature-related markets.

        Rejects anything matching the forbidden (sports/politics/etc.) list,
        even if it happens to mention a temperature-like word.
        """
        text = self._market_text(market).lower()
        if not text:
            return False

        if any(bad in text for bad in _FORBIDDEN_KEYWORDS):
            return False

        # Must mention temperature explicitly (not just generic "weather").
        if any(kw in text for kw in _TEMP_KEYWORDS):
            return True
        return False

    def is_weather_market(self, market: Dict[str, Any]) -> bool:
        text = self._market_text(market).lower()
        if any(bad in text for bad in _FORBIDDEN_KEYWORDS):
            return False
        return any(kw in text for kw in _WEATHER_KEYWORDS)

    def filter_markets(self, markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return only temperature markets the bot is allowed to consider."""
        allowed = set(self.config.enabled_categories)
        results = []
        for m in markets:
            if "temperature" in allowed and self.is_temperature_market(m):
                results.append(m)
            elif "weather" in allowed and self.is_temperature_market(m):
                # We deliberately restrict even "weather" to temperature markets,
                # because the predictor only understands temperature.
                results.append(m)
        log.info("Filtered %d temperature markets from %d candidates.", len(results), len(markets))
        return results

    # ================================================================== #
    # Parsing                                                            #
    # ================================================================== #
    def parse_market(self, market: Dict[str, Any]) -> ParsedMarket:
        market_id = str(market.get("id") or market.get("conditionId") or market.get("question_id") or "")
        title = str(market.get("question") or market.get("title") or "")
        description = str(market.get("description") or "")
        rules = str(market.get("resolutionSource") or market.get("rules") or description)

        pm = ParsedMarket(market_id=market_id, title=title, description=description, rules=rules)

        combined = f"{title}\n{description}\n{rules}"

        # 1) City
        pm.city, pm.city_parse_confidence = self._parse_city(combined)
        # 2) Date
        pm.date, pm.date_parse_confidence, pm.close_time = self._parse_date_and_close(market, combined)
        # 3) Market type + threshold
        self._parse_type_and_threshold(pm, combined)
        # 4) Outcomes (YES/NO token ids)
        pm.outcomes = self._parse_outcomes(market)
        # 5) Rules clarity / resolution source
        pm.rules_clarity_score, pm.resolution_source = self._assess_rules(combined)

        # Determine overall parse_ok: the essentials must be present.
        essentials_present = bool(
            pm.city
            and pm.date
            and pm.market_type != UNKNOWN
            and (pm.threshold_c is not None or pm.market_type in (EXACT, RANGE))
            and pm.outcomes
        )
        pm.parse_ok = essentials_present
        if not essentials_present:
            missing = []
            if not pm.city:
                missing.append("city")
            if not pm.date:
                missing.append("date")
            if pm.market_type == UNKNOWN:
                missing.append("market_type")
            if pm.threshold_c is None and pm.market_type not in (EXACT, RANGE):
                missing.append("threshold")
            if not pm.outcomes:
                missing.append("outcomes")
            pm.add_note(f"parse incomplete; missing: {', '.join(missing)}")

        return pm

    # ------------------------------------------------------------------ #
    # City parsing                                                       #
    # ------------------------------------------------------------------ #
    def _parse_city(self, text: str) -> Tuple[Optional[str], float]:
        lowered = text.lower()
        # Prefer an explicit "in <City>" phrase.
        m = re.search(r"\b(?:in|for|at)\s+([A-Za-z][A-Za-z .'-]{2,30})", text)
        candidate = None
        if m:
            candidate = m.group(1).strip().lower()
            # Trim trailing connective words.
            candidate = re.split(r"\b(on|will|be|today|tomorrow|reach|hit|exceed)\b", candidate)[0].strip()

        # Match against the known-city table first (highest confidence).
        for city in self._known_cities:
            if re.search(rf"\b{re.escape(city)}\b", lowered):
                return city, 0.98

        # If we extracted a candidate but it's not in our table, lower confidence.
        if candidate:
            candidate = candidate.strip(" .'-")
            if 2 < len(candidate) <= 30:
                # Unknown city -> we can still geocode, but confidence is reduced
                # because a wrong match risks a wrong forecast.
                return candidate, 0.55
        return None, 0.0

    # ------------------------------------------------------------------ #
    # Date / close-time parsing                                          #
    # ------------------------------------------------------------------ #
    def _parse_date_and_close(
        self, market: Dict[str, Any], text: str
    ) -> Tuple[Optional[str], float, Optional[datetime]]:
        # Close time from Gamma fields (authoritative for resolution timing).
        close_time = None
        for key in ("endDate", "end_date_iso", "endDateIso", "closeTime", "gameStartTime"):
            raw = market.get(key)
            if raw:
                close_time = parse_iso_datetime(str(raw))
                if close_time:
                    break

        # Date string in title/description.
        date_str = None
        confidence = 0.0

        # Pattern: 2025-06-01
        m_iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
        if m_iso:
            date_str = m_iso.group(0)
            confidence = 0.95

        if not date_str:
            # Pattern: "June 1", "Jan 5, 2025", "5 March"
            months = (
                "january|february|march|april|may|june|july|august|"
                "september|october|november|december|jan|feb|mar|apr|jun|"
                "jul|aug|sep|sept|oct|nov|dec"
            )
            m_named = re.search(
                rf"\b({months})\s+(\d{{1,2}})(?:,?\s*(20\d{{2}}))?\b", text, re.IGNORECASE
            )
            if not m_named:
                m_named = re.search(
                    rf"\b(\d{{1,2}})\s+({months})(?:,?\s*(20\d{{2}}))?\b", text, re.IGNORECASE
                )
            if m_named:
                parsed = self._named_date_to_iso(m_named, close_time)
                if parsed:
                    date_str = parsed
                    confidence = 0.85

        # Fall back to the close-time date if nothing in the text.
        if not date_str and close_time:
            date_str = close_time.strftime("%Y-%m-%d")
            confidence = 0.6  # inferred, not stated

        return date_str, confidence, close_time

    @staticmethod
    def _named_date_to_iso(match: re.Match, close_time: Optional[datetime]) -> Optional[str]:
        month_map = {
            "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
            "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
            "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
            "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
        }
        groups = [g for g in match.groups() if g is not None]
        month = None
        day = None
        year = None
        for g in groups:
            gl = g.lower()
            if gl in month_map:
                month = month_map[gl]
            elif g.isdigit() and len(g) == 4:
                year = int(g)
            elif g.isdigit():
                day = int(g)
        if month is None or day is None:
            return None
        if year is None:
            year = close_time.year if close_time else datetime.utcnow().year
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    # Market type + threshold parsing                                    #
    # ------------------------------------------------------------------ #
    def _parse_type_and_threshold(self, pm: ParsedMarket, text: str) -> None:
        lowered = text.lower()

        is_high = any(k in lowered for k in ("highest temp", "high temp", "maximum temp", "max temp", "warmest"))
        is_low = any(k in lowered for k in ("lowest temp", "low temp", "minimum temp", "min temp", "coldest"))
        # If neither high nor low is explicit but "temperature" is present,
        # default to high (the most common daily-temperature market) but flag it.
        if not is_high and not is_low and "temperature" in lowered:
            is_high = True
            pm.add_note("high/low not explicit; assumed daily high (reduces confidence)")
            pm.rules_clarity_score = min(pm.rules_clarity_score, 0.5)

        # Direction words.
        above = any(k in lowered for k in (">=", "≥", "above", "over", "greater than", "more than", "at least", "exceed", "higher than", "or more"))
        below = any(k in lowered for k in ("<=", "≤", "below", "under", "less than", "lower than", "at most", "or less", "fewer than"))

        # "between X and Y" -> RANGE (ambiguous for our purpose). The hyphen
        # form must be followed by a degree/unit so we don't misread dates
        # like "2026-06-01" as a temperature range.
        if re.search(r"\bbetween\b.*\band\b", lowered) or re.search(
            r"-?\d{1,3}\s*[-\u2013]\s*-?\d{1,3}\s*(?:°|\u00b0|degrees?|deg\b|c\b|f\b)", lowered
        ):
            pm.market_type = RANGE
            pm.is_exact = True

        # "exactly X" -> EXACT.
        if "exactly" in lowered or "exact" in lowered:
            pm.market_type = EXACT
            pm.is_exact = True

        # Threshold value + unit.
        threshold, unit = self._extract_threshold(text)
        if threshold is not None:
            pm.threshold_raw = threshold
            pm.threshold_unit = unit
            pm.threshold_c = threshold if unit == "C" else (threshold - 32.0) * 5.0 / 9.0

        # Assign the comparison-based market type (only if not already EXACT/RANGE).
        if pm.market_type not in (EXACT, RANGE):
            if is_high and above:
                pm.market_type = HIGH_ABOVE
            elif is_high and below:
                pm.market_type = HIGH_BELOW
            elif is_low and below:
                pm.market_type = LOW_BELOW
            elif is_low and above:
                pm.market_type = LOW_ABOVE
            elif is_high:
                # "Highest temperature ... <threshold>?" without explicit dir.
                # Polymarket usually frames these as ">= threshold" YES markets.
                if threshold is not None:
                    pm.market_type = HIGH_ABOVE
                    pm.add_note("direction not explicit; assumed >= for daily high")
                    pm.rules_clarity_score = min(pm.rules_clarity_score or 1.0, 0.5)
                else:
                    pm.market_type = UNKNOWN
            elif is_low:
                if threshold is not None:
                    pm.market_type = LOW_BELOW
                    pm.add_note("direction not explicit; assumed <= for daily low")
                    pm.rules_clarity_score = min(pm.rules_clarity_score or 1.0, 0.5)
                else:
                    pm.market_type = UNKNOWN
            else:
                pm.market_type = UNKNOWN

    @staticmethod
    def _extract_threshold(text: str) -> Tuple[Optional[float], str]:
        """Extract a threshold temperature and its unit from the market text."""
        # Prefer numbers adjacent to an explicit unit.
        m = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°|\u00b0)?\s*(c|f|celsius|fahrenheit)\b", text, re.IGNORECASE)
        if m:
            value = safe_float(m.group(1))
            unit = "F" if m.group(2).lower().startswith("f") else "C"
            return value, unit
        # Otherwise a bare number near a degree symbol or "degrees".
        m2 = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*(?:°|\u00b0|degrees?)", text, re.IGNORECASE)
        if m2:
            value = safe_float(m2.group(1))
            unit = detect_temp_unit(text) or "C"
            return value, unit
        return None, "C"

    # ------------------------------------------------------------------ #
    # Outcomes / token ids                                               #
    # ------------------------------------------------------------------ #
    def _parse_outcomes(self, market: Dict[str, Any]) -> List[OutcomeToken]:
        outcomes: List[OutcomeToken] = []

        labels = self._maybe_json_list(market.get("outcomes"))
        token_ids = self._maybe_json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        prices = self._maybe_json_list(market.get("outcomePrices") or market.get("outcome_prices"))

        if labels and token_ids and len(labels) == len(token_ids):
            for i, label in enumerate(labels):
                price = safe_float(prices[i]) if prices and i < len(prices) else None
                outcomes.append(
                    OutcomeToken(label=str(label).upper(), token_id=str(token_ids[i]), price=price)
                )
        elif token_ids:
            # Fallback: assume binary YES/NO ordering.
            default_labels = ["YES", "NO"]
            for i, tid in enumerate(token_ids):
                label = default_labels[i] if i < len(default_labels) else f"OUTCOME_{i}"
                price = safe_float(prices[i]) if prices and i < len(prices) else None
                outcomes.append(OutcomeToken(label=label, token_id=str(tid), price=price))
        return outcomes

    @staticmethod
    def _maybe_json_list(value: Any) -> List[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                return []
        return []

    # ------------------------------------------------------------------ #
    # Rules clarity                                                      #
    # ------------------------------------------------------------------ #
    def _assess_rules(self, text: str) -> Tuple[float, str]:
        """Score how clear the resolution rules are (0..1) and detect source.

        High clarity requires: a named, reputable data source and an
        unambiguous comparison. Vague phrasing knocks the score down so the
        bot skips.
        """
        lowered = text.lower()
        score = 0.5  # neutral baseline
        source = ""

        reputable_sources = [
            "national weather service", "nws", "noaa", "weather.gov",
            "met office", "metoffice", "accuweather", "weather underground",
            "wunderground", "open-meteo", "meteostat", "visual crossing",
            "weatherapi", "ogimet", "iem", "iowa environmental mesonet",
        ]
        for s in reputable_sources:
            if s in lowered:
                score += 0.35
                source = s
                break

        # Clear comparison language present.
        if any(k in lowered for k in (">=", "≥", "<=", "≤", "above", "below", "at least", "at most", "high temperature", "low temperature")):
            score += 0.1

        # Ambiguity / vagueness penalties.
        vague_terms = ["approximately", "around", "about", "feels like", "subject to", "may be", "estimate", "tbd", "to be determined"]
        if any(v in lowered for v in vague_terms):
            score -= 0.3
        if not source:
            score -= 0.15  # no named source = harder to trust resolution

        # Clamp.
        score = max(0.0, min(1.0, score))
        return round(score, 4), source


def scan_and_parse(
    markets: List[Dict[str, Any]], config: Optional[Config] = None
) -> List[ParsedMarket]:
    """Filter to temperature markets and parse each one."""
    scanner = WeatherMarketScanner(config)
    temp_markets = scanner.filter_markets(markets)
    return [scanner.parse_market(m) for m in temp_markets]
