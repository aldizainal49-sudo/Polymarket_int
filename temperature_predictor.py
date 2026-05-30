"""
temperature_predictor.py
========================
The brain of the bot. Given a parsed market and a weather forecast, it decides
which outcome (if any) is a high-confidence trade and produces:

* ``bot_probability``  - the bot's estimated probability that the chosen
  outcome resolves the way we want, derived from how far the forecast sits
  from the threshold.
* ``confidence_score`` - a harsher composite (geometric mean) of eight
  sub-scores. If any single input is weak, this collapses and the bot skips.

Prediction logic (exactly as specified)
----------------------------------------
A. HIGH_ABOVE  (highest temp >= threshold):
   forecast high well ABOVE threshold  -> YES is high confidence.
   forecast high near threshold        -> SKIP.
   forecast high below threshold       -> do NOT enter YES.

B. HIGH_BELOW  (highest temp < threshold):
   forecast high well BELOW threshold  -> YES (the "below" outcome) high conf.
   near threshold                      -> SKIP.

C. LOW_BELOW   (lowest temp <= threshold):
   forecast low well BELOW threshold   -> YES high conf.
   near threshold                      -> SKIP.

D. LOW_ABOVE   (lowest temp > threshold):
   forecast low well ABOVE threshold   -> YES high conf.
   near threshold                      -> SKIP.

E. EXACT / RANGE:
   default SKIP (too hard). Only considered if ALLOW_EXACT_TEMP_MARKETS=true,
   and even then with a stricter margin.

"Well above/below" means the forecast clears the threshold by at least the
configured safety margin (MIN_TEMP_DISTANCE_C / _F).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Optional

from config import Config, get_config
from utils import (
    SKIP_EXACT_TEMP_DISABLED,
    SKIP_FORECAST_TOO_CLOSE,
    SKIP_LOW_CONFIDENCE,
    SKIP_PARSE_FAILED,
    SKIP_PREDICTION_CONFLICT,
    SKIP_RULES_AMBIGUOUS,
    SKIP_TOO_FAR_FROM_RESOLUTION,
    SKIP_WEATHER_DATA_INCOMPLETE,
    TRADE_HIGH_CONFIDENCE,
    clamp,
    distance_score,
    geometric_mean,
    get_logger,
    hours_until,
    time_score,
)
from weather_data_client import Forecast
from weather_market_scanner import (
    EXACT,
    HIGH_ABOVE,
    HIGH_BELOW,
    LOW_ABOVE,
    LOW_BELOW,
    RANGE,
    ParsedMarket,
)

log = get_logger("predictor")


@dataclass
class Prediction:
    """Outcome of the predictor for a single market."""

    market_id: str
    title: str
    target_outcome: Optional[str] = None     # "YES" / "NO" we'd buy
    bot_probability: float = 0.0
    confidence_score: float = 0.0
    temp_distance_c: Optional[float] = None
    forecast_value_c: Optional[float] = None  # the relevant high or low used
    threshold_c: Optional[float] = None
    decision: str = SKIP_LOW_CONFIDENCE
    skip_reason: Optional[str] = SKIP_LOW_CONFIDENCE
    components: Dict[str, float] = field(default_factory=dict)
    rationale: str = ""

    @property
    def is_trade(self) -> bool:
        return self.decision == TRADE_HIGH_CONFIDENCE

    def components_json(self) -> str:
        try:
            return json.dumps(self.components)
        except (TypeError, ValueError):
            return "{}"


class TemperaturePredictor:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()

    # ------------------------------------------------------------------ #
    # Public entry point                                                 #
    # ------------------------------------------------------------------ #
    def predict(self, pm: ParsedMarket, forecast: Forecast) -> Prediction:
        pred = Prediction(market_id=pm.market_id, title=pm.title)

        # --- Hard gate 1: parsing must have succeeded -------------------
        if not pm.parse_ok:
            return self._skip(pred, SKIP_PARSE_FAILED, "market parsing incomplete")

        # --- Hard gate 2: rules must be clear enough --------------------
        if pm.rules_clarity_score < 0.6 and not self.config.allow_ambiguous_markets:
            return self._skip(pred, SKIP_RULES_AMBIGUOUS, "resolution rules too ambiguous")

        # --- Hard gate 3: weather data must be complete -----------------
        if not forecast.ok or forecast.daily_high_c is None or forecast.daily_low_c is None:
            return self._skip(
                pred, SKIP_WEATHER_DATA_INCOMPLETE, f"forecast incomplete (q={forecast.data_quality})"
            )

        # --- Hard gate 4: resolution timing window ----------------------
        hrs = hours_until(pm.close_time) if pm.close_time else None
        if hrs is not None:
            if hrs < 0:
                return self._skip(pred, SKIP_TOO_FAR_FROM_RESOLUTION, "market already closed")
            if hrs > self.config.absolute_max_hours_to_resolution:
                return self._skip(
                    pred, SKIP_TOO_FAR_FROM_RESOLUTION, f"{hrs:.1f}h to resolution > absolute cap"
                )
            # Beyond MAX but within absolute cap is only allowed if VERY clear.
            if hrs > self.config.max_hours_to_resolution:
                if pm.rules_clarity_score < 0.9:
                    return self._skip(
                        pred,
                        SKIP_TOO_FAR_FROM_RESOLUTION,
                        f"{hrs:.1f}h > preferred max and rules not crystal-clear",
                    )

        # --- Exact / range markets: skip unless explicitly enabled ------
        if pm.market_type in (EXACT, RANGE):
            if not self.config.allow_exact_temp_markets:
                return self._skip(
                    pred, SKIP_EXACT_TEMP_DISABLED, "exact/range market disabled by config"
                )

        # --- Required safety margin -------------------------------------
        # Use the unit the threshold was stated in for the distance check.
        if pm.threshold_unit == "F":
            required_margin_c = self.config.min_temp_distance_f * 5.0 / 9.0
        else:
            required_margin_c = self.config.min_temp_distance_c

        # Exact/range markets (if enabled) demand an even bigger margin.
        if pm.market_type in (EXACT, RANGE):
            required_margin_c *= 1.5

        # --- Core directional logic -------------------------------------
        outcome, fc_value_c, signed_distance = self._evaluate_direction(pm, forecast)
        if outcome is None:
            return self._skip(pred, SKIP_PREDICTION_CONFLICT, "could not map forecast to an outcome")

        pred.target_outcome = outcome
        pred.forecast_value_c = fc_value_c
        pred.threshold_c = pm.threshold_c
        pred.temp_distance_c = signed_distance

        # If the forecast points the WRONG way (e.g. high below threshold for a
        # ">=" market), never enter the YES side.
        if signed_distance is not None and signed_distance <= 0:
            return self._skip(
                pred,
                SKIP_PREDICTION_CONFLICT,
                "forecast does not support the favourable outcome",
            )

        abs_distance = abs(signed_distance) if signed_distance is not None else 0.0

        # Too close to the threshold -> SKIP (the single most important rule).
        if abs_distance < required_margin_c:
            return self._skip(
                pred,
                SKIP_FORECAST_TOO_CLOSE,
                f"forecast only {abs_distance:.2f}C from threshold (need {required_margin_c:.2f}C)",
            )

        # --- Build the component scores ---------------------------------
        dist_score = distance_score(abs_distance, required_margin_c)
        t_score = time_score(
            hrs, self.config.preferred_hours_to_resolution, self.config.max_hours_to_resolution
        ) if hrs is not None else 0.6  # unknown timing -> mediocre, not zero
        components = {
            "city_parse_confidence": clamp(pm.city_parse_confidence),
            "date_parse_confidence": clamp(pm.date_parse_confidence),
            "rules_clarity_score": clamp(pm.rules_clarity_score),
            "weather_data_quality": clamp(forecast.data_quality),
            "distance_from_threshold_score": clamp(dist_score),
            "time_to_resolution_score": clamp(t_score),
            # Liquidity & spread scores are injected later by the pipeline
            # (orderbook_analyzer) - default to neutral here so predict() can
            # run standalone in tests. The pipeline overwrites them.
            "liquidity_score": 1.0,
            "spread_score": 1.0,
        }
        pred.components = components

        # bot_probability: how sure are we the outcome resolves favourably,
        # based purely on the forecast margin. Mapped conservatively.
        pred.bot_probability = self._margin_to_probability(abs_distance, required_margin_c)

        # confidence_score: harsh geometric mean of all components.
        pred.confidence_score = round(geometric_mean(list(components.values())), 4)

        # --- Final gate: probability & confidence must both clear 95% ---
        if pred.bot_probability < self.config.min_bot_probability:
            return self._skip(
                pred,
                SKIP_LOW_CONFIDENCE,
                f"bot_probability {pred.bot_probability:.3f} < {self.config.min_bot_probability}",
            )
        if pred.confidence_score < self.config.min_confidence_score:
            return self._skip(
                pred,
                SKIP_LOW_CONFIDENCE,
                f"confidence_score {pred.confidence_score:.3f} < {self.config.min_confidence_score}",
            )

        pred.decision = TRADE_HIGH_CONFIDENCE
        pred.skip_reason = None
        pred.rationale = (
            f"{pm.market_type}: forecast {fc_value_c:.1f}C vs threshold "
            f"{pm.threshold_c:.1f}C, margin {abs_distance:.1f}C >= {required_margin_c:.1f}C; "
            f"buy {outcome}"
        )
        return pred

    # ------------------------------------------------------------------ #
    # Directional evaluation (logic A-E)                                 #
    # ------------------------------------------------------------------ #
    def _evaluate_direction(self, pm: ParsedMarket, forecast: Forecast):
        """Return (target_outcome, forecast_value_c, signed_distance_c).

        ``signed_distance`` is positive when the forecast supports the
        favourable (YES) outcome, negative when it contradicts it, and the
        magnitude is the margin in Celsius. Returns (None, value, None) if the
        market type can't be evaluated.
        """
        thr = pm.threshold_c
        high = forecast.daily_high_c
        low = forecast.daily_low_c

        if pm.market_type == HIGH_ABOVE:
            # YES wins if daily high >= threshold. Favourable when high > thr.
            return "YES", high, (high - thr) if (high is not None and thr is not None) else None

        if pm.market_type == HIGH_BELOW:
            # YES wins if daily high < threshold. Favourable when high < thr,
            # so positive margin = thr - high.
            return "YES", high, (thr - high) if (high is not None and thr is not None) else None

        if pm.market_type == LOW_BELOW:
            # YES wins if daily low <= threshold. Favourable when low < thr.
            return "YES", low, (thr - low) if (low is not None and thr is not None) else None

        if pm.market_type == LOW_ABOVE:
            # YES wins if daily low > threshold. Favourable when low > thr.
            return "YES", low, (low - thr) if (low is not None and thr is not None) else None

        if pm.market_type in (EXACT, RANGE):
            # Only reachable when ALLOW_EXACT_TEMP_MARKETS=true. We treat the
            # daily high as the reference and require it to sit far from the
            # exact target (i.e. we'd be buying the NO/"not exactly" side).
            if high is not None and thr is not None:
                return "NO", high, abs(high - thr)
            return None, high, None

        return None, high, None

    # ------------------------------------------------------------------ #
    # Margin -> probability mapping                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _margin_to_probability(abs_distance_c: float, required_margin_c: float) -> float:
        """Convert a forecast margin into a conservative win probability.

        At exactly the required margin we assign 0.95 (the floor for a trade).
        The probability rises toward ~0.995 as the margin grows to ~3x the
        requirement, and is capped below 1.0 - the bot never claims certainty.
        """
        if required_margin_c <= 0:
            required_margin_c = 1.0
        ratio = abs_distance_c / required_margin_c
        if ratio < 1.0:
            # Below the safety margin: well under the trade floor.
            return clamp(0.5 + 0.45 * ratio, 0.0, 0.949)
        # ratio in [1, 3] -> probability in [0.95, 0.995], capped.
        prob = 0.95 + 0.045 * clamp((ratio - 1.0) / 2.0, 0.0, 1.0)
        return round(min(prob, 0.995), 4)

    # ------------------------------------------------------------------ #
    # Skip helper                                                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _skip(pred: Prediction, reason: str, rationale: str) -> Prediction:
        pred.decision = reason
        pred.skip_reason = reason
        pred.rationale = rationale
        log.debug("SKIP %s | %s | %s", pred.market_id, reason, rationale)
        return pred
