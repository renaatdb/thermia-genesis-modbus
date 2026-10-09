"""Read-only countdown details shared by thermostats and duration sensors."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _utc_timestamp(value: Any) -> tuple[float | None, str | None]:
    number = _finite_number(value)
    if number is None or number < 0:
        return None, None
    try:
        return number, datetime.fromtimestamp(number, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None, None


def _countdown_info(
    coordinator: Any, *, prefix: str, duration: Any, active: bool, pending: bool
) -> dict[str, Any]:
    """Describe a saved deadline without changing control state or hardware."""
    engine = coordinator.engine
    state = engine.state
    _, started_at = _utc_timestamp(state.get(f"{prefix}_started_at"))
    deadline, formatted_deadline = _utc_timestamp(state.get(f"{prefix}_deadline"))
    try:
        now = _finite_number(engine.now())
    except (TypeError, ValueError, OverflowError, OSError):
        now = None
    if now is not None and now < 0:
        now = None
    remaining_seconds = (
        max(0.0, deadline - now) if active and deadline is not None and now is not None else 0.0
    )
    if active and getattr(coordinator, "_recover_pending", False):
        pending = True
    return {
        "active": active,
        "started_at": started_at,
        "deadline": formatted_deadline,
        "configured_duration_hours": _finite_number(duration),
        "remaining_hours": remaining_seconds / 3600,
        "reset_pending": bool(pending or (active and remaining_seconds == 0)),
        "controller_available": bool(coordinator.last_update_success),
    }


def excess_time_info(coordinator: Any, group: str) -> dict[str, Any]:
    """Report an existing Excess Energy countdown without changing its identity."""
    state = coordinator.engine.state
    backup = state.get("configuration_restore") or {}
    active = (
        state.get("heating_preset") == "pv_charge"
        if group == "heating"
        else state.get("hot_water_mode") == "energy_excess"
    )
    pending = (
        group in state.get("pending_restore", ())
        or group in state.get("charging_cancelled", ())
        or group in backup.get("expired_excess_groups", ())
    )
    return _countdown_info(
        coordinator,
        prefix=f"{group}_excess",
        duration=coordinator.engine.settings.get(f"{group}_excess_hours"),
        active=active,
        pending=pending,
    )


def low_time_info(coordinator: Any) -> dict[str, Any]:
    """Report the independent Low Mode deadline and any pending reset."""
    state = coordinator.engine.state
    backup = state.get("configuration_restore") or {}
    return _countdown_info(
        coordinator,
        prefix="hot_water_low",
        duration=coordinator.engine.settings.get("hot_water_low_hours", 24),
        active=state.get("hot_water_mode") == "evening",
        pending=(
            "hot_water" in state.get("pending_restore", ())
            or "hot_water" in state.get("charging_cancelled", ())
            or bool(backup.get("expired_low_mode"))
        ),
    )


def excess_time_attributes(coordinator: Any, group: str) -> dict[str, Any]:
    """Keep the existing thermostat attributes while using the engine's clock."""
    info = excess_time_info(coordinator, group)
    return {
        "excess_energy_duration_hours": info["configured_duration_hours"],
        "excess_energy_started_at": info["started_at"],
        "excess_energy_deadline": info["deadline"],
        "excess_energy_remaining_hours": (
            round(info["remaining_hours"], 3) if info["deadline"] is not None else None
        ),
        "excess_energy_reset_pending": info["reset_pending"],
    }


def low_time_attributes(coordinator: Any) -> dict[str, Any]:
    """Expose Low Mode countdown details on the hot-water thermostat."""
    info = low_time_info(coordinator)
    return {
        "low_mode_duration_hours": info["configured_duration_hours"],
        "low_mode_started_at": info["started_at"],
        "low_mode_deadline": info["deadline"],
        "low_mode_remaining_hours": (
            round(info["remaining_hours"], 3) if info["deadline"] is not None else None
        ),
        "low_mode_reset_pending": info["reset_pending"],
    }
