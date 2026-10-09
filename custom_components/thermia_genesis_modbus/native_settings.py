"""Verified native settings with conservative integration slider limits.

Addresses and the Celsius ×100 wire encoding come from Thermia's domestic
Genesis 17.1 protocol, ACMBDH01UG0402. The protocol does not publish permitted
temperature ranges. Bounds below are software limits, not controller maxima.
Transport decoding/encoding handles the wire scale independently.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple


class NativeSetting(NamedTuple):
    key: str
    label: str
    address: int
    min_value: float
    max_value: float
    step: float = 1.0


NATIVE_SETTINGS = {
    item.key: item
    for item in (
        NativeSetting("heating_season_stop", "Heat Stop", 16, -10, 40),
        NativeSetting("min_supply_temperature", "Supply Line Minimum", 4, 5, 65),
        NativeSetting("max_supply_temperature", "Supply Line Maximum", 3, 5, 65),
        NativeSetting(
            "passive_cooling_supply_target", "Desired Cooling Supply (Mixing Valve 1)", 302, 5, 30
        ),
        *(
            NativeSetting(
                f"heat_curve_supply_{i + 1}",
                f"Heat Curve Point {i + 1}"
                + (" (Warmest Outside)" if i == 0 else " (Coldest Outside)" if i == 6 else ""),
                6 + i,
                5,
                65,
            )
            for i in range(7)
        ),
    )
}


def validate_native_setting(key: str, value: Any, *, require_step: bool = True) -> float:
    """Check a requested slider value, or a finite supported native readback."""
    if key not in NATIVE_SETTINGS:
        raise ValueError(f"Unsupported native setting: {key}")
    spec = NATIVE_SETTINGS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{spec.label} needs a valid numeric temperature")
    result = float(value)
    if not math.isfinite(result) or not spec.min_value <= result <= spec.max_value:
        raise ValueError(
            f"{spec.label} must be between {spec.min_value:g} and {spec.max_value:g}°C "
            "within the integration's software limits"
        )
    if require_step:
        steps = (result - spec.min_value) / spec.step
        if not math.isclose(steps, round(steps), abs_tol=1e-8):
            raise ValueError(f"{spec.label} must use {spec.step:g}°C steps")
    return result
