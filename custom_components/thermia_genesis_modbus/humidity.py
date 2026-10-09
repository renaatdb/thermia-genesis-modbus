"""Validate optional indoor humidity and derive a read-only cooling limit.

The Magnus expression over water uses a=17.62 and b=243.12 °C. Its inversion
is given in NASA AIAA 2024-4671, equation 23:
https://www.nas.nasa.gov/assets/nas/pdf/staff/Nemec_M_AIAA_2024-4671.pdf
"""

from __future__ import annotations

import math
from typing import NamedTuple

from .temperature import sensor_timeout_seconds


class HumiditySample(NamedTuple):
    """A percentage reading and the reason it can or cannot be selected."""

    value: float | None
    reason: str
    age_seconds: float | None = None
    unit: str | None = None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _converted_humidity(value, unit):
    if isinstance(value, str) and value.strip().lower() in {"unknown", "unavailable"}:
        return None, value.strip().lower(), None
    numeric = _number(value)
    if numeric is None:
        return None, "invalid_value", None
    if str(unit or "").strip() != "%":
        return None, "unsupported_unit", str(unit) if unit is not None else None
    if not 0 < numeric <= 100:
        return None, "out_of_range", "%"
    return numeric, "valid", "%"


def valid_humidity(value, unit="%"):
    """Accept finite relative humidity percentages greater than zero."""
    return _converted_humidity(value, unit)[0]


def humidity_sample(state, now, timeout=None):
    """Use the temperature sensors' opt-in report-freshness semantics."""
    if state is None:
        return HumiditySample(None, "not_found")
    attributes = getattr(state, "attributes", {})
    value, reason, unit = _converted_humidity(
        state.state, attributes.get("unit_of_measurement")
    )
    reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    try:
        age = (now - reported).total_seconds() if reported is not None else None
    except (TypeError, ValueError, OverflowError):
        age = None
    if reason != "valid":
        return HumiditySample(None, reason, age, unit)
    freshness = sensor_timeout_seconds(timeout)
    if freshness:
        if age is None or age < -60:
            return HumiditySample(None, "invalid_report_time", age, unit)
        if age > freshness:
            return HumiditySample(None, "stale", age, unit)
    return HumiditySample(value, "valid", age, unit)


def dew_point(inside, humidity):
    """Return dew point in Celsius for plausible indoor temperature and RH.

    Keep calculation precision for the subsequent safe upward rounding of a
    water-supply limit. At saturation the dew point is exactly the air temperature.
    """
    temperature = _number(inside)
    relative_humidity = valid_humidity(humidity)
    if temperature is None or not -20 <= temperature <= 60 or relative_humidity is None:
        return None
    if relative_humidity == 100:
        return temperature
    # Subtract logs rather than dividing first: even a tiny positive RH remains
    # finite instead of underflowing to log(0).
    gamma = (
        math.log(relative_humidity) - math.log(100)
        + 17.62 * temperature / (243.12 + temperature)
    )
    return 243.12 * gamma / (17.62 - gamma)


def cooling_guard_info(inside, humidity, limit=65, margin=2, *, configured=True):
    """Describe cooling protection without changing controller policy or state.

    The engine applies resume hysteresis and keeps any existing supply target
    above this minimum. Without a selected humidity sensor this guard is inactive.
    """
    temperature = _number(inside)
    if temperature is not None and not -20 <= temperature <= 60:
        temperature = None
    relative_humidity = valid_humidity(humidity)
    humidity_limit = _number(limit)
    dew_point_margin = _number(margin)
    point = dew_point(temperature, relative_humidity)
    valid_settings = (
        humidity_limit is not None
        and 30 <= humidity_limit <= 90
        and humidity_limit.is_integer()
        and dew_point_margin is not None
        and 1 <= dew_point_margin <= 5
        and dew_point_margin.is_integer()
    )
    minimum = math.ceil(point + dew_point_margin) if point is not None and valid_settings else None
    status = (
        "no_sensor_selected"
        if not configured
        else "invalid_settings"
        if not valid_settings
        else "missing_humidity"
        if relative_humidity is None
        else "missing_inside_temperature"
        if temperature is None
        else "high_humidity"
        if relative_humidity >= humidity_limit
        else "ready"
    )
    return {
        "configured": bool(configured),
        "allowed": status in {"no_sensor_selected", "ready"},
        "status": status,
        "inside_temperature": temperature,
        "relative_humidity": relative_humidity,
        "humidity_limit": humidity_limit,
        "dew_point": point,
        "dew_point_margin": dew_point_margin,
        "min_cooling_supply": minimum,
    }
