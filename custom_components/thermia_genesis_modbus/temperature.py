"""Temperature selection independent of Home Assistant for easy verification."""

import math
from typing import NamedTuple


class TemperatureSample(NamedTuple):
    """A converted reading and an explanation when it cannot be selected."""

    value: float | None
    reason: str
    age_seconds: float | None = None
    unit: str | None = None


def temperature_source_status(external_status, fallback_status, *, inside=False):
    """A readable source diagnosis, independent of temperature availability."""
    if external_status == "valid":
        return "External sensor in use"
    if fallback_status == "valid":
        return "Thermia room sensor in use" if inside else "Thermia outside sensor in use"
    if external_status == "not_selected":
        location = "inside" if inside else "outside"
        return f"No {location} sensor selected; Thermia unavailable"
    return {
        "not_found": "Selected sensor not found",
        "unknown": "Selected sensor reports unknown",
        "unavailable": "Selected sensor unavailable",
        "stale": "Selected sensor reading is stale",
        "unsupported_unit": "Selected sensor unit is unsupported",
        "invalid_report_time": "Selected sensor report time is invalid",
        "invalid_value": "Selected sensor has invalid temperature",
        "out_of_range": "Selected sensor temperature is out of range",
    }.get(external_status, "No valid temperature available")


def _converted_temperature(value, unit, *, inside=False):
    if isinstance(value, bool):
        return None, "invalid_value", None
    if isinstance(value, str) and value.strip().lower() in {"unknown", "unavailable"}:
        return None, value.strip().lower(), None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None, "invalid_value", None
    if not math.isfinite(numeric):
        return None, "invalid_value", None
    normalized = str(unit or "").strip().lower().replace("º", "°")
    if normalized in ("", "°c", "c", "celsius", "degc", "degrees celsius"):
        normalized = "°C"
    elif normalized in ("°f", "f", "fahrenheit", "degf", "degrees fahrenheit"):
        numeric = (numeric - 32) * 5 / 9
        normalized = "°F"
    elif normalized in ("k", "°k", "kelvin"):
        numeric -= 273.15
        normalized = "K"
    else:
        return None, "unsupported_unit", str(unit)
    minimum, maximum = (-10, 60) if inside else (-50, 60)
    if not minimum <= numeric <= maximum:
        return None, "out_of_range", normalized
    return numeric, "valid", normalized


def valid_temperature(value, unit: str | None = "°C", *, inside: bool = False):
    """Accept finite temperatures; selected unitless sensors are read as Celsius."""
    return _converted_temperature(value, unit, inside=inside)[0]


def sensor_timeout_seconds(value):
    """A missing or zero timeout leaves valid Home Assistant state usable."""
    try:
        timeout = float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
    return timeout if math.isfinite(timeout) and timeout > 0 else 0.0


def temperature_sample(state, now, timeout=None, *, inside: bool = False):
    """Apply report freshness only when the user selects a positive timeout."""
    if state is None:
        return TemperatureSample(None, "not_found")
    attributes = getattr(state, "attributes", {})
    value, reason, unit = _converted_temperature(
        state.state, attributes.get("unit_of_measurement"), inside=inside
    )
    reported = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
    try:
        age = (now - reported).total_seconds() if reported is not None else None
    except (TypeError, ValueError, OverflowError):
        age = None
    if reason != "valid":
        return TemperatureSample(None, reason, age, unit)
    freshness = sensor_timeout_seconds(timeout)
    if freshness:
        if age is None or age < -60:
            return TemperatureSample(None, "invalid_report_time", age, unit)
        if age > freshness:
            return TemperatureSample(None, "stale", age, unit)
    return TemperatureSample(value, "valid", age, unit)


def fresh_temperature(state, now, timeout=None, *, inside: bool = False):
    """Return the selected value, retaining the previous public helper API."""
    return temperature_sample(state, now, timeout, inside=inside).value
