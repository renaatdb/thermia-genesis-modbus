"""Durable, Home Assistant independent Thermia control policy.

The writer is responsible for checking register permissions and verifying reads
after every write. Callers must serialize commands and polling. No service mode
or direct compressor command is used here: these are native operating requests.
"""

from __future__ import annotations

import math
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable

from .humidity import cooling_guard_info, valid_humidity
from .native_settings import NATIVE_SETTINGS, validate_native_setting

Read = Callable[[str], Any]
Write = Callable[[str, Any], Awaitable[None]]
Persist = Callable[[dict[str, Any]], Awaitable[None]]


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _bounded(value: Any, minimum: float, maximum: float, name: str) -> float:
    result = _number(value)
    if result is None or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return result


def _whole_temperature(value: Any, minimum: float, maximum: float, name: str) -> float:
    result = _bounded(value, minimum, maximum, name)
    if not result.is_integer():
        raise ValueError(f"{name} must use whole-degree temperature steps")
    return result


class ControlEngine:
    """Coordinate thermostats and restore temporary PV charging overrides."""

    def __init__(
        self,
        read: Read,
        write: Write,
        persist: Persist,
        settings: dict[str, Any],
        state: dict[str, Any] | None = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.read = read
        self.write = write
        self.persist = persist
        self.now = now
        self.settings = {
            "control_mode": "internal",
            "hysteresis": 0.3,
            "hot_water_hysteresis": 2.0,
            "max_start_temperature": 55.0,
            "max_hot_water_temperature": 60.0,
            "smart_grid_mode": "disabled",
            "enable_undocumented_controls": False,
            "heating_excess_hours": 12.0,
            "heating_excess_heat_stop_offset": 2.0,
            "hot_water_excess_hours": 6.0,
            "hot_water_evening_start": 35.0,
            "hot_water_evening_stop": 40.0,
            "hot_water_low_hours": 24.0,
            "heating_low_offset": 2.0,
            "heating_vacation_temperature": 17.0,
            "heating_vacation_cooling_offset": 0.0,
            "inside_humidity_sensor": "",
            "cooling_humidity_enabled": False,
            "cooling_humidity_limit": 80.0,
            "cooling_vacation_humidity_limit": 65.0,
            "cooling_dew_point_margin": 2.0,
            **deepcopy(settings),
        }
        self.settings.pop("charge_supply_temperature", None)
        self.state: dict[str, Any] = {
            "heating_mode": "off",
            "last_heating_mode": "heat",
            "heating_preset": "normal",
            "heating_target": None,
            "heating_low": 20.0,
            "heating_high": 24.0,
            "hot_water_mode": "auto",
            "hot_water_target": None,
            "hot_water_start": None,
            "managed_heating": False,
            "managed_hot_water": False,
            "overrides": {},
            "charge_active": {"heating": False, "hot_water": False},
            "sg_owners": [],
            "pending_restore": [],
            "boost_once": False,
            "boost_enabled": False,
            "boost_previous_mode": None,
            "boost_previous_managed": False,
            "hot_water_exit_mode": None,
            "native_boost_available": False,
            "native_boost_coupled": False,
            "control_warning": None,
            "settings": {},
            "charging_cancelled": [],
            "heating_excess_started_at": None,
            "heating_excess_deadline": None,
            "heating_excess_previous_mode": None,
            "heating_excess_previous_managed": False,
            "hot_water_excess_started_at": None,
            "hot_water_excess_deadline": None,
            "hot_water_low_started_at": None,
            "hot_water_low_deadline": None,
            "cooling_humidity_requested": False,
            "cooling_humidity_paused": False,
            "cooling_humidity_high": False,
            "cooling_humidity_resume_pending": False,
            **deepcopy(state or {}),
        }
        self.state.setdefault("charge_active", {}).setdefault("heating", False)
        self.state["charge_active"].setdefault("hot_water", False)
        self.state["settings"].pop("charge_supply_temperature", None)
        # Current config-entry options win over older saved runtime values.
        # Standalone callers may still recover settings omitted from their
        # explicit configuration, except hardware maxima requiring confirmation.
        for name, value in self.state.get("settings", {}).items():
            if name not in settings and name not in {
                "max_hot_water_temperature",
                "max_start_temperature",
            }:
                self._validate_setting(name, value)
                self.settings[name] = value
        self._inside: float | None = None
        self._outdoor: float | None = None
        self._thermal_action = "idle"
        self._humidity: float | None = None
        self._restoring = False
        self._hydrate()

    async def _save(self) -> None:
        await self.persist(deepcopy(self.state))

    @property
    def external_control(self) -> bool:
        return self.settings.get("control_mode") == "external"

    def _release_managed_control(self) -> None:
        """Release policy ownership without changing native function permissions."""
        self.state.update(
            managed_heating=False,
            managed_hot_water=False,
            heating_preset="normal",
            hot_water_mode="auto",
            boost_enabled=False,
            boost_once=False,
            boost_previous_mode=None,
            boost_previous_managed=False,
            native_boost_coupled=False,
            hot_water_exit_mode=None,
        )
        self._clear_excess_time("heating")
        self._clear_excess_time("hot_water")
        self._clear_low_time()
        self._thermal_action = "idle"
        self._hydrate()

    async def set_control_mode(self, mode: str) -> None:
        mode = self._validate_setting("control_mode", mode)
        previous = self.settings["control_mode"]
        if mode == previous:
            return
        if not await self.recover():
            raise ValueError("Restore temporary settings before changing control mode")
        self._release_managed_control()
        self.settings["control_mode"] = mode
        self.state["settings"]["control_mode"] = mode
        try:
            await self._save()
        except BaseException:
            self.settings["control_mode"] = previous
            self.state["settings"]["control_mode"] = previous
            raise

    async def _ensure_external_handoff(self) -> None:
        if not self.external_control:
            return
        if (
            self.state["managed_heating"]
            or self.state["managed_hot_water"]
            or self.state["heating_preset"] != "normal"
            or self.state["hot_water_mode"] not in {"auto", "off"}
            or self.state["boost_enabled"]
            or self.state["boost_once"]
            or self.state["sg_owners"]
            or self.state["overrides"].keys() - {
                "cooling_humidity", "automation_temperature", "native_settings"
            }
        ):
            if not await self.recover():
                raise ValueError("External control is awaiting restoration of temporary settings")
            self._release_managed_control()
            await self._save()

    def _hydrate(self) -> None:
        """Keep native normal setpoints, rather than adopting charging values."""
        if (
            not self.state["managed_heating"]
            and "heating" not in self.state["overrides"]
            and "heating_guard" not in self.state["overrides"]
            and "heating_target_edit" not in self.state["overrides"]
            and "heating_profile" not in self.state["overrides"]
            and not (self._humidity_protection_enabled() and self.state["cooling_humidity_paused"])
        ):
            heating = self.read("heating_enabled")
            cooling = self.read("passive_cooling_enabled")
            if heating is not None and cooling is not None:
                self.state["heating_mode"] = (
                    "heat_cool"
                    if heating and cooling
                    else "heat"
                    if heating
                    else "cool"
                    if cooling
                    else "off"
                )
                if self.state["heating_mode"] != "off":
                    self.state["last_heating_mode"] = self.state["heating_mode"]
        if (
            not self.state["managed_hot_water"]
            and "hot_water" not in self.state["overrides"]
            and "water_guard" not in self.state["overrides"]
            and "water_temperature_edit" not in self.state["overrides"]
        ):
            enabled = self.read("hot_water_enabled")
            if enabled is not None:
                self.state["hot_water_mode"] = "auto" if enabled else "off"
        for state_key, read_key, default in (
            ("heating_target", "comfort_wheel", None),
            ("hot_water_target", "hot_water_stop", None),
            ("hot_water_start", "hot_water_start", None),
        ):
            if self.state.get(state_key) is not None:
                continue
            group = "hot_water" if state_key.startswith("hot_water") else "heating"
            source = self.state["overrides"].get(group, {})
            value = _number(source.get(read_key, self.read(read_key)))
            if state_key == "heating_target" and "heating_target_edit" in self.state["overrides"]:
                value = _number(self.state["overrides"]["heating_target_edit"]["policy"][state_key])
            if state_key == "heating_target" and (value is None or not 10 <= value <= 35):
                value = default
            self.state[state_key] = value

    def _native_heating_dial(self) -> float:
        native = self.read("comfort_wheel")
        if isinstance(native, bool) or not isinstance(native, (int, float)):
            raise ValueError("The native heating dial must have a valid direct temperature reading")
        return _bounded(native, 10, 40, "Native heating dial temperature")

    async def _begin_heating_target_edit(self, *, sync_target: bool = True) -> None:
        if "heating_target_edit" in self.state["overrides"]:
            return
        profile = self.state["overrides"].get("heating_profile", {})
        originals = self.state["overrides"].get("heating", {})
        native = self._native_heating_dial() if sync_target else None
        if sync_target:
            native = profile.get("comfort_wheel", originals.get("comfort_wheel", native))
        keys = (
            "heating_target",
            "heating_low",
            "heating_high",
            "heating_mode",
            "last_heating_mode",
            "heating_preset",
            "managed_heating",
        )
        policy = {key: deepcopy(self.state[key]) for key in keys}
        if profile:
            policy = deepcopy(profile["policy"])
        heating = profile.get(
            "heating_enabled", originals.get("heating_enabled", self.read("heating_enabled"))
        )
        cooling = profile.get(
            "passive_cooling_enabled",
            originals.get("passive_cooling_enabled", self.read("passive_cooling_enabled")),
        )
        guard = self.state["overrides"].get("heating_guard")
        if not originals and guard is not None:
            heating = guard["heating_enabled"]
        if heating not in (0, 1) or cooling not in (0, 1):
            raise ValueError("Both native space-heating function permissions must be available")
        cancelled_excess = policy["heating_preset"] == "pv_charge"
        if cancelled_excess:
            policy.update(
                heating_preset="normal",
                heating_mode=self.state["heating_excess_previous_mode"] or policy["heating_mode"],
                managed_heating=self.state["heating_excess_previous_managed"],
            )
        snapshot = {
            "heating_enabled": bool(heating),
            "passive_cooling_enabled": bool(cooling),
            "policy": policy,
            "cancelled_excess": cancelled_excess,
        }
        if sync_target:
            snapshot["comfort_wheel"] = native
        self.state["overrides"]["heating_target_edit"] = snapshot
        try:
            await self._save()
        except Exception:
            del self.state["overrides"]["heating_target_edit"]
            raise

    async def _commit_heating_target_edit(self) -> None:
        snapshot = self.state["overrides"].get("heating_target_edit")
        if snapshot is None:
            return
        # Save the new policy while the recovery journal is still present.
        await self._save()
        del self.state["overrides"]["heating_target_edit"]
        try:
            await self._save()
        except BaseException:
            self.state["overrides"]["heating_target_edit"] = snapshot
            raise

    def _temperature_edit_payload(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or not value:
            raise ValueError("Provide thermostat temperature fields")
        if set(value) - {"temperature", "target_temp_low", "target_temp_high", "hvac_mode"}:
            raise ValueError("Unsupported thermostat temperature field")
        if not any(key in value for key in ("temperature", "target_temp_low", "target_temp_high")):
            raise ValueError("Provide a thermostat temperature to edit")
        if "temperature" in value and any(
            key in value for key in ("target_temp_low", "target_temp_high")
        ):
            raise ValueError("Provide either a single temperature or a temperature range")
        return value

    def effective_heating_target(
        self,
        mode: str | None = None,
        preset: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> float | None:
        """Return the displayed active target without replacing Normal values."""
        mode = mode or self.state["heating_mode"]
        preset = preset or self.state["heating_preset"]
        settings = self.settings if settings is None else {**self.settings, **settings}
        if preset == "pv_charge" and mode != "heat_cool":
            return _number(settings.get("max_heating_temperature"))
        normal = _number(
            self.state["heating_low"] if mode == "heat_cool" else self.state["heating_target"]
        )
        if normal is None:
            return None
        if preset == "low" and mode in {"heat", "heat_cool"}:
            offset = self._validate_setting("heating_low_offset", settings["heating_low_offset"])
            return max(10.0, normal - offset)
        if preset == "vacation" and mode in {"heat", "heat_cool"}:
            return self._validate_setting(
                "heating_vacation_temperature", settings["heating_vacation_temperature"]
            )
        if preset == "vacation" and mode == "cool":
            offset = self._validate_setting(
                "heating_vacation_cooling_offset", settings["heating_vacation_cooling_offset"]
            )
            return max(10.0, normal - offset)
        return normal

    def effective_heating_range(
        self,
        mode: str | None = None,
        preset: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> tuple[float, float]:
        low = self.effective_heating_target("heat_cool", preset, settings)
        high = self.state["heating_high"]
        if (preset or self.state["heating_preset"]) == "vacation":
            proposed = self.settings if settings is None else {**self.settings, **settings}
            offset = self._validate_setting(
                "heating_vacation_cooling_offset", proposed["heating_vacation_cooling_offset"]
            )
            high = max(10.0, high - offset)
        if low is None or not low < high:
            raise ValueError(
                "The effective heating target must be below the effective cooling target"
            )
        return low, high

    async def _apply_heating_profile(self, preset: str, *, mode: str | None = None) -> None:
        mode = mode or self.state["heating_mode"]
        if preset == "low" and mode == "cool":
            raise ValueError("Low Mode reduces heating only; select Normal or Vacation in Cool")
        target = self.effective_heating_target(mode, preset)
        if mode == "heat_cool":
            self.effective_heating_range(mode, preset)
        if mode in {"cool", "heat_cool"} and self._inside is None:
            raise ValueError("Cooling presets require a measured inside temperature")
        if mode in {"heat", "heat_cool"}:
            _bounded(target, 10, 35, "Native heating target")
            self._native_heating_dial()
        try:
            snapshot = self.state["overrides"].get("heating_profile")
            if snapshot is None:
                await self._begin_heating_target_edit(sync_target=mode in {"heat", "heat_cool"})
                snapshot = deepcopy(self.state["overrides"]["heating_target_edit"])
                self.state["overrides"]["heating_profile"] = snapshot
                await self._save()
            elif mode in {"heat", "heat_cool"} and "comfort_wheel" not in snapshot:
                snapshot["comfort_wheel"] = self._native_heating_dial()
                await self._save()
            self.state.update(heating_mode=mode, heating_preset=preset, managed_heating=True)
            self._clear_excess_time("heating")
            await self._save()
            await self._stop_charge("heating")
            await self._normal_heating(mode)
            await self._protect_sg_channels()
            await self._save()
            await self._commit_heating_target_edit()
        except Exception:
            try:
                await self._stop_charge("heating")
                await self._restore_group("heating_profile")
            except Exception:
                pass
            raise

    async def _exit_heating_profile(self, *, mode: str | None = None, managed: bool = True) -> None:
        mode = mode or self.state["heating_mode"]
        await self._restore_group("heating_profile")
        self.state.update(heating_mode=mode, heating_preset="normal", managed_heating=managed)
        if managed:
            await self._normal_heating(mode)
        await self._save()

    def _humidity_protection_enabled(self) -> bool:
        return bool(self.settings.get("inside_humidity_sensor")) and (
            self.state["heating_preset"] == "vacation"
            or bool(self.settings.get("cooling_humidity_enabled"))
            or (self._restoring and "cooling_humidity" in self.state["overrides"])
        )

    def cooling_humidity_info(self, *, inside: Any = ..., humidity: Any = ...) -> dict[str, Any]:
        vacation = self.state["heating_preset"] == "vacation"
        normal_limit = _number(self.settings["cooling_humidity_limit"])
        vacation_limit = _number(self.settings["cooling_vacation_humidity_limit"])
        active_limit = vacation_limit if vacation else normal_limit
        info = cooling_guard_info(
            self._inside if inside is ... else _number(inside),
            self._humidity if humidity is ... else valid_humidity(humidity),
            active_limit,
            self.settings["cooling_dew_point_margin"],
            configured=bool(self.settings.get("inside_humidity_sensor")),
        )
        enabled = self._humidity_protection_enabled()
        if info["configured"] and not enabled:
            info.update(allowed=True, status="disabled_in_normal")
        if (
            enabled
            and info["allowed"]
            and self.state["cooling_humidity_high"]
            and info["relative_humidity"] > info["humidity_limit"] - 2
        ):
            info.update(allowed=False, status="high_humidity")
        native = self.read("passive_cooling_supply_target")
        spec = NATIVE_SETTINGS["passive_cooling_supply_target"]
        if enabled and info["allowed"]:
            try:
                validate_native_setting("passive_cooling_supply_target", native, require_step=False)
            except ValueError:
                info.update(allowed=False, status="cooling_supply_unavailable")
            if (
                info["min_cooling_supply"] is not None
                and info["min_cooling_supply"] > spec.max_value
            ):
                info.update(allowed=False, status="dew_point_above_supply_limit")
        return {
            **info,
            "paused": self.state["cooling_humidity_paused"],
            "requested": self.state["cooling_humidity_requested"],
            "native_supply_target": _number(native),
            "max_cooling_supply": spec.max_value,
            "restore_pending": "cooling_humidity" in self.state["pending_restore"]
            or self.state["cooling_humidity_resume_pending"],
            "protection_enabled": enabled,
            "enabled_in_normal": bool(self.settings.get("cooling_humidity_enabled")),
            "vacation_guard": vacation,
            "active_humidity_profile": "vacation" if vacation else "normal",
            "active_humidity_limit": info["humidity_limit"],
            "normal_humidity_limit": normal_limit,
            "vacation_humidity_limit": vacation_limit,
            "normal_resume_humidity_limit": (
                normal_limit - 2 if normal_limit is not None else None
            ),
            "vacation_resume_humidity_limit": (
                vacation_limit - 2 if vacation_limit is not None else None
            ),
            "resume_humidity_limit": (
                info["humidity_limit"] - 2 if info["humidity_limit"] is not None else None
            ),
        }

    async def _cooling_permission(self, requested: bool, *, allow_start: bool = True) -> bool:
        configured = self._humidity_protection_enabled()
        if not requested or not configured:
            if not requested:
                self.state["cooling_humidity_resume_pending"] = False
            snapshot = self.state["overrides"].get("cooling_humidity")
            if snapshot is not None:
                snapshot["requested"] = requested
                await self._save()
                await self._restore_group("cooling_humidity")
            self.state["cooling_humidity_requested"] = False
            self.state["cooling_humidity_paused"] = False
            self.state["cooling_humidity_high"] = False
            return requested
        self.state["cooling_humidity_requested"] = True
        info = self.cooling_humidity_info()
        if self._humidity is not None and info["humidity_limit"] is not None:
            limit = info["humidity_limit"]
            if self._humidity >= limit:
                self.state["cooling_humidity_high"] = True
            elif self._humidity <= limit - 2:
                self.state["cooling_humidity_high"] = False
                info = self.cooling_humidity_info()
        snapshot = self.state["overrides"].get("cooling_humidity")
        native = self.read("passive_cooling_supply_target")
        if not info["allowed"]:
            if snapshot is None and _number(native) is not None:
                try:
                    original = validate_native_setting(
                        "passive_cooling_supply_target", native, require_step=False
                    )
                    permission = self.read("passive_cooling_enabled")
                    if permission not in (0, 1):
                        raise ValueError("The native cooling permission is unavailable")
                    self.state["overrides"]["cooling_humidity"] = {
                        "passive_cooling_supply_target": original,
                        "passive_cooling_enabled": bool(permission),
                        "requested": True,
                    }
                except ValueError:
                    pass
            self.state["cooling_humidity_paused"] = True
            self.state["control_warning"] = f"Cooling paused: {info['status'].replace('_', ' ')}"
            await self._save()
            await self.write("passive_cooling_enabled", False)
            return False
        base = snapshot["passive_cooling_supply_target"] if snapshot else float(native)
        required = max(base, float(info["min_cooling_supply"]))
        if not allow_start or (self._restoring and float(native) < required):
            self.state["cooling_humidity_paused"] = True
            await self.write("passive_cooling_enabled", False)
            await self._save()
            return False
        if snapshot is None and (
            required != float(native) or self.state["cooling_humidity_paused"]
        ):
            permission = self.read("passive_cooling_enabled")
            if permission not in (0, 1):
                raise ValueError("The native cooling permission is unavailable")
            self.state["overrides"]["cooling_humidity"] = {
                "passive_cooling_supply_target": float(native),
                "passive_cooling_enabled": bool(permission),
                "requested": True,
            }
            await self._save()
        if required != float(native):
            # Stop before either raising a newly required floor or lowering an
            # old temporary floor. Every supply write has a durable original.
            await self.write("passive_cooling_enabled", False)
            await self._write("passive_cooling_supply_target", required)
        self.state["cooling_humidity_paused"] = False
        if (self.state.get("control_warning") or "").startswith("Cooling paused:"):
            self.state["control_warning"] = None
        return True

    async def _sync_cooling_humidity(self, *, allow_start: bool = True) -> None:
        native_on = bool(self.read("passive_cooling_enabled"))
        owned_pause = (
            self.state["cooling_humidity_paused"] or self.state["cooling_humidity_resume_pending"]
        )
        requested = (
            native_on
            or (owned_pause and self.state["cooling_humidity_requested"])
            or self.state["cooling_humidity_resume_pending"]
        )
        if not self._humidity_protection_enabled():
            releasing = (
                "cooling_humidity" in self.state["overrides"]
                or self.state["cooling_humidity_resume_pending"]
            )
            if "cooling_humidity" in self.state["overrides"]:
                await self._cooling_permission(requested, allow_start=allow_start)
            if requested and allow_start and releasing:
                await self._write("passive_cooling_enabled", True)
            return
        if not requested and not self.state["managed_heating"]:
            await self._cooling_permission(False)
            return
        if requested and (not self.state["managed_heating"] or not allow_start):
            allowed = await self._cooling_permission(True, allow_start=allow_start)
            if allowed and allow_start:
                await self._write("passive_cooling_enabled", True)

    def _heating_temperature_edit_plan(self, value: Any) -> tuple[str, dict[str, float]] | None:
        payload = self._temperature_edit_payload(value)
        excess = self.state["heating_preset"] == "pv_charge"
        mode = payload.get("hvac_mode") or self.state["heating_mode"]
        if mode not in {"off", "heat", "cool", "heat_cool"}:
            raise ValueError("Unsupported heating mode")
        displayed = {}
        if excess:
            displayed = {
                key: _number(self.settings.get("max_heating_temperature"))
                for key in ("temperature", "target_temp_low", "target_temp_high")
            }
        elif "temperature" in payload:
            displayed["temperature"] = self.effective_heating_target()
        else:
            # A scalar Cool/Heat edit must not validate a hypothetical Auto
            # range whose Vacation endpoints may overlap.
            if "target_temp_low" in payload:
                displayed["target_temp_low"] = self.effective_heating_target("heat_cool")
            if "target_temp_high" in payload:
                high = self.state["heating_high"]
                if self.state["heating_preset"] == "vacation":
                    high = max(10.0, high - self.settings["heating_vacation_cooling_offset"])
                displayed["target_temp_high"] = high
        if (
            (not excess or payload.get("hvac_mode") is None)
            and mode == self.state["heating_mode"]
            and all(
                proposed == displayed[key]
                for key, proposed in payload.items()
                if key != "hvac_mode"
            )
        ):
            return None
        updates = {}
        if "temperature" in payload:
            target = _whole_temperature(payload["temperature"], 10, 35, "Heating target")
            updates["heating_low" if mode == "heat_cool" else "heating_target"] = target
        else:
            for field, key, label in (
                ("target_temp_low", "heating_low", "Heating lower target"),
                ("target_temp_high", "heating_high", "Cooling upper target"),
            ):
                if field in payload:
                    if (
                        self.state["heating_preset"] != "normal"
                        and payload[field] == displayed[field]
                    ):
                        continue
                    updates[key] = _whole_temperature(payload[field], 10, 35, label)
        low = updates.get("heating_low", self.state["heating_low"])
        high = updates.get("heating_high", self.state["heating_high"])
        if (mode == "heat_cool" or "heating_low" in updates or "heating_high" in updates) and (
            low >= high
        ):
            raise ValueError("The heating target must be below the cooling target")
        if mode == "heat_cool" and self._inside is None:
            raise ValueError("Automatic heat/cool requires a measured inside temperature")
        if mode == "heat":
            _bounded(
                updates.get("heating_target", self.state["heating_target"]),
                10,
                35,
                "Heating target",
            )
        if mode in {"heat", "heat_cool"}:
            self._native_heating_dial()
        return mode, updates

    async def _edit_heating_temperature(self, value: Any) -> None:
        plan = self._heating_temperature_edit_plan(value)
        if plan is None:
            return
        mode, updates = plan
        await self._begin_heating_target_edit(sync_target=mode in {"heat", "heat_cool"})
        self.state.update(updates)
        self.state.update(heating_mode=mode, heating_preset="normal", managed_heating=True)
        self._clear_excess_time("heating")
        # A restart must finish cancelling the override, even if restoring the
        # fixed supply or writing the new target is interrupted.
        await self._save()
        await self._stop_charge("heating")
        await self._restore_group("heating_profile")
        self.state.update(updates)
        self.state.update(heating_mode=mode, heating_preset="normal", managed_heating=True)
        await self._normal_heating(mode)
        if mode != "off":
            self.state["last_heating_mode"] = mode
        guard = self.state["overrides"].get("heating_guard")
        if guard is not None:
            guard["heating_enabled"] = bool(self.read("heating_enabled"))
        await self._protect_sg_channels()
        await self._commit_heating_target_edit()

    def _hot_water_temperature_edit_plan(self, value: Any) -> tuple[float, float] | None:
        payload = self._temperature_edit_payload(value)
        if payload.get("hvac_mode") not in {None, "off", "auto"}:
            raise ValueError("Unsupported hot-water mode")
        snapshot = self.state["overrides"].get("hot_water", {})
        normal_start = snapshot.get("hot_water_start", self.read("hot_water_start"))
        normal_stop = snapshot.get("hot_water_stop", self.read("hot_water_stop"))
        requested = {
            key: payload[field]
            for field, key in (("target_temp_low", "start"), ("target_temp_high", "stop"))
            if field in payload
        }
        if "temperature" in payload:
            requested["stop"] = payload["temperature"]
        start_maximum = _number(self.settings.get("max_start_temperature"))
        # Frontends send both displayed handles even when only one moved.
        # The untouched side comes from the saved Normal profile.
        requested = {
            key: proposed
            for key, proposed in requested.items()
            if proposed != self.read("hot_water_start" if key == "start" else "hot_water_stop")
            or (
                key == "start"
                and start_maximum is not None
                and (numeric := _number(proposed)) is not None
                and numeric > min(60, math.floor(start_maximum))
            )
        }
        if not requested and (
            payload.get("hvac_mode") is None
            or (
                self.state["hot_water_mode"] not in {"manual_on", "energy_excess", "evening"}
                and payload["hvac_mode"]
                == ("off" if self.state["hot_water_mode"] == "off" else "auto")
            )
        ):
            return None
        stop_maximum = _number(self.settings.get("max_hot_water_temperature"))
        if start_maximum is None or stop_maximum is None:
            raise ValueError("Confirm both hot-water pump limits in integration options")
        self._validate_setting("max_start_temperature", start_maximum)
        self._validate_setting("max_hot_water_temperature", stop_maximum)
        start = _bounded(requested.get("start", normal_start), 30, 60, "Normal hot-water start")
        stop = _bounded(requested.get("stop", normal_stop), 30, 60, "Normal hot-water stop")
        if "start" in requested:
            _whole_temperature(start, 30, 60, "Normal hot-water start")
            # A valid edited START settles at the configured controller cap.
            # Keep its raw edit intent even if it equals the displayed preset
            # value after clamping, so the edit still returns to Normal.
            start = min(start, 60, math.floor(start_maximum))
        if "stop" in requested:
            _whole_temperature(stop, 30, 60, "Normal hot-water stop")
        if start >= stop:
            # Preserve the deliberately moved endpoint when a restored
            # Normal counterpart would collide. Both moved ends anchor STOP.
            start_upper = min(60, math.floor(start_maximum))
            stop_upper = min(60, math.floor(stop_maximum))
            upper = min(stop_upper, start_upper + 5)
            if upper < 35:
                raise ValueError(
                    "No Normal hot-water pair with a 5°C gap fits the configured pump limits"
                )
            if set(requested) == {"start"}:
                start = max(30, min(start, start_upper, stop_upper - 5))
                stop = start + 5
            else:
                stop = max(35, min(stop, upper))
                start = stop - 5
        _bounded(start, 30, min(60, start_maximum), "Normal hot-water start")
        _bounded(stop, 30, min(60, stop_maximum), "Normal hot-water stop")
        return start, stop

    def _normal_water_permission(self, stop: float, *, enabled: bool = True) -> bool:
        if not enabled:
            return False
        if self.state["sg_owners"] or (
            self.settings["smart_grid_mode"] == "sg_ready" and self.read("smart_grid_request") == 3
        ):
            temperature = self._water_temperature()
            return temperature is not None and temperature < stop
        return True

    async def _edit_hot_water_temperature(self, value: Any) -> None:
        pair = self._hot_water_temperature_edit_plan(value)
        if pair is None:
            return
        start, stop = pair
        cancelled_preset = (
            self.state["hot_water_mode"] in {"manual_on", "energy_excess", "evening"}
            or self.state["boost_enabled"]
            or self.state["boost_once"]
        )
        original = self.state["overrides"].get("hot_water", {})
        native_start = original.get("hot_water_start", self.read("hot_water_start"))
        native_stop = original.get("hot_water_stop", self.read("hot_water_stop"))
        # Retain exact native originals, including their wire precision.
        if (
            _number(native_start) is None
            or _number(native_stop) is None
            or native_start >= native_stop
        ):
            raise ValueError("The original Normal hot-water temperature range is unavailable")
        mode = value.get("hvac_mode") or (
            "off" if self.state["hot_water_mode"] == "off" else "auto"
        )
        native_permission = self.read("hot_water_enabled")
        if native_permission not in (0, 1):
            raise ValueError("The native hot-water permission is unavailable")
        previous_guard = self.state["overrides"].get("water_guard")
        desired_permission = (
            mode != "off"
            if cancelled_preset or value.get("hvac_mode") is not None
            else bool(
                previous_guard["hot_water_enabled"]
                if previous_guard is not None
                else native_permission
            )
        )
        snapshot = {
            "hot_water_start": native_start,
            "hot_water_stop": native_stop,
            "hot_water_enabled": mode != "off" if cancelled_preset else bool(native_permission),
            "policy": {
                "hot_water_start": native_start,
                "hot_water_target": native_stop,
                "hot_water_mode": mode,
                "managed_hot_water": True,
                "boost_enabled": False,
                "boost_once": False,
                "boost_previous_mode": None,
                "boost_previous_managed": False,
                "native_boost_coupled": False,
                "hot_water_exit_mode": None,
            },
        }
        if not cancelled_preset:
            snapshot["policy"] = {key: deepcopy(self.state[key]) for key in snapshot["policy"]}
        if previous_guard is not None:
            snapshot["water_guard_permission"] = previous_guard["hot_water_enabled"]
        if "hot_water_boost" in original or self._native_boost_available():
            snapshot["hot_water_boost"] = (
                False if cancelled_preset else self.read("hot_water_boost")
            )
        self.state["overrides"]["water_temperature_edit"] = snapshot
        try:
            await self._save()
        except Exception:
            del self.state["overrides"]["water_temperature_edit"]
            raise
        self.state.update(deepcopy(snapshot["policy"]))
        self.state.update(
            hot_water_start=start,
            hot_water_target=stop,
            hot_water_mode=mode,
            managed_hot_water=True,
            hot_water_exit_mode=mode,
        )
        self._clear_excess_time("hot_water")
        self._clear_low_time()
        if any(owner != "hot_water" for owner in self.state["sg_owners"]):
            # Capture the desired permission before our temporary safety pause,
            # so the remaining owner's eventual release can restore it.
            await self._snapshot("water_guard", ["hot_water_enabled"])
        guard = self.state["overrides"].get("water_guard")
        if guard is not None:
            guard["hot_water_enabled"] = desired_permission
        if original:
            original["hot_water_enabled"] = self._normal_water_permission(
                stop, enabled=desired_permission
            )
            if "hot_water_boost" in snapshot:
                original["hot_water_boost"] = False
        await self._save()
        if not self._normal_water_permission(stop, enabled=desired_permission):
            # Remove demand before returning lower native thresholds while a
            # shared SG request is still active, including our exiting owner.
            await self._write("hot_water_enabled", False)
        await self._stop_charge("hot_water")
        if "hot_water_boost" in snapshot:
            await self._write("hot_water_boost", snapshot["hot_water_boost"])
        await self._water_thresholds(start, stop)
        await self._write(
            "hot_water_enabled", self._normal_water_permission(stop, enabled=desired_permission)
        )
        self.state["hot_water_exit_mode"] = None
        await self._protect_sg_channels()
        await self._save()
        del self.state["overrides"]["water_temperature_edit"]
        try:
            await self._save()
        except BaseException:
            self.state["overrides"]["water_temperature_edit"] = snapshot
            raise

    async def _prepare_heating_target_command(self, action: str, value: Any) -> None:
        if (
            self.state["heating_preset"] != "normal"
            and action != "heating_mode"
            and not (action == "heating_preset" and value == "normal")
        ):
            return
        mode = self.state["heating_mode"]
        if action == "heating_mode":
            mode = value
        elif action == "heating_preset" and value == "normal":
            mode = self.state["heating_excess_previous_mode"] or mode
        elif action not in {"heating_target", "heating_range"}:
            return
        if mode not in {"heat", "heat_cool"}:
            return
        if mode == "heat_cool" and self._inside is None:
            raise ValueError("Automatic heat/cool requires a measured inside temperature")
        if action == "heating_target":
            _whole_temperature(value, 10, 35, "Heating target")
        elif action == "heating_range":
            low, high = value
            low = _bounded(low, 10, 35, "Heating lower target")
            high = _bounded(high, 10, 35, "Cooling upper target")
            if low != self.state["heating_low"]:
                _whole_temperature(low, 10, 35, "Heating lower target")
            if high != self.state["heating_high"]:
                _whole_temperature(high, 10, 35, "Cooling upper target")
            if low >= high:
                raise ValueError("The heating target must be below the cooling target")
        elif mode == "heat":
            _bounded(self.state["heating_target"], 10, 35, "Heating target")
        else:
            _bounded(self.state["heating_low"], 10, 35, "Heating lower target")
        await self._begin_heating_target_edit()

    def _validate_setting(self, name: str, value: Any) -> Any:
        if name == "control_mode":
            if isinstance(value, str) and value in {"internal", "external"}:
                return value
            raise ValueError("Control mode must be internal or external")
        bounds = {
            "max_hot_water_temperature": (5, 90),
            "max_start_temperature": (5, 90),
            "max_heating_temperature": (10, 35),
            "hysteresis": (0.1, 5),
            "hot_water_hysteresis": (1, 30),
            "hot_water_boost_start": (30, 60),
            "hot_water_boost_stop": (30, 60),
            "heating_excess_hours": (0.1, 168),
            "heating_excess_heat_stop_offset": (0, 25),
            "hot_water_excess_hours": (0.1, 168),
            "hot_water_evening_start": (30, 60),
            "hot_water_evening_stop": (30, 60),
            "hot_water_low_hours": (0.1, 168),
            "heating_low_offset": (0, 25),
            "heating_vacation_temperature": (10, 35),
            "heating_vacation_cooling_offset": (0, 25),
            "cooling_humidity_limit": (30, 90),
            "cooling_vacation_humidity_limit": (30, 90),
            "cooling_dew_point_margin": (1, 5),
        }
        if name in {"hot_water_boost_start", "hot_water_boost_stop"} and value is None:
            return None
        if name in bounds:
            if name in {"cooling_humidity_limit", "cooling_vacation_humidity_limit"}:
                result = _bounded(value, *bounds[name], name)
                if not result.is_integer():
                    raise ValueError(f"{name} must use whole percentage points")
                return result
            if name in {
                "heating_low_offset",
                "heating_excess_heat_stop_offset",
                "heating_vacation_temperature",
                "heating_vacation_cooling_offset",
                "cooling_dew_point_margin",
            }:
                return _whole_temperature(value, *bounds[name], name)
            return _bounded(value, *bounds[name], name)
        if name == "inside_humidity_sensor" and isinstance(value, str):
            return value
        if name == "smart_grid_mode" and value in {"disabled", "sg_ready", "power_limit"}:
            return value
        if name in {"enable_undocumented_controls", "cooling_humidity_enabled"} and isinstance(
            value, bool
        ):
            return value
        raise ValueError(f"Unsupported setting or invalid value: {name}")

    def _validate_new_duration(self, name: str, value: Any) -> float:
        """Whole-hour user edits, without altering legacy stored durations."""
        if name not in {"heating_excess_hours", "hot_water_excess_hours", "hot_water_low_hours"}:
            raise ValueError(f"Unsupported duration setting: {name}")
        result = _bounded(value, 1, 168, name)
        if not result.is_integer():
            raise ValueError(f"{name} must use whole-hour steps")
        return result

    def _clock(self) -> float:
        now = _number(self.now())
        if now is None or now < 0:
            raise ValueError("A valid UTC clock is required for timed presets")
        return now

    def _clear_excess_time(self, group: str) -> None:
        self.state[f"{group}_excess_started_at"] = None
        self.state[f"{group}_excess_deadline"] = None
        if group == "heating":
            self.state["heating_excess_previous_mode"] = None
            self.state["heating_excess_previous_managed"] = False

    def _clear_low_time(self) -> None:
        self.state["hot_water_low_started_at"] = None
        self.state["hot_water_low_deadline"] = None

    async def _start_low_time(self) -> None:
        previous = {
            key: self.state[key] for key in ("hot_water_low_started_at", "hot_water_low_deadline")
        }
        started = self._clock()
        hours = self._validate_setting("hot_water_low_hours", self.settings["hot_water_low_hours"])
        self.state.update(
            hot_water_low_started_at=started, hot_water_low_deadline=started + hours * 3600
        )
        try:
            await self._save()
        except Exception:
            self.state.update(previous)
            raise

    async def _start_excess_time(self, group: str) -> bool:
        fresh = self.state[f"{group}_excess_started_at"] is None
        keys = [f"{group}_excess_started_at", f"{group}_excess_deadline"]
        if group == "heating":
            keys.extend(("heating_excess_previous_mode", "heating_excess_previous_managed"))
        previous = {key: self.state[key] for key in keys}
        hours = self._validate_setting(
            f"{group}_excess_hours", self.settings[f"{group}_excess_hours"]
        )
        started = self._clock()
        self.state[f"{group}_excess_started_at"] = started
        self.state[f"{group}_excess_deadline"] = started + hours * 3600
        if group == "heating" and fresh:
            self.state["heating_excess_previous_mode"] = self.state["heating_mode"]
            self.state["heating_excess_previous_managed"] = self.state["managed_heating"]
        try:
            await self._save()
        except Exception:
            self.state.update(previous)
            raise
        return fresh

    async def _refresh_excess_deadlines(self) -> None:
        changed = False
        for group in ("heating", "hot_water"):
            started = _number(self.state[f"{group}_excess_started_at"])
            if started is None:
                continue
            hours = self._validate_setting(
                f"{group}_excess_hours", self.settings[f"{group}_excess_hours"]
            )
            deadline = started + hours * 3600
            if deadline != self.state[f"{group}_excess_deadline"]:
                self.state[f"{group}_excess_deadline"] = deadline
                changed = True
        started = _number(self.state["hot_water_low_started_at"])
        if started is not None:
            hours = self._validate_setting(
                "hot_water_low_hours", self.settings["hot_water_low_hours"]
            )
            deadline = started + hours * 3600
            if deadline != self.state["hot_water_low_deadline"]:
                self.state["hot_water_low_deadline"] = deadline
                changed = True
        if changed:
            await self._save()

    async def _finish_heating_excess(self, *, managed: bool | None = None) -> None:
        previous = self.state["heating_excess_previous_mode"] or self.state["heating_mode"]
        previous_managed = self.state["heating_excess_previous_managed"]
        if managed is not None:
            previous_managed = managed
        await self._stop_charge("heating")
        self.state["heating_preset"] = "normal"
        if previous_managed:
            await self._normal_heating(previous)
        self.state.update(
            heating_mode=previous, heating_preset="normal", managed_heating=previous_managed
        )
        self._clear_excess_time("heating")
        await self._save()

    async def _finish_water_excess(self) -> None:
        await self._exit_energy_excess("auto")
        self.state.update(
            hot_water_mode="auto",
            managed_hot_water=True,
            boost_enabled=False,
            boost_once=False,
            boost_previous_mode=None,
            boost_previous_managed=False,
            hot_water_exit_mode=None,
        )
        self._clear_excess_time("hot_water")
        await self._save()

    async def _expire_excess(self) -> None:
        await self._refresh_excess_deadlines()
        if (
            all(
                self.state[f"{group}_excess_deadline"] is None for group in ("heating", "hot_water")
            )
            and self.state["hot_water_low_deadline"] is None
        ):
            return
        now = self._clock()
        failure: Exception | None = None
        for group in ("heating", "hot_water"):
            deadline = _number(self.state[f"{group}_excess_deadline"])
            if deadline is None or now < deadline:
                continue
            try:
                if group == "heating":
                    await self._finish_heating_excess()
                elif self.state["hot_water_mode"] == "energy_excess":
                    await self._finish_water_excess()
                else:
                    self._clear_excess_time("hot_water")
                    await self._save()
            except Exception as err:
                failure = failure or err
        low_deadline = _number(self.state["hot_water_low_deadline"])
        if low_deadline is not None and now >= low_deadline:
            try:
                if self.state["hot_water_mode"] == "evening":
                    await self._exit_evening_water("auto")
                else:
                    self._clear_low_time()
                    await self._save()
            except Exception as err:
                failure = failure or err
        if failure is not None:
            raise failure

    def _water_limits(
        self, *, once: bool = False, settings: dict[str, Any] | None = None
    ) -> tuple[float, float, float]:
        proposed = self.settings if settings is None else {**self.settings, **settings}
        maximum = _number(proposed.get("max_hot_water_temperature"))
        start_maximum = _number(proposed.get("max_start_temperature"))
        if maximum is None or start_maximum is None:
            raise ValueError(
                "Confirm the pump's maximum hot-water stop and start temperatures "
                "in the integration options before charging"
            )
        self._validate_setting("max_hot_water_temperature", maximum)
        self._validate_setting("max_start_temperature", start_maximum)
        margin = self._validate_setting("hot_water_hysteresis", proposed["hot_water_hysteresis"])
        if once:
            start = min(60.0 - margin, start_maximum)
            if maximum < 60:
                raise ValueError(
                    "Hot-water boost requires a configured stop limit of at least 60°C"
                )
            _bounded(start, 30, 59, "Hot-water Excess Energy start")
            return 60.0, start, 60.0 - start
        configured_start = proposed.get("hot_water_boost_start")
        configured_stop = proposed.get("hot_water_boost_stop")
        if configured_start is not None or configured_stop is not None:
            if configured_start is None or configured_stop is None:
                raise ValueError("Set both hot-water boost start and stop temperatures")
            start = _bounded(configured_start, 30, 60, "Hot-water boost start")
            stop = _bounded(configured_stop, 30, 60, "Hot-water boost stop")
            if start > start_maximum or stop > maximum:
                raise ValueError("Hot-water boost temperatures exceed the verified pump limits")
            maximum = stop
        else:
            start = min(maximum - margin, start_maximum)
        if start <= 0 or start >= maximum:
            raise ValueError("Hot-water charging start must be below its stop temperature")
        return maximum, start, margin

    def hot_water_excess_info(self) -> dict[str, Any]:
        """Report configured and effective limits without claiming hardware support."""
        requested_gap = _number(self.settings.get("hot_water_hysteresis"))
        info = {
            "configured_restart_gap": requested_gap,
            "requested_start_temperature": 60.0 - requested_gap
            if requested_gap is not None
            else None,
            "effective_start_temperature": None,
            "effective_stop_temperature": None,
            "effective_restart_gap": None,
            "available": False,
            "reason": None,
        }
        try:
            stop, start, gap = self._water_limits(once=True)
        except ValueError as error:
            info["reason"] = str(error)
            return info
        info.update(
            effective_start_temperature=start,
            effective_stop_temperature=stop,
            effective_restart_gap=gap,
            available=True,
        )
        return info

    def _water_temperature(self) -> float | None:
        values = [
            value
            for key in ("hot_water_top_temperature", "hot_water_weighted_temperature")
            if (value := _number(self.read(key))) is not None
        ]
        return max(values) if values else None

    def _evening_water_limits(
        self, settings: dict[str, Any] | None = None, *, require_caps: bool = True
    ) -> tuple[float, float]:
        """Validate a Low Mode profile without changing settings or hardware."""
        proposed = self.settings if settings is None else {**self.settings, **settings}
        start = self._validate_setting(
            "hot_water_evening_start", proposed["hot_water_evening_start"]
        )
        stop = self._validate_setting("hot_water_evening_stop", proposed["hot_water_evening_stop"])
        if start >= stop:
            raise ValueError("Low Mode hot-water start must be below its stop temperature")
        if require_caps:
            start_maximum = _number(proposed.get("max_start_temperature"))
            stop_maximum = _number(proposed.get("max_hot_water_temperature"))
            if start_maximum is None or stop_maximum is None:
                raise ValueError("Confirm both hot-water pump limits before selecting Low Mode")
            self._validate_setting("max_start_temperature", start_maximum)
            self._validate_setting("max_hot_water_temperature", stop_maximum)
            if start > start_maximum or stop > stop_maximum:
                raise ValueError("Low Mode hot-water temperatures exceed the verified pump limits")
        return start, stop

    async def _apply_evening_water(self) -> None:
        """Apply native auto thresholds while retaining the normal recovery pair."""
        start, stop = self._evening_water_limits()
        keys = ["hot_water_enabled", "hot_water_start", "hot_water_stop"]
        native_boost = self._native_boost_available()
        if native_boost:
            keys.append("hot_water_boost")
        capture_permission = "hot_water_enabled" not in self.state["overrides"].get("hot_water", {})
        await self._snapshot("hot_water", keys)
        snapshot = self.state["overrides"]["hot_water"]
        guard = self.state["overrides"].get("water_guard")
        if capture_permission and guard is not None:
            # A global heating boost may have temporarily paused normal water.
            # Preserve the guard's native original rather than that pause.
            snapshot["hot_water_enabled"] = guard["hot_water_enabled"]
        self.state.update(
            hot_water_start=snapshot["hot_water_start"],
            hot_water_target=snapshot["hot_water_stop"],
        )
        if native_boost:
            # Evening explicitly selects regular controller operation. This
            # durable Off intent also prevents later recovery from boosting.
            snapshot["hot_water_boost"] = False
        await self._save()
        try:
            if native_boost:
                await self._write("hot_water_boost", False)
            await self._water_thresholds(start, stop)
            allowed = True
            if self.state["sg_owners"] and "hot_water" not in self.state["sg_owners"]:
                await self._snapshot("water_guard", ["hot_water_enabled"])
                self.state["overrides"]["water_guard"]["hot_water_enabled"] = True
                await self._save()
                temperature = self._water_temperature()
                allowed = temperature is not None and temperature < stop
            await self._write("hot_water_enabled", allowed)
            self.state["charge_active"]["hot_water"] = False
            self.state["native_boost_coupled"] = False
        except Exception as err:
            guard = self.state["overrides"].get("water_guard")
            if guard is not None:
                guard["hot_water_enabled"] = snapshot["hot_water_enabled"]
                await self._save()
            await self._abort_charge("hot_water", err)
            if not self.state["pending_restore"]:
                self.state.update(
                    hot_water_mode="auto" if snapshot["hot_water_enabled"] else "off",
                    managed_hot_water=False,
                )
                self._hydrate()
            raise

    async def _exit_evening_water(self, mode: str = "auto") -> None:
        self.state["hot_water_exit_mode"] = mode
        snapshot = self.state["overrides"].get("hot_water")
        if snapshot is not None:
            snapshot["hot_water_enabled"] = mode != "off"
        guard = self.state["overrides"].get("water_guard")
        if guard is not None:
            # All journals must retain this explicit request, regardless of
            # their eventual retry or shutdown restoration order.
            guard["hot_water_enabled"] = mode != "off"
        await self._save()
        await self._stop_charge("hot_water")
        if snapshot is not None:
            await self._write("hot_water_enabled", mode != "off")
        else:
            await self._normal_water(mode)
        self.state.update(
            hot_water_mode=mode,
            managed_hot_water=True,
            hot_water_exit_mode=None,
        )
        self._clear_excess_time("hot_water")
        self._clear_low_time()
        await self._save()

    def _water_start_route(self, start: float) -> bool:
        """Can a new native demand be requested at the current temperature?"""
        if self.settings["smart_grid_mode"] == "sg_ready":
            return self.read("smart_grid_request") is not None
        if self._native_boost_available():
            return True
        temperature = self._water_start_temperature()
        return temperature is not None and temperature < start

    def _water_start_temperature(self) -> float | None:
        """Reading for start-route checks; native START logic is controller-owned."""
        temperature = _number(self.read("hot_water_weighted_temperature"))
        if temperature is None:
            temperature = _number(self.read("hot_water_top_temperature"))
        return temperature

    def _native_boost_available(self) -> bool:
        """Only a valid binary read can be saved and safely restored."""
        return self.read("hot_water_boost") in (0, 1)

    async def _exit_energy_excess(self, mode: str) -> None:
        """Persist an explicit Boost Off request before restoring normal water."""
        snapshot = self.state["overrides"].get("hot_water")
        if snapshot is not None:
            snapshot["hot_water_boost"] = False
            snapshot["hot_water_enabled"] = mode != "off"
        self.state["hot_water_exit_mode"] = mode
        await self._save()
        await self._stop_charge("hot_water")
        # Also covers an already-restored override or an interrupted entry.
        if not self._native_boost_available():
            raise ValueError("The native Thermia Boost control is unavailable")
        await self._write("hot_water_boost", False)
        if snapshot is not None:
            await self._write("hot_water_enabled", mode != "off")
        else:
            await self._normal_water(mode)

    async def _write(self, key: str, value: Any) -> None:
        if key == "passive_cooling_enabled":
            value = await self._cooling_permission(bool(value))
        current = self.read(key)
        # Missing values are not equivalent to zero/off.
        if current is None or current != value:
            await self.write(key, value)
        if (
            key == "passive_cooling_enabled"
            and value
            and self.state["cooling_humidity_resume_pending"]
        ):
            self.state["cooling_humidity_resume_pending"] = False
            try:
                await self._save()
            except BaseException:
                self.state["cooling_humidity_resume_pending"] = True
                raise

    async def _snapshot(self, group: str, keys: list[str]) -> None:
        snapshot = self.state["overrides"].get(group)
        if snapshot is None:
            snapshot = {}
        missing = {}
        for key in keys:
            if key not in snapshot:
                value = self.read(key)
                if value is None or (isinstance(value, (float, int)) and not math.isfinite(value)):
                    raise ValueError(f"Cannot safely save the original {key} setting")
                missing[key] = value
        if missing:
            self.state["overrides"][group] = {**snapshot, **missing}
            # Never touch hardware until the recovery values have been saved.
            await self._save()

    async def _water_thresholds(
        self, start: float, stop: float, *, allow_equal: bool = False
    ) -> None:
        equal_boost = allow_equal and start == stop == 60
        if start >= stop and not equal_boost:
            raise ValueError("Hot-water start temperature must be below stop temperature")
        current_start = _number(self.read("hot_water_start"))
        current_stop = _number(self.read("hot_water_stop"))
        if current_start is None or current_stop is None:
            raise ValueError("Hot-water thresholds are not available")
        # Order writes so every intermediate pair remains a valid hysteresis.
        if current_start >= stop:
            await self._write("hot_water_start", start)
        if start >= current_stop:
            await self._write("hot_water_stop", stop)
        await self._write("hot_water_start", start)
        await self._write("hot_water_stop", stop)

    async def _space_requests(self, heating: bool, cooling: bool) -> None:
        if heating and cooling:
            raise ValueError("Heating and cooling cannot be requested together")
        # Clear the other request first, including when a previous write failed.
        try:
            if heating:
                await self._write("passive_cooling_enabled", False)
                await self._write("heating_enabled", True)
            elif cooling:
                await self._write("heating_enabled", False)
                await self._write("passive_cooling_enabled", True)
            else:
                await self._write("heating_enabled", False)
                await self._write("passive_cooling_enabled", False)
        except Exception:
            self.state["cooling_humidity_requested"] = False
            humidity_guard = self.state["overrides"].get("cooling_humidity")
            if humidity_guard is not None:
                humidity_guard["requested"] = False
            guard = self.state["overrides"].get("heating_guard")
            if guard is not None:
                guard["heating_enabled"] = False
            snapshot = self.state["overrides"].get("heating_target_edit")
            if snapshot is not None:
                snapshot.update(heating_enabled=False, passive_cooling_enabled=False)
                snapshot["policy"].update(
                    heating_mode="off", heating_preset="normal", managed_heating=False
                )
                try:
                    await self._save()
                except Exception:
                    pass
            # A lost readback can follow a successful physical write. Attempt
            # each Off request independently before reporting the failure.
            for key in ("heating_enabled", "passive_cooling_enabled"):
                try:
                    # Cached Off can be stale after a lost readback, so this
                    # cleanup must reach the verified writer unconditionally.
                    await self.write(key, False)
                except Exception:
                    pass
            raise

    async def _write_native_settings(self, values: dict[str, Any]) -> None:
        """Order verified native writes, including exact recovery originals.

        Requests are checked against software limits before reaching this
        helper. Recovery may restore an original value outside those limits.
        """
        for key, value in values.items():
            if key not in NATIVE_SETTINGS or isinstance(value, bool) or _number(value) is None:
                raise ValueError(f"Cannot safely write the native {key} setting")
        cooling_target = values.get("passive_cooling_supply_target")
        resume_cooling = bool(self.read("passive_cooling_enabled"))
        if (
            resume_cooling
            and cooling_target is not None
            and cooling_target != self.read("passive_cooling_supply_target")
        ):
            # A user edit or rollback must never lower the water floor with
            # cooling active. Re-enable only through the current guard.
            await self.write("passive_cooling_enabled", False)
        minimum_key, maximum_key = "min_supply_temperature", "max_supply_temperature"
        if minimum_key in values or maximum_key in values:
            current_minimum = _number(self.read(minimum_key))
            current_maximum = _number(self.read(maximum_key))
            if current_minimum is None or current_maximum is None:
                raise ValueError("Both native supply-temperature limits must be available")
            minimum = values.get(minimum_key, current_minimum)
            maximum = values.get(maximum_key, current_maximum)
            if minimum > maximum:
                raise ValueError("Supply line minimum must not exceed its maximum")
            if maximum < current_minimum:
                await self._write(minimum_key, minimum)
            if minimum > current_maximum:
                await self._write(maximum_key, maximum)
            await self._write(minimum_key, minimum)
            await self._write(maximum_key, maximum)
        for key, value in values.items():
            if key not in {minimum_key, maximum_key}:
                await self._write(key, value)
        if cooling_target is not None and resume_cooling:
            await self._write("passive_cooling_enabled", True)

    def _sync_automation_temperature(self, key: str, value: float) -> None:
        state_key = {
            "comfort_wheel": "heating_target",
            "hot_water_start": "hot_water_start",
            "hot_water_stop": "hot_water_target",
        }[key]
        self.state[state_key] = value

    async def _automation_temperature(self, action: str, value: Any) -> None:
        """Edit exactly one native temperature without changing permissions."""
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or _number(value) is None
        ):
            raise ValueError("A finite numeric native temperature is required")
        key = {
            "native_heating_target": "comfort_wheel",
            "native_hot_water_start": "hot_water_start",
            "native_hot_water_stop": "hot_water_stop",
        }[action]
        if self.state["sg_owners"] or self.state["pending_restore"]:
            raise ValueError("Finish Smart Grid charging and pending recovery first")
        if key == "comfort_wheel":
            if (
                self.state["managed_heating"]
                or self.state["heating_preset"] != "normal"
                or {"heating", "heating_profile", "heating_target_edit", "heating_guard"}
                .intersection(self.state["overrides"])
            ):
                raise ValueError(
                    "Use the native Heating/Cooling switches to release thermostat control first"
                )
            requested = _bounded(value, 10, 35, "Native heating target")
            if not math.isclose(requested * 100, round(requested * 100), abs_tol=1e-8):
                raise ValueError("Native heating target must use 0.01 degree steps")
            original = self._native_heating_dial()
        else:
            if self.state["hot_water_mode"] not in {"auto", "off"} or (
                self.state["boost_enabled"] or self.state["boost_once"]
                or self.read("hot_water_boost")
                or {"hot_water", "water_temperature_edit", "normal_water_edit", "water_guard"}
                .intersection(self.state["overrides"])
            ):
                raise ValueError("Finish the hot-water preset or Boost before editing native thresholds")
            maximum_key = (
                "max_start_temperature" if key == "hot_water_start" else "max_hot_water_temperature"
            )
            maximum = _number(self.settings.get(maximum_key))
            if maximum is None:
                raise ValueError("Confirm the pump's hot-water limit in integration options")
            self._validate_setting(maximum_key, maximum)
            requested = _whole_temperature(value, 30, min(60, maximum), "Native hot-water threshold")
            start = _number(self.read("hot_water_start"))
            stop = _number(self.read("hot_water_stop"))
            if start is None or stop is None or start >= stop:
                raise ValueError("A valid native START/STOP pair is required")
            if (requested if key == "hot_water_start" else start) >= (
                requested if key == "hot_water_stop" else stop
            ):
                raise ValueError(
                    "Hot-water START must remain below STOP; the other threshold is unchanged"
                )
            original = start if key == "hot_water_start" else stop
        if requested == original:
            self._sync_automation_temperature(key, requested)
            await self._save()
            return
        group = "automation_temperature"
        await self._snapshot(group, [key])
        snapshot = self.state["overrides"][group]
        try:
            await self._write(key, requested)
            self._sync_automation_temperature(key, requested)
            del self.state["overrides"][group]
            try:
                await self._save()
            except BaseException:
                self.state["overrides"][group] = snapshot
                raise
        except Exception:
            try:
                await self._restore_group(group)
            except Exception:
                pass
            raise
        except BaseException:
            if group not in self.state["pending_restore"]:
                self.state["pending_restore"].append(group)
            await self._save()
            raise

    async def _apply_native_settings(self, values: dict[str, Any]) -> None:
        """Commit a native-setting batch or retain originals until recovered."""
        if not isinstance(values, dict):
            raise ValueError("Native settings must be a mapping of supported names and values")
        requested = {key: validate_native_setting(key, value) for key, value in values.items()}
        if not requested:
            return
        resume_cooling = False
        if "passive_cooling_supply_target" in requested:
            resume_cooling = (
                bool(self.read("passive_cooling_enabled"))
                or self.state["cooling_humidity_requested"]
            )
            if "cooling_humidity" in self.state["overrides"]:
                # This deliberate native edit replaces the baseline; it is
                # distinct from adjusting the temporary dew-point floor.
                await self._restore_group("cooling_humidity")
        keys = list(requested)
        minimum_key, maximum_key = "min_supply_temperature", "max_supply_temperature"
        if minimum_key in requested or maximum_key in requested:
            for key in (minimum_key, maximum_key):
                if key not in keys:
                    keys.append(key)
        originals = {
            key: validate_native_setting(key, self.read(key), require_step=False) for key in keys
        }
        if minimum_key in originals:
            minimum = requested.get(minimum_key, originals[minimum_key])
            maximum = requested.get(maximum_key, originals[maximum_key])
            if originals[minimum_key] > originals[maximum_key] or minimum > maximum:
                raise ValueError("Supply line minimum must not exceed its maximum")
        changed = {key: value for key, value in requested.items() if value != originals[key]}
        if not changed:
            return
        if {maximum_key, "heating_season_stop"}.intersection(changed) and (
            self.state["heating_preset"] == "pv_charge" or "heating" in self.state["overrides"]
        ):
            # A deliberate native baseline edit ends charging. Restore before
            # taking the edit journal: a raised Heat stop is a temporary value,
            # never an original that a failed edit or restart should restore.
            self.state.update(heating_preset="normal", managed_heating=False)
            self._clear_excess_time("heating")
            self._thermal_action = "idle"
            await self._stop_charge("heating")
            self._hydrate()
            await self._save()
        await self._snapshot("native_settings", keys)
        snapshot = self.state["overrides"]["native_settings"]
        try:
            await self._write_native_settings(changed)
            del self.state["overrides"]["native_settings"]
            try:
                await self._save()
            except Exception:
                self.state["overrides"]["native_settings"] = snapshot
                raise
        except Exception:
            try:
                await self._restore_group("native_settings")
            except Exception:
                pass
            raise

        except BaseException:
            if "native_settings" in self.state["overrides"]:
                if "native_settings" not in self.state["pending_restore"]:
                    self.state["pending_restore"].append("native_settings")
                await self._save()
            raise

        if resume_cooling:
            await self._write("passive_cooling_enabled", True)

    async def _acquire_sg(self, owner: str) -> None:
        if self.settings["smart_grid_mode"] != "sg_ready":
            return
        await self._snapshot("smart_grid", ["smart_grid_request"])
        if owner not in self.state["sg_owners"]:
            self.state["sg_owners"].append(owner)
            await self._save()
        await self._protect_sg_channels()
        await self._write("smart_grid_request", 3)

    async def _release_sg(self, owner: str) -> None:
        if owner in self.state["sg_owners"]:
            self.state["sg_owners"].remove(owner)
            await self._save()
        if not self.state["sg_owners"]:
            await self._restore_group("smart_grid")
        await self._protect_sg_channels()

    async def _protect_sg_channels(self) -> None:
        """Global SG boost must not overheat a channel in normal operation."""
        owners = self.state["sg_owners"]
        if owners and "hot_water" not in owners:
            await self._snapshot("water_guard", ["hot_water_enabled"])
            target = (
                self._evening_water_limits()[1]
                if self.state["hot_water_mode"] == "evening"
                else _number(self.state["hot_water_target"])
            )
            temperature = self._water_temperature()
            paused_profile = (
                (
                    self.state["boost_enabled"]
                    or self.settings.get("hot_water_boost_start") is not None
                    or self.read("hot_water_start") == self.read("hot_water_stop") == 60
                )
                and "hot_water" in self.state["overrides"]
                and not self.state["charge_active"]["hot_water"]
            )
            allowed = (
                bool(self.state["overrides"]["water_guard"]["hot_water_enabled"])
                and self.state["hot_water_mode"] != "off"
                and target is not None
                and temperature is not None
                and temperature < target
                and not paused_profile
            )
            await self._write("hot_water_enabled", allowed)
        else:
            await self._restore_group("water_guard")
        if owners and "heating" not in owners and not self.state["managed_heating"]:
            await self._snapshot("heating_guard", ["heating_enabled"])
            target = _number(self.state["heating_target"])
            allowed = (
                bool(self.state["overrides"]["heating_guard"]["heating_enabled"])
                and self._inside is not None
                and target is not None
                and self._inside < target
            )
            await self._write("heating_enabled", allowed)
        else:
            await self._restore_group("heating_guard")

    async def _restore_group(self, group: str) -> None:
        snapshot = self.state["overrides"].get(group)
        if not snapshot:
            return
        try:
            if group == "heating":
                # First remove our fixed supply override, then return the target.
                await self._space_requests(False, False)
                await self._write("fixed_supply_enabled", False)
                await self._write("fixed_supply_target", snapshot["fixed_supply_target"])
                await self._write("fixed_supply_enabled", snapshot["fixed_supply_enabled"])
                for key in ("heating_season_stop", "comfort_wheel"):
                    if key in snapshot:
                        # Readback failures can leave the cache showing an
                        # original which the physical pump has already changed.
                        await self.write(key, snapshot[key])
                await self._write("heating_enabled", snapshot["heating_enabled"])
                await self._write("passive_cooling_enabled", snapshot["passive_cooling_enabled"])
            elif group in {"hot_water", "normal_water_edit", "water_temperature_edit"}:
                water_edit = self.state["overrides"].get("water_temperature_edit")
                if group == "water_temperature_edit":
                    self.state.update(deepcopy(snapshot["policy"]))
                    self._clear_excess_time("hot_water")
                    self._clear_low_time()
                    guard = self.state["overrides"].get("water_guard")
                    if guard is not None:
                        guard["hot_water_enabled"] = snapshot.get(
                            "water_guard_permission", snapshot["hot_water_enabled"]
                        )
                if water_edit is not None and not self._normal_water_permission(
                    water_edit["hot_water_stop"], enabled=water_edit["hot_water_enabled"]
                ):
                    await self._write("hot_water_enabled", False)
                if "hot_water_boost" in snapshot:
                    if group == "water_temperature_edit":
                        await self.write("hot_water_boost", snapshot["hot_water_boost"])
                    else:
                        await self._write("hot_water_boost", snapshot["hot_water_boost"])
                    self.state["native_boost_coupled"] = False
                await self._water_thresholds(
                    snapshot["hot_water_start"],
                    snapshot["hot_water_stop"],
                    allow_equal=snapshot["hot_water_start"] == snapshot["hot_water_stop"] == 60,
                )
                if "hot_water_enabled" in snapshot:
                    allowed = snapshot["hot_water_enabled"]
                    if water_edit is not None:
                        allowed = self._normal_water_permission(
                            snapshot["hot_water_stop"], enabled=allowed
                        )
                    await self._write("hot_water_enabled", allowed)
                    if (
                        group == "water_temperature_edit"
                        and snapshot["hot_water_enabled"]
                        and not allowed
                        and not self.state["sg_owners"]
                        and self.settings["smart_grid_mode"] == "sg_ready"
                        and self.read("smart_grid_request") == 3
                    ):
                        raise RuntimeError(
                            "Normal hot-water permission awaits Smart Grid restoration"
                        )
            elif group == "smart_grid":
                await self._write("smart_grid_request", snapshot["smart_grid_request"])
            elif group in {"water_guard", "heating_guard"}:
                for key, value in snapshot.items():
                    water_edit = self.state["overrides"].get("water_temperature_edit")
                    if (
                        group == "water_guard"
                        and water_edit is not None
                        and not self.state["sg_owners"]
                    ):
                        allowed = self._normal_water_permission(
                            water_edit["hot_water_stop"], enabled=value
                        )
                        await self._write(key, allowed)
                        if value and not allowed:
                            raise RuntimeError(
                                "Normal hot-water permission awaits Smart Grid restoration"
                            )
                        continue
                    await self._write(key, value)
            elif group == "native_settings":
                await self._write_native_settings(snapshot)
            elif group == "automation_temperature":
                for key, value in snapshot.items():
                    # Force restoration even if a lost readback left an old cache.
                    await self.write(key, value)
                    self._sync_automation_temperature(key, value)
            elif group == "cooling_humidity":
                await self.write("passive_cooling_enabled", False)
                await self.write(
                    "passive_cooling_supply_target", snapshot["passive_cooling_supply_target"]
                )
                self.state["cooling_humidity_requested"] = bool(
                    snapshot.get("requested", snapshot["passive_cooling_enabled"])
                )
                self.state["cooling_humidity_paused"] = bool(
                    self._humidity_protection_enabled() and self.state["cooling_humidity_requested"]
                )
                self.state["cooling_humidity_resume_pending"] = bool(
                    self.state["cooling_humidity_requested"]
                    and not (
                        self.settings.get("inside_humidity_sensor")
                        and (
                            self.settings.get("cooling_humidity_enabled")
                            or self.state["heating_preset"] == "vacation"
                        )
                    )
                )
            elif group in {"heating_target_edit", "heating_profile"}:
                self.state.update(deepcopy(snapshot["policy"]))
                if snapshot.get("cancelled_excess"):
                    self._clear_excess_time("heating")
                # Stop both independently without changing the original retry
                # intent if one permission or target restore cannot complete.
                failure = None
                for key in ("heating_enabled", "passive_cooling_enabled"):
                    try:
                        await self.write(key, False)
                    except Exception as err:
                        failure = failure or err
                if failure is not None:
                    raise failure
                # A lost readback can leave the cache showing the original
                # value while the dial has physically changed.
                if "comfort_wheel" in snapshot:
                    await self.write("comfort_wheel", snapshot["comfort_wheel"])
                await self._write("heating_enabled", snapshot["heating_enabled"])
                await self._write("passive_cooling_enabled", snapshot["passive_cooling_enabled"])
                self._thermal_action = "idle"
            else:
                raise ValueError(f"Unknown recovery group {group}")
        except Exception as err:
            if group in {"heating", "heating_target_edit", "heating_profile"}:
                for key in ("heating_enabled", "passive_cooling_enabled"):
                    try:
                        await self.write(key, False)
                    except Exception:
                        pass
            if group in {"hot_water", "water_temperature_edit"}:
                # A failed native Boost or threshold restore must not prevent
                # an independent attempt to remove the water permission.
                try:
                    await self._write("hot_water_enabled", False)
                except Exception:
                    pass
            if group not in self.state["pending_restore"]:
                self.state["pending_restore"].append(group)
            self.state["control_warning"] = f"Restoring {group} is pending: {err}"
            await self._save()
            raise
        del self.state["overrides"][group]
        if group in self.state["pending_restore"]:
            self.state["pending_restore"].remove(group)
        if group in self.state["charge_active"]:
            self.state["charge_active"][group] = False
        if group == "hot_water":
            self.state["native_boost_coupled"] = False
        try:
            await self._save()
        except Exception:
            self.state["overrides"][group] = snapshot
            if group not in self.state["pending_restore"]:
                self.state["pending_restore"].append(group)
            raise

    async def _stop_charge(self, group: str) -> None:
        failure: Exception | None = None
        try:
            await self._restore_group(group)
        except Exception as err:
            failure = err
        # Release shared boosting even if restoring a local setting failed.
        try:
            await self._release_sg(group)
        except Exception as err:
            failure = failure or err
        if failure is not None:
            if group not in self.state["charging_cancelled"]:
                self.state["charging_cancelled"].append(group)
            await self._save()
            raise failure
        self.state["charge_active"][group] = False
        await self._save()

    async def _abort_charge(self, group: str, original: Exception) -> None:
        try:
            await self._stop_charge(group)
        except Exception:
            # The durable snapshots remain for recovery; report the first failure.
            pass
        self.state["control_warning"] = f"Could not start {group} charging: {original}"
        await self._save()

    def _heating_charge_target(self) -> float:
        target = _bounded(
            self.settings.get("max_heating_temperature"), 10, 35, "Heating Excess Energy target"
        )
        self._native_heating_dial()
        try:
            validate_native_setting(
                "heating_season_stop", self.read("heating_season_stop"), require_step=False
            )
        except ValueError as error:
            raise ValueError(
                "Heating Excess Energy requires a readable native Heat stop setting"
            ) from error
        self._heating_charge_heat_stop()
        return target

    def _heating_charge_heat_stop(self, settings: dict[str, Any] | None = None) -> float:
        original = (
            self.state["overrides"]
            .get("heating", {})
            .get("heating_season_stop", self.read("heating_season_stop"))
        )
        original = validate_native_setting("heating_season_stop", original, require_step=False)
        proposed = self.settings if settings is None else {**self.settings, **settings}
        offset = self._validate_setting(
            "heating_excess_heat_stop_offset", proposed["heating_excess_heat_stop_offset"]
        )
        target = original + offset
        if target > NATIVE_SETTINGS["heating_season_stop"].max_value:
            raise ValueError(
                "Original Heat stop plus the Excess Energy offset must not exceed 40°C"
            )
        return target

    def _heating_charge_supply(self) -> float:
        try:
            return validate_native_setting(
                "max_supply_temperature", self.read("max_supply_temperature"), require_step=False
            )
        except ValueError as error:
            raise ValueError(
                "Heating Excess Energy requires a readable native Supply Line Maximum within the integration's software limits"
            ) from error

    async def _charge_heating(self) -> bool:
        try:
            ceiling = self._heating_charge_target()
            supply = self._heating_charge_supply()
            if self._inside is None:
                raise ValueError(
                    "Heating Excess Energy requires a valid measured inside temperature"
                )
            keys = [
                "heating_enabled",
                "passive_cooling_enabled",
                "fixed_supply_enabled",
                "fixed_supply_target",
                "comfort_wheel",
                "heating_season_stop",
            ]
            if self._inside >= ceiling:
                await self._snapshot("heating", keys)
                await self._pause_heating_charge()
                await self._write("comfort_wheel", ceiling)
                await self._save()
                return False
            await self._restore_group("heating_guard")
            await self._snapshot("heating", keys)
            await self._acquire_sg("heating")
            await self._write("passive_cooling_enabled", False)
            await self._write("comfort_wheel", ceiling)
            # Always base the temporary offset on the original journal value,
            # never the already raised setting. Native season rules still apply.
            await self._write("heating_season_stop", self._heating_charge_heat_stop())
            await self._write("fixed_supply_target", supply)
            await self._write("fixed_supply_enabled", True)
            await self._write("heating_enabled", True)
            self.state["charge_active"]["heating"] = True
            await self._save()
        except Exception as err:
            if "heating" in self.state["overrides"]:
                await self._abort_charge("heating", err)
            raise
        return True

    async def _pause_heating_charge(self) -> None:
        """Remove fixed supply at the ceiling without rebasing the originals."""
        snapshot = self.state["overrides"]["heating"]
        failure: Exception | None = None
        try:
            await self._write("fixed_supply_enabled", False)
            await self._write("fixed_supply_target", snapshot["fixed_supply_target"])
            await self._write("fixed_supply_enabled", snapshot["fixed_supply_enabled"])
        except Exception as err:
            failure = err
        try:
            await self._space_requests(False, False)
            self.state["charge_active"]["heating"] = False
        except Exception as err:
            failure = failure or err
        if "heating_season_stop" in snapshot:
            try:
                await self._write("heating_season_stop", snapshot["heating_season_stop"])
            except Exception as err:
                failure = failure or err
        await self._save()
        try:
            await self._release_sg("heating")
        except Exception as err:
            failure = failure or err
        if failure is not None:
            raise failure

    async def _charge_water(
        self, *, once: bool = False, boost: bool = False, energy_excess: bool = False
    ) -> bool:
        energy_excess = energy_excess or self.state["hot_water_mode"] == "energy_excess"
        if energy_excess and not self._native_boost_available():
            raise ValueError("Excess Energy requires an available native Thermia Boost control")
        boost = boost or energy_excess
        continuous_boost = boost or self.state["boost_enabled"]
        fixed_boost = once or self.state["boost_once"] or continuous_boost
        initial_boost = (once and not self.state["boost_once"]) or (
            boost and not self.state["boost_enabled"]
        )
        stop, start, _ = self._water_limits(once=fixed_boost)
        temperature = self._water_temperature()
        if fixed_boost and temperature is None:
            raise ValueError(
                "Hot-water boost needs a valid tank temperature to enforce its 60°C ceiling"
            )
        keys = ["hot_water_enabled", "hot_water_start", "hot_water_stop"]
        native_boost = self._native_boost_available()
        self.state["native_boost_available"] = bool(native_boost)
        if native_boost:
            keys.append("hot_water_boost")
        if temperature is not None and temperature >= stop:
            try:
                if continuous_boost or (
                    not fixed_boost and self.settings.get("hot_water_boost_start") is not None
                ):
                    await self._snapshot("hot_water", keys)
                    if initial_boost:
                        original = self.state["overrides"]["hot_water"]
                        self.state.update(
                            hot_water_start=original["hot_water_start"],
                            hot_water_target=original["hot_water_stop"],
                        )
                        await self._save()
                    await self._pause_water_charge()
                    await self._water_thresholds(start, stop)
                else:
                    await self._stop_charge("hot_water")
            except Exception as err:
                await self._abort_charge("hot_water", err)
                raise
            return False
        if not self.state["charge_active"]["hot_water"] and not self._water_start_route(start):
            raise ValueError(
                "An immediate hot-water demand cannot be requested at this temperature: "
                "verify a native boost control or SG Ready support first"
            )
        try:
            await self._restore_group("water_guard")
            await self._snapshot("hot_water", keys)
            if initial_boost:
                original = self.state["overrides"]["hot_water"]
                self.state.update(
                    hot_water_start=original["hot_water_start"],
                    hot_water_target=original["hot_water_stop"],
                )
                await self._save()
            await self._acquire_sg("hot_water")
            await self._water_thresholds(start, stop)
            await self._write("hot_water_enabled", True)
            if native_boost:
                await self._write("hot_water_boost", True)
            self.state["native_boost_coupled"] = bool(native_boost)
            self.state["charge_active"]["hot_water"] = True
            await self._save()
        except Exception as err:
            await self._abort_charge("hot_water", err)
            raise
        return True

    async def _pause_water_charge(self) -> None:
        """Keep explicit boost bounds and recovery values during hysteresis."""
        snapshot = self.state["overrides"].get("hot_water", {})
        failure: Exception | None = None
        if "hot_water_boost" in snapshot:
            try:
                await self._write("hot_water_boost", False)
                self.state["native_boost_coupled"] = False
            except Exception as err:
                failure = err
        try:
            await self._write("hot_water_enabled", False)
            self.state["charge_active"]["hot_water"] = False
        except Exception as err:
            failure = failure or err
        await self._save()
        try:
            await self._release_sg("hot_water")
        except Exception as err:
            failure = failure or err
        if failure is not None:
            raise failure

    async def _normal_heating(self, mode: str | None = None) -> None:
        mode = mode or self.state["heating_mode"]
        if mode == "off":
            action = "idle"
        elif mode == "heat":
            action = "heating"
        elif mode == "cool":
            if self.state["heating_preset"] in {"low", "vacation"}:
                if self._inside is None:
                    await self._space_requests(False, False)
                    self._thermal_action = "idle"
                    self.state["control_warning"] = "Cooling paused: missing inside temperature"
                    return
                target = _bounded(self.effective_heating_target("cool"), 10, 35, "Cooling target")
                hysteresis = self._validate_setting("hysteresis", self.settings["hysteresis"])
                action = self._thermal_action
                if action != "cooling" or self._inside <= target:
                    action = "idle"
                if action == "idle" and self._inside >= target + hysteresis:
                    action = "cooling"
            else:
                action = "cooling"
        elif self._inside is None:
            await self._space_requests(False, False)
            self._thermal_action = "idle"
            if self.state["heating_preset"] in {"low", "vacation"}:
                self.state["control_warning"] = (
                    "Automatic heat/cool paused: missing inside temperature"
                )
                return
            raise ValueError("Automatic heat/cool requires a measured inside temperature")
        else:
            hysteresis = self._validate_setting("hysteresis", self.settings["hysteresis"])
            low, high = self.effective_heating_range()
            action = self._thermal_action
            if action == "heating" and self._inside >= low:
                action = "idle"
            if action == "cooling" and self._inside <= high:
                action = "idle"
            if action == "idle":
                if self._inside <= low - hysteresis:
                    action = "heating"
                elif self._inside >= high + hysteresis:
                    action = "cooling"
        local_journal = False
        target = None
        if mode in {"heat", "heat_cool"}:
            target = self.effective_heating_target(mode)
            target = _bounded(target, 10, 35, "Native heating target")
            native = self._native_heating_dial()
            needs_write = (
                native != target
                or self.read("heating_enabled") != (action == "heating")
                or self.read("passive_cooling_enabled") != (action == "cooling")
            )
            if needs_write and not {"heating_target_edit", "heating_profile"}.intersection(
                self.state["overrides"]
            ):
                await self._begin_heating_target_edit()
                local_journal = True
        try:
            if target is not None:
                await self._write("comfort_wheel", target)
            await self._space_requests(action == "heating", action == "cooling")
            self._thermal_action = action
            if local_journal:
                await self._commit_heating_target_edit()
        except Exception:
            if local_journal:
                try:
                    await self._restore_group("heating_target_edit")
                except Exception:
                    pass
            raise

    async def _normal_water(self, mode: str | None = None) -> None:
        mode = mode or self.state["hot_water_mode"]
        if mode == "off":
            await self._write("hot_water_enabled", False)
            return
        self._hydrate()
        start, stop = self.state["hot_water_start"], self.state["hot_water_target"]
        if start is None or stop is None:
            raise ValueError("The native hot-water temperature settings are unavailable")
        await self._water_thresholds(start, stop)
        await self._write("hot_water_enabled", True)

    async def command(self, action: str, value: Any = None) -> None:
        """Apply one user command. Selected modes change only after successful writes."""
        if self.external_control and action not in {
            "native_heating_target", "native_hot_water_start", "native_hot_water_stop",
            "native_heating_enabled", "native_cooling_enabled", "hot_water_enabled",
            "native_hot_water_boost", "native_setting", "native_settings",
            "auxiliary_heater", "anti_legionella", "setting",
        }:
            raise ValueError(
                "External Control / EMHASS is selected: use native switches and temperature numbers; "
                "integration thermostats and presets are disabled"
            )
        await self._ensure_external_handoff()
        self._hydrate()
        if self.state["pending_restore"]:
            await self._retry_pending()
            if self.state["pending_restore"]:
                raise RuntimeError(
                    "A previous override still needs restoring; retry after connection recovers"
                )
        if self.external_control:
            self._release_managed_control()
        self.state["control_warning"] = None
        try:
            if action in {
                "native_heating_target", "native_hot_water_start", "native_hot_water_stop"
            }:
                await self._automation_temperature(action, value)
                return
            if action in {"heating_target", "heating_range"} and self.state["heating_preset"] in {
                "low",
                "vacation",
            }:
                payload = (
                    {"temperature": value}
                    if action == "heating_target"
                    else {"target_temp_low": value[0], "target_temp_high": value[1]}
                )
                await self.command("heating_temperature_edit", payload)
                return
            await self._prepare_heating_target_command(action, value)
            if action == "heating_temperature_edit":
                await self._edit_heating_temperature(value)
                return
            if action == "hot_water_temperature_edit":
                await self._edit_hot_water_temperature(value)
                return
            if action == "native_setting":
                key, new_value = value
                await self.command("native_settings", {key: new_value})
                return
            if action == "native_settings":
                await self._apply_native_settings(value)
                return
            if action in {"heating_enabled", "hot_water_enabled"}:
                if not isinstance(value, bool):
                    raise ValueError("The enable switch must be on or off")
                if self.external_control and action == "hot_water_enabled":
                    if self.read("hot_water_enabled") not in (0, 1):
                        raise ValueError("The native hot-water permission is unavailable")
                    await self._write("hot_water_enabled", value)
                    self.state["managed_hot_water"] = False
                    self._hydrate()
                    await self._save()
                    return
                if action == "heating_enabled":
                    if value and self.state["heating_mode"] != "off":
                        return
                    mode = self.state.get("last_heating_mode", "heat") if value else "off"
                    if value and mode not in {"heat", "cool", "heat_cool"}:
                        mode = "heat"
                    await self.command("heating_mode", mode)
                else:
                    if value and self.state["hot_water_mode"] != "off":
                        return
                    await self.command("hot_water_mode", "auto" if value else "off")
                return
            if action in {"native_heating_enabled", "native_cooling_enabled"}:
                if not isinstance(value, bool):
                    raise ValueError("The enable switch must be on or off")
                key = (
                    "heating_enabled"
                    if action == "native_heating_enabled"
                    else "passive_cooling_enabled"
                )
                if self.read(key) not in (0, 1):
                    raise ValueError("The native function permission is unavailable")
                await self._stop_charge("heating")
                await self._restore_group("heating_profile")
                await self._write(key, value)
                guard = self.state["overrides"].get("heating_guard")
                if guard is not None and key in guard:
                    guard[key] = value
                self.state.update(heating_preset="normal", managed_heating=False)
                self._clear_excess_time("heating")
                self._thermal_action = "idle"
                self._hydrate()
                await self._save()
                return
            if action == "native_hot_water_boost":
                if not isinstance(value, bool):
                    raise ValueError("Thermia Boost must be on or off")
                if not self._native_boost_available():
                    raise ValueError("The native Thermia Boost control is unavailable")
                if value and self.state["hot_water_mode"] == "evening":
                    raise ValueError("Select hot-water Normal before enabling Thermia Boost")
                if self.state["hot_water_mode"] == "energy_excess" or self.state["boost_enabled"]:
                    if not value:
                        raise ValueError(
                            "Select hot-water Normal or Off to end Excess Energy first"
                        )
                    temperature = self._water_temperature()
                    if temperature is None or temperature >= 60:
                        raise ValueError(
                            "Thermia Boost is paused at the Excess Energy safety ceiling"
                        )
                await self._write("hot_water_boost", value)
                self.state["native_boost_available"] = True
                await self._save()
                return
            if action == "heating_mode":
                if value not in {"off", "heat", "cool", "heat_cool"}:
                    raise ValueError("Unsupported heating mode")
                if value == "heat_cool" and self._inside is None:
                    raise ValueError("Automatic heat/cool requires a measured inside temperature")
                if value == "off" and (
                    self.state["heating_preset"] in {"pv_charge", "low", "vacation"}
                    or {"heating", "heating_profile"}.intersection(self.state["overrides"])
                ):
                    previous_mode = self.state["heating_mode"]
                    last_mode = (
                        previous_mode if previous_mode != "off" else self.state["last_heating_mode"]
                    )
                    # Off is durable intent, rather than an override pause.
                    # Every journal that could restore a space permission
                    # must carry it before the first restoration write.
                    for group in ("heating", "heating_profile", "heating_target_edit"):
                        snapshot = self.state["overrides"].get(group)
                        if snapshot is not None:
                            snapshot.update(heating_enabled=False, passive_cooling_enabled=False)
                            snapshot.get("policy", {}).update(
                                heating_mode="off",
                                heating_preset="normal",
                                managed_heating=True,
                                last_heating_mode=last_mode,
                            )
                    guard = self.state["overrides"].get("heating_guard")
                    if guard is not None:
                        guard["heating_enabled"] = False
                    guard = self.state["overrides"].get("cooling_humidity")
                    if guard is not None:
                        guard.update(requested=False, passive_cooling_enabled=False)
                    self.state.update(
                        heating_mode="off",
                        heating_preset="normal",
                        managed_heating=True,
                        last_heating_mode=last_mode,
                        cooling_humidity_requested=False,
                        cooling_humidity_resume_pending=False,
                        cooling_humidity_paused=False,
                    )
                    self._clear_excess_time("heating")
                    await self._save()
                    await self._stop_charge("heating")
                    await self._restore_group("heating_profile")
                    await self._normal_heating("off")
                    await self._protect_sg_channels()
                    await self._save()
                    return
                if self.state["heating_preset"] in {"low", "vacation"}:
                    if self.state["heating_preset"] == "low" and value == "cool":
                        await self._begin_heating_target_edit(sync_target=False)
                        await self._exit_heating_profile(mode=value)
                        await self._commit_heating_target_edit()
                        return
                    await self._apply_heating_profile(self.state["heating_preset"], mode=value)
                    return
                await self._stop_charge("heating")
                self.state["heating_preset"] = "normal"
                try:
                    await self._normal_heating(value)
                except Exception:
                    self.state.update(
                        heating_mode="off", heating_preset="normal", managed_heating=False
                    )
                    self._clear_excess_time("heating")
                    self._thermal_action = "idle"
                    await self._save()
                    raise
                if value != "off":
                    self.state["last_heating_mode"] = value
                elif self.state["heating_mode"] != "off":
                    self.state["last_heating_mode"] = self.state["heating_mode"]
                self.state.update(heating_mode=value, heating_preset="normal", managed_heating=True)
                self._clear_excess_time("heating")
            elif action == "heating_preset":
                if value not in {"normal", "pv_charge", "low", "vacation"}:
                    raise ValueError("Unsupported heating preset")
                if value in {"low", "vacation"}:
                    await self._apply_heating_profile(value)
                    return
                if value == "pv_charge":
                    if self.state["heating_mode"] == "cool":
                        raise ValueError("Excess Energy heats the house; it is unavailable in Cool")
                    self._heating_charge_supply()
                    if "heating_profile" in self.state["overrides"]:
                        mode = self.state["heating_mode"]
                        await self._restore_group("heating_profile")
                        self.state.update(heating_mode=mode, managed_heating=True)
                    fresh = await self._start_excess_time("heating")
                    try:
                        await self._charge_heating()
                    except Exception:
                        if fresh and not self.state["pending_restore"]:
                            self._clear_excess_time("heating")
                        raise
                    if self.state["heating_mode"] in {"off", "cool"}:
                        self.state["heating_mode"] = "heat"
                else:
                    if "heating_profile" in self.state["overrides"]:
                        await self._exit_heating_profile()
                        await self._commit_heating_target_edit()
                        return
                    await self._finish_heating_excess(managed=True)
                    await self._commit_heating_target_edit()
                    return
                self.state.update(heating_preset=value, managed_heating=True)
            elif action == "heating_target":
                target = _whole_temperature(value, 10, 35, "Heating target")
                old_target = self.state["heating_target"]
                self.state["heating_target"] = target
                if self.state["heating_preset"] == "pv_charge" and self.state[
                    "heating_excess_previous_mode"
                ] in {"heat", "heat_cool"}:
                    self.state["heating_excess_previous_managed"] = True
                try:
                    if self.state["heating_preset"] == "normal" and self.state["heating_mode"] in {
                        "heat",
                        "heat_cool",
                    }:
                        await self._normal_heating()
                        self.state["managed_heating"] = True
                except Exception:
                    self.state["heating_target"] = old_target
                    raise
            elif action == "heating_range":
                low, high = value
                low = _bounded(low, 10, 35, "Heating lower target")
                high = _bounded(high, 10, 35, "Cooling upper target")
                if low != self.state["heating_low"]:
                    _whole_temperature(low, 10, 35, "Heating lower target")
                if high != self.state["heating_high"]:
                    _whole_temperature(high, 10, 35, "Cooling upper target")
                if low >= high:
                    raise ValueError("The heating target must be below the cooling target")
                old = self.state["heating_low"], self.state["heating_high"]
                self.state.update(heating_low=low, heating_high=high)
                if self.state["heating_preset"] == "pv_charge" and self.state[
                    "heating_excess_previous_mode"
                ] in {"heat", "heat_cool"}:
                    self.state["heating_excess_previous_managed"] = True
                try:
                    if self.state["heating_preset"] == "normal" and self.state["heating_mode"] in {
                        "heat",
                        "heat_cool",
                    }:
                        await self._normal_heating()
                        self.state["managed_heating"] = True
                except Exception:
                    self.state.update(heating_low=old[0], heating_high=old[1])
                    raise
            elif action == "hot_water_mode":
                if value not in {"off", "auto", "manual_on", "energy_excess", "evening"}:
                    raise ValueError("Unsupported hot-water mode")
                if value == "evening":
                    self._evening_water_limits()
                    if self.state["hot_water_mode"] == "energy_excess":
                        await self._finish_water_excess()
                    elif self.state["hot_water_mode"] != "evening":
                        await self._stop_charge("hot_water")
                    await self._start_low_time()
                    await self._apply_evening_water()
                    self.state.update(
                        hot_water_mode="evening",
                        managed_hot_water=True,
                        boost_enabled=False,
                        boost_once=False,
                        boost_previous_mode=None,
                        boost_previous_managed=False,
                        hot_water_exit_mode=None,
                    )
                    self._clear_excess_time("hot_water")
                elif value == "energy_excess":
                    self._water_limits(once=True)
                    if not self._native_boost_available():
                        raise ValueError(
                            "Excess Energy requires an available native Thermia Boost control"
                        )
                    if self._water_temperature() is None:
                        raise ValueError("Excess Energy requires a valid tank temperature")
                    if self.state["hot_water_mode"] == "evening":
                        await self._exit_evening_water()
                    if self.state["hot_water_mode"] == "manual_on" and not (
                        self.state["boost_enabled"] or self.state["boost_once"]
                    ):
                        await self._stop_charge("hot_water")
                    already_excess = self.state["hot_water_mode"] == "energy_excess"
                    previous = (
                        self.state["boost_previous_mode"]
                        if already_excess
                        else self.state["hot_water_mode"]
                    )
                    previous_managed = (
                        self.state["boost_previous_managed"]
                        if already_excess
                        else self.state["managed_hot_water"]
                    )
                    fresh = await self._start_excess_time("hot_water")
                    try:
                        await self._charge_water(boost=True, energy_excess=True)
                    except Exception:
                        if fresh and not self.state["pending_restore"]:
                            self._clear_excess_time("hot_water")
                        raise
                    self.state.update(
                        hot_water_mode="energy_excess",
                        managed_hot_water=True,
                        boost_enabled=True,
                        boost_once=False,
                        boost_previous_mode=previous,
                        boost_previous_managed=previous_managed,
                        hot_water_exit_mode=None,
                    )
                    await self._protect_sg_channels()
                    await self._save()
                    return
                elif value == "manual_on" and self.state["boost_enabled"]:
                    return
                elif value == "manual_on":
                    if self.state["hot_water_mode"] == "evening":
                        await self._exit_evening_water()
                    await self._charge_water()
                elif value != "evening":
                    if self.state["hot_water_mode"] == "energy_excess":
                        await self._exit_energy_excess(value)
                    elif self.state["hot_water_mode"] == "evening":
                        await self._exit_evening_water(value)
                    else:
                        await self._stop_charge("hot_water")
                        await self._normal_water(value)
                self.state.update(
                    hot_water_mode=value,
                    managed_hot_water=True,
                    boost_once=False,
                    boost_enabled=False,
                    boost_previous_mode=None,
                    hot_water_exit_mode=None,
                )
                self._clear_excess_time("hot_water")
                if value != "evening":
                    self._clear_low_time()
            elif action == "hot_water_range":
                if self.state["hot_water_mode"] == "evening":
                    raise ValueError("Select hot-water Normal before editing its temperature range")
                start_value, stop_value = value
                start_maximum = _number(self.settings.get("max_start_temperature"))
                stop_maximum = _number(self.settings.get("max_hot_water_temperature"))
                if start_maximum is None or stop_maximum is None:
                    raise ValueError("Confirm both hot-water pump limits in integration options")
                start = _bounded(start_value, 30, min(60, start_maximum), "Normal hot-water start")
                stop = _bounded(stop_value, 30, min(60, stop_maximum), "Normal hot-water stop")
                if start != self.state["hot_water_start"]:
                    _whole_temperature(start, 30, min(60, start_maximum), "Normal hot-water start")
                if stop != self.state["hot_water_target"]:
                    _whole_temperature(stop, 30, min(60, stop_maximum), "Normal hot-water stop")
                if start >= stop:
                    raise ValueError("Hot-water start temperature must be below target temperature")
                if self.state["hot_water_mode"] in {"manual_on", "energy_excess"}:
                    # Save the newly requested normal pair for every exit path,
                    # including shutdown/recovery, while leaving active boost alone.
                    await self._snapshot(
                        "hot_water", ["hot_water_enabled", "hot_water_start", "hot_water_stop"]
                    )
                    self.state["overrides"]["hot_water"].update(
                        hot_water_start=start, hot_water_stop=stop
                    )
                else:
                    await self._snapshot("normal_water_edit", ["hot_water_start", "hot_water_stop"])
                    try:
                        await self._water_thresholds(start, stop)
                    except Exception:
                        try:
                            await self._restore_group("normal_water_edit")
                        except Exception:
                            pass
                        raise
                    del self.state["overrides"]["normal_water_edit"]
                self.state.update(hot_water_start=start, hot_water_target=stop)
            elif action in {"hot_water_target", "hot_water_start"}:
                self._hydrate()
                value = _whole_temperature(value, 30, 60, "Hot-water temperature")
                start = value if action == "hot_water_start" else self.state["hot_water_start"]
                stop = value if action == "hot_water_target" else self.state["hot_water_target"]
                await self.command("hot_water_range", (start, stop))
                return
            elif action == "passive_cooling":
                if not isinstance(value, bool):
                    raise ValueError("Passive cooling must be on or off")
                await self._stop_charge("heating")
                await self._restore_group("heating_profile")
                self._clear_excess_time("heating")
                if value:
                    await self._space_requests(False, True)
                    self.state.update(
                        heating_mode="cool", heating_preset="normal", managed_heating=True
                    )
                    self._thermal_action = "cooling"
                else:
                    await self._write("passive_cooling_enabled", False)
                    if self.state["heating_mode"] == "cool":
                        self.state.update(
                            heating_mode="off", heating_preset="normal", managed_heating=True
                        )
                    self._thermal_action = "idle"
            elif action in {"auxiliary_heater", "anti_legionella"}:
                if not isinstance(value, bool):
                    raise ValueError("The switch value must be on or off")
                if action == "anti_legionella" and self.read("anti_legionella_enabled") not in (
                    0,
                    1,
                ):
                    raise ValueError("The native anti-legionella control is unavailable")
                await self._write(
                    "immersion_heater"
                    if action == "auxiliary_heater"
                    else "anti_legionella_enabled",
                    (2 if value else 0) if action == "auxiliary_heater" else value,
                )
            elif action == "hot_water_boost_enabled":
                if not isinstance(value, bool):
                    raise ValueError("The hot-water boost switch must be on or off")
                if value:
                    if self.state["boost_enabled"]:
                        return
                    self._water_limits(once=True)
                    if self.state["hot_water_mode"] == "evening":
                        await self._exit_evening_water()
                    if self.state["boost_once"]:
                        self.state.update(boost_enabled=True, boost_once=False)
                    else:
                        if self.state["hot_water_mode"] == "manual_on":
                            raise ValueError(
                                "Cancel continuous Manual on before enabling the fixed boost"
                            )
                        previous = self.state["hot_water_mode"]
                        previous_managed = self.state["managed_hot_water"]
                        await self._charge_water(boost=True)
                        self.state.update(
                            hot_water_mode="manual_on",
                            managed_hot_water=True,
                            boost_enabled=True,
                            boost_once=False,
                            boost_previous_mode=previous,
                            boost_previous_managed=previous_managed,
                        )
                else:
                    if self.state["hot_water_mode"] == "energy_excess":
                        await self.command("hot_water_mode", "auto")
                        return
                    if not self.state["boost_enabled"]:
                        return
                    previous = self.state["boost_previous_mode"] or "auto"
                    previous_managed = self.state["boost_previous_managed"]
                    await self._stop_charge("hot_water")
                    self.state.update(
                        hot_water_mode=previous,
                        managed_hot_water=previous_managed,
                        boost_enabled=False,
                        boost_once=False,
                        boost_previous_mode=None,
                        boost_previous_managed=False,
                    )
            elif action == "boost_once":
                if self.state["boost_once"]:
                    return
                if self.state["hot_water_mode"] == "manual_on":
                    raise ValueError("Hot water is already in continuous manual charging mode")
                if self.state["hot_water_mode"] == "evening":
                    self._water_limits(once=True)
                    await self._exit_evening_water()
                previous = self.state["hot_water_mode"]
                previous_managed = self.state["managed_hot_water"]
                active = await self._charge_water(once=True)
                if active:
                    self.state.update(
                        hot_water_mode="manual_on",
                        managed_hot_water=True,
                        boost_once=True,
                        boost_previous_mode=previous,
                        boost_previous_managed=previous_managed,
                    )
            elif action == "setting":
                name, new_value = value
                if name == "control_mode":
                    await self.set_control_mode(new_value)
                    return
                new_value = self._validate_setting(name, new_value)
                if name in {
                    "heating_excess_hours",
                    "hot_water_excess_hours",
                    "hot_water_low_hours",
                }:
                    new_value = self._validate_new_duration(name, new_value)
                if (
                    name
                    in {
                        "max_hot_water_temperature",
                        "max_start_temperature",
                        "max_heating_temperature",
                        "hot_water_hysteresis",
                        "hot_water_evening_start",
                        "hot_water_evening_stop",
                        "hot_water_boost_start",
                        "hot_water_boost_stop",
                        "heating_low_offset",
                        "heating_vacation_temperature",
                        "heating_vacation_cooling_offset",
                        "cooling_dew_point_margin",
                    }
                    and new_value is not None
                ):
                    _whole_temperature(new_value, float("-inf"), float("inf"), name)
                previous_setting = self.settings.get(name)
                saved_setting_present = name in self.state["settings"]
                previous_saved_setting = self.state["settings"].get(name)
                if name == "heating_excess_heat_stop_offset" and (
                    self.state["heating_preset"] == "pv_charge"
                    or "heating" in self.state["overrides"]
                ):
                    self._heating_charge_heat_stop({name: new_value})
                if name in {"hot_water_evening_start", "hot_water_evening_stop"}:
                    self._evening_water_limits(
                        {name: new_value}, require_caps=self.state["hot_water_mode"] == "evening"
                    )
                if name in {
                    "heating_low_offset",
                    "heating_vacation_temperature",
                    "heating_vacation_cooling_offset",
                } and self.state["heating_preset"] in {"low", "vacation"}:
                    self.effective_heating_range(settings={name: new_value}) if self.state[
                        "heating_mode"
                    ] == "heat_cool" else self.effective_heating_target(settings={name: new_value})
                if name == "hot_water_hysteresis" and (
                    self.state["boost_enabled"] or self.state["boost_once"]
                ):
                    previous = self.settings[name]
                    self.settings[name] = new_value
                    try:
                        self._water_limits(once=True)
                    except Exception:
                        self.settings[name] = previous
                        raise
                self.settings[name] = new_value
                self.state["settings"][name] = new_value
                if name in {
                    "inside_humidity_sensor",
                    "cooling_humidity_enabled",
                    "cooling_humidity_limit",
                    "cooling_vacation_humidity_limit",
                    "cooling_dew_point_margin",
                }:
                    try:
                        await self._sync_cooling_humidity()
                        if self.state["managed_heating"]:
                            await self._normal_heating(self.state["heating_mode"])
                    except Exception:
                        self.settings[name] = previous_setting
                        if saved_setting_present:
                            self.state["settings"][name] = previous_saved_setting
                        else:
                            self.state["settings"].pop(name, None)
                        await self._save()
                        raise
                if name in {
                    "heating_low_offset",
                    "heating_vacation_temperature",
                    "heating_vacation_cooling_offset",
                } and self.state["heating_preset"] in {"low", "vacation"}:
                    try:
                        await self._apply_heating_profile(self.state["heating_preset"])
                    except Exception:
                        self.settings[name] = previous_setting
                        if saved_setting_present:
                            self.state["settings"][name] = previous_saved_setting
                        else:
                            self.state["settings"].pop(name, None)
                        await self._save()
                        raise
                if name in {
                    "heating_excess_hours",
                    "hot_water_excess_hours",
                    "hot_water_low_hours",
                }:
                    await self._expire_excess()
                if name in {"hot_water_evening_start", "hot_water_evening_stop"} and (
                    self.state["hot_water_mode"] == "evening"
                ):
                    try:
                        await self._apply_evening_water()
                    except Exception:
                        self.settings[name] = previous_setting
                        if saved_setting_present:
                            self.state["settings"][name] = previous_saved_setting
                        else:
                            self.state["settings"].pop(name, None)
                        await self._save()
                        raise
            else:
                raise ValueError(f"Unknown control action: {action}")
            # Preserve an explicitly selected normal state when a global boost
            # ends; guard snapshots must not resurrect an older enable setting.
            if action in {"hot_water_mode", "heating_mode", "passive_cooling"}:
                guard = "water_guard" if action == "hot_water_mode" else "heating_guard"
                key = "hot_water_enabled" if guard == "water_guard" else "heating_enabled"
                if guard in self.state["overrides"]:
                    self.state["overrides"][guard][key] = (
                        True
                        if action == "hot_water_mode" and value == "evening"
                        else bool(self.read(key))
                    )
            await self._protect_sg_channels()
            await self._save()
            await self._commit_heating_target_edit()
        except Exception as err:
            if (
                action == "hot_water_mode"
                and value == "evening"
                and self.state["hot_water_mode"] != "evening"
                and not self.state["pending_restore"]
            ):
                self._clear_low_time()
            edit_group = {
                "heating_temperature_edit": ("heating", "heating_target_edit"),
                "hot_water_temperature_edit": ("hot_water", "water_temperature_edit"),
            }.get(action)
            if edit_group is not None and edit_group[1] in self.state["overrides"]:
                try:
                    await self._stop_charge(edit_group[0])
                except Exception:
                    pass
            if "water_temperature_edit" in self.state["overrides"]:
                try:
                    await self._restore_group("water_temperature_edit")
                except Exception:
                    pass
            if "heating_target_edit" in self.state["overrides"]:
                try:
                    await self._restore_group("heating_target_edit")
                except Exception:
                    pass
            if not self.state["pending_restore"]:
                self.state["control_warning"] = str(err)
            await self._save()
            raise

    async def _retry_pending(self) -> None:
        pending = sorted(self.state["pending_restore"], key=lambda item: item != "smart_grid")
        for group in pending:
            if group == "smart_grid" and self.state["sg_owners"]:
                continue
            try:
                await self._restore_group(group)
            except Exception:
                continue
        if not self.state["pending_restore"]:
            for group in self.state["charging_cancelled"]:
                if group == "heating":
                    previous = self.state["heating_excess_previous_mode"]
                    self.state.update(
                        heating_preset="normal",
                        managed_heating=(
                            self.state["heating_excess_previous_managed"]
                            if previous is not None
                            else False
                        ),
                    )
                    if previous is not None:
                        self.state["heating_mode"] = previous
                    self._clear_excess_time("heating")
                elif group == "hot_water":
                    exit_mode = self.state.get("hot_water_exit_mode")
                    previous = exit_mode or (
                        self.state["boost_previous_mode"]
                        if self.state["boost_once"] or self.state["boost_enabled"]
                        else "auto"
                    )
                    self.state.update(
                        hot_water_mode=previous or "auto",
                        managed_hot_water=exit_mode is not None,
                        boost_once=False,
                        boost_previous_mode=None,
                        boost_enabled=False,
                        native_boost_coupled=False,
                        hot_water_exit_mode=None,
                    )
                    self._clear_excess_time("hot_water")
            self.state["charging_cancelled"] = []
            if not self.state["sg_owners"]:
                for group in ("water_guard", "heating_guard"):
                    try:
                        await self._restore_group(group)
                    except Exception:
                        pass
            if not self.state["pending_restore"]:
                self.state["control_warning"] = None
                if self.state["heating_preset"] != "pv_charge":
                    self._clear_excess_time("heating")
                if self.state["hot_water_mode"] != "energy_excess":
                    self._clear_excess_time("hot_water")
                if self.state["hot_water_mode"] != "evening":
                    self._clear_low_time()
            await self._save()

    async def recover(self) -> bool:
        previous = self._restoring
        self._restoring = True
        try:
            return await self._recover_saved()
        finally:
            self._restoring = previous

    async def _recover_saved(self) -> bool:
        """Restore saved overrides after restart; never resume a previous PV run."""
        if self.state["heating_preset"] in {"pv_charge", "low", "vacation"} or {
            "heating",
            "heating_profile",
        }.intersection(self.state["overrides"]):
            self.state.update(heating_preset="normal", managed_heating=False)
        if self.state["hot_water_mode"] == "energy_excess":
            snapshot = self.state["overrides"].get("hot_water")
            if snapshot is not None and "hot_water_boost" in snapshot:
                snapshot["hot_water_boost"] = False
        if (
            self.state["hot_water_mode"] in {"manual_on", "energy_excess"}
            or "hot_water" in self.state["overrides"]
        ):
            self.state.update(hot_water_mode="auto", managed_hot_water=False)
        self.state.update(
            boost_once=False,
            boost_enabled=False,
            native_boost_coupled=False,
            boost_previous_mode=None,
            boost_previous_managed=False,
            hot_water_exit_mode=None,
            sg_owners=[],
        )
        for group in self.state["overrides"]:
            if group not in self.state["pending_restore"]:
                self.state["pending_restore"].append(group)
        await self._save()
        await self._retry_pending()
        if self.external_control:
            self._release_managed_control()
            await self._save()
        if not self.state["pending_restore"]:
            await self._sync_cooling_humidity()
        self._hydrate()
        return not self.state["pending_restore"]

    async def shutdown(self) -> bool:
        """Return temporary overrides to their native values before unloading."""
        return await self.recover()

    def update_measurements(
        self, inside: float | None, outdoor: float | None, humidity: float | None = None
    ) -> None:
        """Refresh effective readings immediately before evaluating a command."""
        self._inside = _number(inside)
        self._outdoor = _number(outdoor)
        self._humidity = valid_humidity(humidity)

    async def _pending_water_safety(self) -> None:
        """Remove fixed water demand without restarting during another recovery."""
        if not (self.state["boost_enabled"] or self.state["boost_once"]):
            return
        temperature = self._water_temperature()
        if temperature is None or temperature >= 60:
            await self._pause_water_charge()
        if temperature is None:
            previous = self.state["boost_previous_mode"] or "auto"
            previous = "off" if previous == "off" else "auto"
            if self.state["hot_water_mode"] == "energy_excess":
                await self._exit_energy_excess(previous)
            else:
                await self._stop_charge("hot_water")
            self.state.update(
                hot_water_mode=previous,
                managed_hot_water=False,
                boost_enabled=False,
                boost_once=False,
                boost_previous_mode=None,
                boost_previous_managed=False,
                native_boost_coupled=False,
                hot_water_exit_mode=None,
            )
            self._clear_excess_time("hot_water")
            await self._save()

    async def tick(
        self, inside: float | None, outdoor: float | None, humidity: float | None = None
    ) -> None:
        """Enforce charging ceilings and explicit thermostat requests."""
        self.update_measurements(inside, outdoor, humidity)
        self._hydrate()
        failure = None
        try:
            if self.external_control:
                await self._ensure_external_handoff()
                if self.state["pending_restore"]:
                    await self._retry_pending()
                if self.state["pending_restore"]:
                    raise ValueError("External control is awaiting restoration of temporary settings")
                self._release_managed_control()
                await self._sync_cooling_humidity()
                return
            if self.state["pending_restore"]:
                await self._retry_pending()
            try:
                await self._expire_excess()
            except Exception as err:
                failure = err
            if self.state["pending_restore"]:
                try:
                    await self._sync_cooling_humidity(allow_start=False)
                except Exception as err:
                    failure = failure or err
                await self._pending_water_safety()
                if failure is not None:
                    raise failure
                return
            if failure is None:
                try:
                    await self._sync_cooling_humidity()
                    if self.state["managed_heating"]:
                        if self.state["heating_preset"] == "pv_charge":
                            if self._inside is None:
                                await self._stop_charge("heating")
                                self.state.update(heating_preset="normal", managed_heating=False)
                                self._clear_excess_time("heating")
                                self._hydrate()
                                self.state["control_warning"] = (
                                    "Inside temperature is unavailable; heating Excess Energy cancelled and native settings restored"
                                )
                                await self._save()
                            else:
                                ceiling = _number(self.settings.get("max_heating_temperature"))
                                if ceiling is None:
                                    raise ValueError("The room charging ceiling is missing")
                                active = self.state["charge_active"]["heating"]
                                if active and self._inside >= ceiling:
                                    await self._pause_heating_charge()
                                    await self._write(
                                        "comfort_wheel", self._heating_charge_target()
                                    )
                                elif (
                                    active or self._inside <= ceiling - self.settings["hysteresis"]
                                ):
                                    await self._charge_heating()
                                else:
                                    # A selected preset still displays its
                                    # target while charging is temperature-paused.
                                    await self._pause_heating_charge()
                                    await self._write(
                                        "comfort_wheel", self._heating_charge_target()
                                    )
                        else:
                            try:
                                await self._normal_heating()
                            except Exception:
                                if "heating_profile" in self.state["overrides"]:
                                    await self._restore_group("heating_profile")
                                raise
                except Exception as err:
                    failure = err
            if self.state["pending_restore"]:
                try:
                    await self._sync_cooling_humidity(allow_start=False)
                except Exception as err:
                    failure = failure or err
                await self._pending_water_safety()
                if failure is not None:
                    raise failure
                return
            if self.state["managed_hot_water"] and self.state["hot_water_mode"] == "evening":
                await self._apply_evening_water()
            if self.state["managed_hot_water"] and self.state["hot_water_mode"] in {
                "manual_on",
                "energy_excess",
            }:
                energy_excess = self.state["hot_water_mode"] == "energy_excess"
                fixed_boost = self.state["boost_once"] or self.state["boost_enabled"]
                stop, start, margin = self._water_limits(once=fixed_boost)
                temperature = self._water_temperature()
                native_unavailable = energy_excess and not self._native_boost_available()
                if fixed_boost and (temperature is None or native_unavailable):
                    previous = self.state["boost_previous_mode"] or "auto"
                    previous_managed = self.state["boost_previous_managed"]
                    if energy_excess:
                        previous = "off" if previous == "off" else "auto"
                        await self._exit_energy_excess(previous)
                    else:
                        await self._stop_charge("hot_water")
                    self.state.update(
                        hot_water_mode=previous,
                        managed_hot_water=previous_managed,
                        boost_enabled=False,
                        boost_once=False,
                        boost_previous_mode=None,
                        boost_previous_managed=False,
                        hot_water_exit_mode=None,
                        native_boost_coupled=False,
                        control_warning=(
                            "Tank temperature or native Boost control is unavailable; hot-water boost cancelled "
                            "and original settings restored"
                        ),
                    )
                    self._clear_excess_time("hot_water")
                    await self._save()
                    if failure is not None:
                        raise failure
                    return
                restart_temperature = self._water_start_temperature()
                active = self.state["charge_active"]["hot_water"]
                explicit_profile = (
                    not fixed_boost and self.settings.get("hot_water_boost_start") is not None
                )
                restart = start if explicit_profile else stop - margin
                eligible_temperature = restart_temperature if explicit_profile else temperature
                if (
                    (active or explicit_profile or self.state["boost_enabled"])
                    and temperature is not None
                    and temperature >= stop
                ):
                    if self.state["boost_enabled"] or (
                        explicit_profile and not self.state["boost_once"]
                    ):
                        await self._pause_water_charge()
                        if fixed_boost:
                            await self._water_thresholds(start, stop)
                    else:
                        await self._stop_charge("hot_water")
                    if self.state["boost_once"]:
                        previous = self.state["boost_previous_mode"] or "auto"
                        self.state.update(
                            hot_water_mode=previous, boost_once=False, boost_previous_mode=None
                        )
                        self.state["managed_hot_water"] = self.state["boost_previous_managed"]
                        self.state["boost_previous_managed"] = False
                        await self._save()
                else:
                    if fixed_boost and "hot_water" in self.state["overrides"]:
                        # A changed gap updates native thresholds even while
                        # paused without rebasing recovery values or timers.
                        await self._water_thresholds(start, stop)
                    if active or (
                        (eligible_temperature is None or eligible_temperature <= restart)
                        and self._water_start_route(start)
                    ):
                        await self._charge_water()
            await self._protect_sg_channels()
            if failure is not None:
                raise failure
        except Exception as err:
            self.state["control_warning"] = str(err)
            await self._save()
            raise
