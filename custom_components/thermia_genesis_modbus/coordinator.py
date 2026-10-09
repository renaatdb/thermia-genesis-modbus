"""Polling, external temperature fallback and durable control state."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from copy import deepcopy

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from modbus_connection import ModbusError

from .const import DOMAIN, SCAN_INTERVAL, normalize_options
from .control import ControlEngine
from .device import OPTIONAL_WRITES, ThermiaDevice
from .humidity import HumiditySample, humidity_sample
from .native_settings import NATIVE_SETTINGS, validate_native_setting
from .registers import ACTIVE_DEMAND_BITS, STATUS_BY_CODE
from .temperature import (
    TemperatureSample,
    sensor_timeout_seconds,
    temperature_sample,
    valid_temperature,
)

_LOGGER = logging.getLogger(__name__)


class ThermiaCoordinator(DataUpdateCoordinator):
    """One poll and one serialized controller for a pump."""

    def __init__(self, hass, entry, device):
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL
        )
        self.entry = entry
        self.device = device
        self._values = {}
        self._lock = asyncio.Lock()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self._recover_pending = True
        self._unsub_sensors = None
        self._last_outdoor_write = 0.0
        self._pending_options = None
        self._stopping = False
        self.engine = ControlEngine(
            self.value, self._async_write, self._async_save, normalize_options(entry.options)
        )

    async def async_load(self):
        saved = await self._store.async_load()
        if saved:
            state = saved.get("control", {})
            # Normalize each source before merging. A legacy entry still
            # carrying 900 must not reintroduce its automatic expiry after a
            # version-3 settings snapshot has already been saved.
            settings = normalize_options(saved.get("settings", {}))
            entry_options = normalize_options(self.entry.options)
            settings.update(
                {
                    key: entry_options[key]
                    for key in self.entry.options
                    if key in entry_options
                    and not (
                        key in {"max_start_temperature", "max_hot_water_temperature"}
                        and self.entry.options[key] is None
                    )
                }
            )
            self.engine = ControlEngine(
                self.value, self._async_write, self._async_save, settings, state
            )

    async def _async_save(self, state):
        await self._store.async_save(deepcopy({"control": state, "settings": self.engine.settings}))

    def value(self, key):
        return self._values.get(key)

    def _external_sample(self, setting, *, inside=False):
        entity_id = self.engine.settings.get(setting)
        if not entity_id:
            return TemperatureSample(None, "not_selected")
        return temperature_sample(
            self.hass.states.get(entity_id),
            dt_util.utcnow(),
            self.engine.settings.get("sensor_timeout", 0),
            inside=inside,
        )

    def _external(self, setting, *, inside=False):
        return self._external_sample(setting, inside=inside).value

    def _humidity_sample(self):
        entity_id = self.engine.settings.get("inside_humidity_sensor")
        if not entity_id:
            return HumiditySample(None, "not_selected")
        return humidity_sample(
            self.hass.states.get(entity_id),
            dt_util.utcnow(),
            self.engine.settings.get("sensor_timeout", 0),
        )

    @property
    def effective_inside_humidity(self):
        return self._humidity_sample().value

    def humidity_source_details(self):
        sample = self._humidity_sample()
        entity_id = self.engine.settings.get("inside_humidity_sensor") or None
        selected = self.hass.states.get(entity_id) if entity_id else None
        return {
            "configured_sensor": entity_id,
            "selected_sensor": entity_id,
            "selected_sensor_state": selected.state if selected else None,
            "selected_sensor_unit": selected.attributes.get("unit_of_measurement")
            if selected
            else None,
            "external_value": sample.value,
            "external_unit": sample.unit,
            "external_status": sample.reason,
            "external_report_age_seconds": round(sample.age_seconds, 1)
            if sample.age_seconds is not None
            else None,
            "freshness_limit_seconds": sensor_timeout_seconds(
                self.engine.settings.get("sensor_timeout", 0)
            ),
            "controller_available": self.last_update_success,
        }

    def _refresh_measurements(self):
        self.engine.update_measurements(
            self.effective_inside, self.effective_outside, self.effective_inside_humidity
        )

    def temperature_source_info(self, location):
        """Explain source selection without including extra private entity IDs."""
        inside = location == "inside"
        sample = self._external_sample(f"{location}_sensor", inside=inside)
        native = valid_temperature(
            self.value("indoor_temperature" if inside else "outdoor_temperature"),
            inside=inside,
        )
        if inside and self.value("room_sensor_alarm") is True:
            fallback_status = "room_sensor_alarm"
        else:
            fallback_status = "valid" if native is not None else "unavailable"
        return {
            "external_status": sample.reason,
            "external_unit": sample.unit,
            "external_report_age_seconds": (
                round(sample.age_seconds, 1) if sample.age_seconds is not None else None
            ),
            "freshness_limit_seconds": sensor_timeout_seconds(
                self.engine.settings.get("sensor_timeout", 0)
            ),
            "fallback_status": fallback_status,
        }

    def temperature_source_details(self, location):
        """Local entity attributes, including the configured sensor for review.

        Downloaded diagnostics use temperature_source_info instead, keeping
        these configured entity IDs out of that export.
        """
        entity_id = self.engine.settings.get(f"{location}_sensor") or None
        state = self.hass.states.get(entity_id) if entity_id else None
        return {
            **self.temperature_source_info(location),
            "selected_sensor": entity_id,
            "selected_sensor_state": state.state if state is not None else None,
            "selected_sensor_unit": (
                state.attributes.get("unit_of_measurement") if state is not None else None
            ),
            "effective_temperature": getattr(self, f"effective_{location}"),
            "thermia_temperature": self.value(
                "indoor_temperature" if location == "inside" else "outdoor_temperature"
            ),
            "controller_available": self.last_update_success,
        }

    @property
    def effective_inside(self):
        external = self._external("inside_sensor", inside=True)
        if external is not None:
            return external
        if self.value("room_sensor_alarm") is True:
            return None
        return valid_temperature(self.value("indoor_temperature"), inside=True)

    @property
    def effective_outside(self):
        external = self._external("outside_sensor")
        return (
            external
            if external is not None
            else valid_temperature(self.value("outdoor_temperature"))
        )

    @property
    def inside_source(self):
        if self._external("inside_sensor", inside=True) is not None:
            return self.engine.settings["inside_sensor"]
        return "Thermia room sensor" if self.effective_inside is not None else "Unavailable"

    @property
    def outside_source(self):
        if self._external("outside_sensor") is not None:
            return self.engine.settings["outside_sensor"]
        if self.effective_outside is None:
            return "Unavailable"
        if self.value("outdoor_source") == 0:
            return "Thermia physical outside sensor"
        return "Thermia BMS source"

    @property
    def status(self):
        value = self.value("main_demand")
        return STATUS_BY_CODE.get(value, f"Unknown ({value})") if value is not None else None

    @property
    def active_demands(self):
        flags = self.value("active_demand_flags")
        if flags is not None:
            return [name for bit, name in ACTIVE_DEMAND_BITS.items() if int(flags) & (1 << bit)]
        return [self.status] if self.status is not None else []

    @property
    def alarm_classes(self):
        return [
            name for name in ("A", "B", "C") if self.value(f"alarm_class_{name.lower()}") is True
        ]

    async def _async_write(self, key, value):
        # An unused BMS outdoor input may contain the missing-value sentinel.
        # It can be initialized, but only if the protocol read itself succeeded.
        initialize_bms = (
            key == "bms_outdoor_temperature"
            and key in self.device.specs
            and key not in self.device.unsupported
        )
        if (
            self.value(key) is None
            and not initialize_bms
            and key in self.device.specs
            and key not in self.device.unsupported
        ):
            # A failed confirmation invalidates the cached value after a write
            # may have reached the pump. Read again so a recovery write can
            # proceed; missing sensors and unsupported registers stay blocked.
            self._values[key] = await self.device.async_read_value(key)
        if self.value(key) is None and not initialize_bms:
            raise ValueError(f"The controller has not supplied a valid {key} register")
        try:
            result = await self.device.async_write(key, value)
        except (ValueError, RuntimeError, OSError, ModbusError):
            self._values[key] = self.device.values.get(key)
            raise
        self._values[key] = result

    async def _async_outdoor_feed(self):
        """Publish fresh samples periodically, switching to physical sensing on loss."""
        selected = self.engine.settings.get("outside_sensor")
        snapshot = self.engine.state.get("outdoor_source_snapshot")
        if not selected:
            if snapshot is not None:
                await self._async_write("outdoor_source", snapshot)
                self.engine.state.pop("outdoor_source_snapshot", None)
                await self._async_save(self.engine.state)
                self._values["outdoor_temperature"] = await self.device.async_read_value(
                    "outdoor_temperature"
                )
            return
        temperature = self._external("outside_sensor")
        if temperature is None:
            if self.value("outdoor_source") == 1:
                await self._async_write("outdoor_source", 0)
                self._values["outdoor_temperature"] = await self.device.async_read_value(
                    "outdoor_temperature"
                )
            return
        if (
            self.value("outdoor_source") is None
            or "bms_outdoor_temperature" not in self.device.specs
            or "bms_outdoor_temperature" in self.device.unsupported
        ):
            self.engine.state["control_warning"] = (
                "External outside input is unsupported on this controller"
            )
            return
        if snapshot is None:
            self.engine.state["outdoor_source_snapshot"] = self.value("outdoor_source")
            await self._async_save(self.engine.state)
        now = time.monotonic()
        changed = self.value("bms_outdoor_temperature") != round(temperature, 2)
        if changed or self.value("outdoor_source") != 1 or now - self._last_outdoor_write >= 300:
            await self._async_write("bms_outdoor_temperature", round(temperature, 2))
            self._last_outdoor_write = now
        if self.value("outdoor_source") != 1:
            await self._async_write("outdoor_source", 1)
        self._values["outdoor_temperature"] = await self.device.async_read_value(
            "outdoor_temperature"
        )

    async def _async_update_data(self):
        async with self._lock:
            if self._stopping:
                return dict(self._values)
            self._refresh_measurements()
            # Poll failures must not turn an old cache into a successful update.
            try:
                self._values = await self.device.async_update()
                if self.value("main_demand") is None:
                    raise UpdateFailed(
                        "The Thermia operating-status register did not return a valid value"
                    )
            except (ModbusError, ValueError, OSError) as error:
                raise UpdateFailed(str(error)) from error
            try:
                if not await self._async_restore_configuration():
                    return dict(self._values)
                if self._recover_pending:
                    self._recover_pending = not await self.engine.recover()
                    if self._recover_pending:
                        return dict(self._values)
                if not await self.async_apply_pending_options():
                    return dict(self._values)
                await self._async_outdoor_feed()
                await self.engine.tick(
                    self.effective_inside, self.effective_outside, self.effective_inside_humidity
                )
            except ModbusError as error:
                raise UpdateFailed(str(error)) from error
            except (ValueError, RuntimeError, OSError) as error:
                # Keep valid monitoring data visible when an optional control fails.
                self.engine.state["control_warning"] = str(error)
                _LOGGER.warning("Thermia control could not complete: %s", error)
            return dict(self._values)

    async def _async_expire_configuration_backup(self, backup):
        """Replace expired charging values in a rollback before touching hardware.

        Evaluate the saved policy against an in-memory device. This retains the
        engine's shared Smart Grid and threshold ordering rules, while the outer
        recovery journal stays durable until physical restoration is confirmed.
        """
        now = self.engine._clock()
        values = dict(backup["native"])
        changed_values = {}

        def read(key):
            return values.get(key, self.value(key))

        async def write(key, value):
            values[key] = value
            changed_values[key] = value

        async def save(state):
            pass

        preview = ControlEngine(
            read, write, save, backup["settings"], backup["state"], now=lambda: now
        )
        preview.update_measurements(
            self.effective_inside, self.effective_outside, self.effective_inside_humidity
        )
        await preview._refresh_excess_deadlines()
        expired = []
        for group in ("heating", "hot_water"):
            deadline = preview.state[f"{group}_excess_deadline"]
            if (
                isinstance(deadline, (int, float))
                and not isinstance(deadline, bool)
                and math.isfinite(deadline)
                and now >= deadline
            ):
                expired.append(group)
        low_deadline = preview.state["hot_water_low_deadline"]
        low_expired = (
            isinstance(low_deadline, (int, float))
            and not isinstance(low_deadline, bool)
            and math.isfinite(low_deadline)
            and now >= low_deadline
        )
        if low_expired:
            expired.append("hot_water")
        restart_groups = []
        if self._recover_pending:
            if preview.state["heating_preset"] in {"pv_charge", "low", "vacation"} or {
                "heating",
                "heating_profile",
            }.intersection(preview.state["overrides"]):
                restart_groups.append("heating")
            if (
                preview.state["hot_water_mode"] in {"manual_on", "energy_excess", "evening"}
                or "hot_water" in preview.state["overrides"]
            ):
                restart_groups.append("hot_water")
                low_expired = low_expired or preview.state["hot_water_mode"] == "evening"
            expired = sorted(set(expired) | set(restart_groups))
        if not expired:
            return None
        error = None
        try:
            if restart_groups:
                await preview.recover()
            else:
                await preview._expire_excess()
        except (ValueError, RuntimeError, OSError, ModbusError) as failure:
            # One function can finish expiry even if the other cannot resume
            # its normal room policy. Preserve every safe restoration planned.
            error = failure
        backup["native"].update(changed_values)
        backup["state"] = deepcopy(preview.state)
        backup["thermal_action"] = preview._thermal_action
        backup["expired_excess_groups"] = sorted(
            set(backup.get("expired_excess_groups", [])) | set(expired)
        )
        backup["expired_low_mode"] = bool(backup.get("expired_low_mode") or low_expired)
        await self._async_save(self.engine.state)
        warning = self.engine.state.get("control_warning")
        self.engine.state = deepcopy(backup["state"])
        self.engine.state["configuration_restore"] = backup
        self.engine.state["control_warning"] = warning
        self.engine.settings = dict(backup["settings"])
        self.engine._thermal_action = backup["thermal_action"]
        await self._async_save(self.engine.state)
        return error

    async def _async_restore_expired_functions(self, backup):
        """Restore each expired demand before an unrelated rollback can fail."""
        native = backup["native"]
        groups = backup.get("expired_excess_groups", [])
        if not groups:
            return
        failure = None

        async def refresh(keys):
            for key in keys:
                if key in native:
                    self._values[key] = await self.device.async_read_value(key)

        if "smart_grid_request" in native:
            try:
                await refresh(("smart_grid_request",))
                await self.engine._write("smart_grid_request", native["smart_grid_request"])
            except (ValueError, RuntimeError, OSError, ModbusError) as error:
                failure = error
        if "hot_water" in groups:
            try:
                if "hot_water_boost" in native:
                    await refresh(("hot_water_boost",))
                    await self.engine._write("hot_water_boost", False)
                if {"hot_water_start", "hot_water_stop"} <= native.keys():
                    await refresh(("hot_water_start", "hot_water_stop"))
                    await self.engine._water_thresholds(
                        native["hot_water_start"],
                        native["hot_water_stop"],
                        allow_equal=native["hot_water_start"] == native["hot_water_stop"] == 60,
                    )
                if "hot_water_enabled" in native:
                    await refresh(("hot_water_enabled",))
                    await self.engine._write("hot_water_enabled", native["hot_water_enabled"])
            except (ValueError, RuntimeError, OSError, ModbusError) as error:
                failure = failure or error
                # A failed Boost confirmation must not leave a boosted demand
                # enabled. Keep the intended Auto permission in the journal.
                if "hot_water_enabled" in native:
                    try:
                        await refresh(("hot_water_enabled",))
                        await self.engine._write("hot_water_enabled", False)
                    except (ValueError, RuntimeError, OSError, ModbusError):
                        pass
        if "heating" in groups:
            try:
                space = ("heating_enabled", "passive_cooling_enabled")
                await refresh(space)
                await self.engine._space_requests(False, False)
                if "fixed_supply_enabled" in native:
                    await refresh(("fixed_supply_enabled",))
                    await self.engine._write("fixed_supply_enabled", False)
                if "fixed_supply_target" in native:
                    await refresh(("fixed_supply_target",))
                    await self.engine._write("fixed_supply_target", native["fixed_supply_target"])
                if "fixed_supply_enabled" in native:
                    await self.engine._write("fixed_supply_enabled", native["fixed_supply_enabled"])
                if "heating_season_stop" in native:
                    await refresh(("heating_season_stop",))
                    await self.engine._write("heating_season_stop", native["heating_season_stop"])
                if "comfort_wheel" in native:
                    await refresh(("comfort_wheel",))
                    await self.engine._write("comfort_wheel", native["comfort_wheel"])
                if all(key in native for key in space):
                    await refresh(space)
                    if any(self.value(key) != native[key] for key in space):
                        await self.engine._space_requests(False, False)
                        for key in space:
                            # Both native permissions may be enabled; the
                            # controller chooses which demand to serve.
                            await self.engine._write(key, native[key])
            except (ValueError, RuntimeError, OSError, ModbusError) as error:
                failure = failure or error
                for key in ("heating_enabled", "passive_cooling_enabled"):
                    if key in native:
                        try:
                            await refresh((key,))
                            await self.engine._write(key, False)
                        except (ValueError, RuntimeError, OSError, ModbusError):
                            pass
        if failure is not None:
            raise failure

    async def _async_restore_configuration(self):
        """Retry a failed settings save before allowing further commands."""
        backup = self.engine.state.get("configuration_restore")
        if backup is None:
            return True
        backup["settings"].pop("charge_supply_temperature", None)
        backup["state"].get("settings", {}).pop("charge_supply_temperature", None)
        native = backup["native"]
        try:
            expiry_error = await self._async_expire_configuration_backup(backup)
            warning = self.engine.state.get("control_warning")
            self.engine.settings = dict(backup["settings"])
            self.engine.state = deepcopy(backup["state"])
            self.engine.state["configuration_restore"] = backup
            self.engine.state["control_warning"] = warning
            self._refresh_measurements()
            # Before restoring any colder original cooling-water target,
            # remove the permission. A later On passes the restored guard.
            if "passive_cooling_supply_target" in native:
                await self.engine.write("passive_cooling_enabled", False)
            await self._async_restore_expired_functions(backup)
            # The failing request may already have changed the device. Refresh
            # before equality checks, after expired demands receive priority.
            for key in native:
                self._values[key] = await self.device.async_read_value(key)
            await self.engine._write_native_settings(
                {
                    key: value
                    for key, value in native.items()
                    if key in NATIVE_SETTINGS and self.value(key) != value
                }
            )
            # Remove a global boost first. Restore threshold pairs in an order
            # that stays valid at every intermediate register write.
            if "smart_grid_request" in native:
                await self.engine._write("smart_grid_request", native["smart_grid_request"])
            if {"hot_water_start", "hot_water_stop"} <= native.keys():
                await self.engine._water_thresholds(
                    native["hot_water_start"],
                    native["hot_water_stop"],
                    allow_equal=native["hot_water_start"] == native["hot_water_stop"] == 60,
                )
            if "fixed_supply_target" in native:
                if self.value("fixed_supply_target") != native["fixed_supply_target"]:
                    await self.engine._write("fixed_supply_enabled", False)
                await self.engine._write("fixed_supply_target", native["fixed_supply_target"])
            if "comfort_wheel" in native:
                await self.engine._write("comfort_wheel", native["comfort_wheel"])
            # On/off requests are restored after clearing their counterparts.
            space = ("heating_enabled", "passive_cooling_enabled")
            if all(key in native for key in space) and any(
                self.value(key) != native[key] for key in space
            ):
                await self.engine._space_requests(False, False)
            for key, value in native.items():
                if (
                    key
                    not in {
                        "smart_grid_request",
                        "hot_water_start",
                        "hot_water_stop",
                        "fixed_supply_target",
                        "comfort_wheel",
                    }
                    and key not in NATIVE_SETTINGS
                ):
                    await self.engine._write(key, value)
            if expiry_error is not None:
                raise expiry_error
        except (ValueError, RuntimeError, OSError, ModbusError) as error:
            self.engine.state["control_warning"] = f"Restoring configuration is pending: {error}"
            try:
                await self._async_save(self.engine.state)
            except OSError as save_error:
                _LOGGER.warning("Thermia configuration recovery could not be saved: %s", save_error)
            return False
        warning = self.engine.state.get("control_warning")
        self.engine.state.pop("configuration_restore", None)
        self.engine.settings = dict(backup["settings"])
        self.engine._thermal_action = backup.get("thermal_action", "idle")
        self.engine.state["control_warning"] = warning
        try:
            await self._async_save(self.engine.state)
        except BaseException as error:
            # The previous durable record still contains the rollback. Keep
            # it in memory too until the restored policy can be saved.
            self.engine.state["configuration_restore"] = backup
            if isinstance(error, OSError):
                self.engine.state["control_warning"] = (
                    f"Restoring configuration is pending: {error}"
                )
                _LOGGER.warning("Thermia configuration recovery could not be saved: %s", error)
                return False
            raise
        return True

    async def async_configure_controls(self, options, *, control_changes=None):
        """Save preferences and verify only explicitly changed controls.

        OptionsFlow supplies changes compared with the originally displayed
        values, so saving sensor preferences does not reapply old pump flags.
        """
        changes = {
            key: value for key, value in (control_changes or {}).items() if value is not None
        }
        if self._stopping:
            raise HomeAssistantError("Home Assistant is stopping")
        async with self._lock:
            old_settings = dict(self.engine.settings)
            try:
                if not await self._async_restore_configuration():
                    raise ValueError(
                        "Previous configuration must be restored before saving new settings"
                    )
                if not await self.async_apply_pending_options():
                    raise ValueError(
                        "Previous settings must be restored before applying configuration"
                    )
                old_settings = dict(self.engine.settings)
                settings = normalize_options(options)
                for key in (
                    "control_mode",
                    "max_hot_water_temperature",
                    "max_start_temperature",
                    "max_heating_temperature",
                    "hysteresis",
                    "hot_water_hysteresis",
                    "hot_water_evening_start",
                    "hot_water_evening_stop",
                    "heating_excess_hours",
                    "heating_excess_heat_stop_offset",
                    "hot_water_excess_hours",
                    "hot_water_low_hours",
                    "heating_low_offset",
                    "heating_vacation_temperature",
                    "heating_vacation_cooling_offset",
                    "cooling_humidity_limit",
                    "cooling_vacation_humidity_limit",
                    "cooling_dew_point_margin",
                    "cooling_humidity_enabled",
                    "smart_grid_mode",
                    "enable_undocumented_controls",
                ):
                    if settings[key] is not None or key in {
                        "cooling_humidity_limit",
                        "cooling_vacation_humidity_limit",
                    }:
                        self.engine._validate_setting(key, settings[key])
                    if key in {
                        "heating_excess_hours",
                        "hot_water_excess_hours",
                        "hot_water_low_hours",
                    } and settings[key] != old_settings.get(key):
                        self.engine._validate_new_duration(key, settings[key])
                self.engine._evening_water_limits(settings, require_caps=False)
                if self.engine.state["heating_preset"] == "pv_charge":
                    self.engine._heating_charge_heat_stop(settings)
                if self.engine.state["heating_mode"] == "heat_cool":
                    self.engine.effective_heating_range(preset="vacation", settings=settings)
                if (
                    self.engine.state["hot_water_mode"] == "energy_excess"
                    and settings["hot_water_hysteresis"] != old_settings["hot_water_hysteresis"]
                    and all(
                        settings[key] == old_settings[key]
                        for key in ("max_hot_water_temperature", "max_start_temperature")
                    )
                ):
                    self.engine._water_limits(once=True, settings=settings)
                allowed = {
                    "hot_water_start",
                    "hot_water_target",
                    "heating_enabled",
                    "hot_water_enabled",
                    "passive_cooling_enabled",
                    "auxiliary_heater_enabled",
                    "anti_legionella_enabled",
                    "native_boost_enabled",
                } | NATIVE_SETTINGS.keys()
                if changes.keys() - allowed:
                    raise ValueError("An unsupported configuration control was supplied")
                switch_commands = {
                    "heating_enabled": ("native_heating_enabled", ("heating_enabled",)),
                    "passive_cooling_enabled": (
                        "native_cooling_enabled",
                        ("passive_cooling_enabled",),
                    ),
                    "hot_water_enabled": ("hot_water_enabled", ("hot_water_enabled",)),
                    "native_boost_enabled": ("native_hot_water_boost", ("hot_water_boost",)),
                    "auxiliary_heater_enabled": ("auxiliary_heater", ("immersion_heater",)),
                    "anti_legionella_enabled": ("anti_legionella", ("anti_legionella_enabled",)),
                }
                native_changes = changes
                if native_changes and not self.last_update_success:
                    raise ValueError("Connect the heat pump before changing its settings")
                for key, value in native_changes.items():
                    if key in switch_commands:
                        if not isinstance(value, bool):
                            raise ValueError("Switch values must be on or off")
                        if any(
                            self.value(register) is None for register in switch_commands[key][1]
                        ):
                            raise ValueError(f"{key} is unavailable on this controller")
                for key in ("anti_legionella_enabled", "native_boost_enabled"):
                    if key in changes:
                        register = switch_commands[key][1][0]
                        if self.value(register) not in (0, 1):
                            raise ValueError(f"{key} is unavailable on this controller")
                native_settings = {}
                for key, value in changes.items():
                    if key not in NATIVE_SETTINGS:
                        continue
                    native_settings[key] = validate_native_setting(key, value)
                    reported = self.value(key)
                    if (
                        key not in self.device.specs
                        or key in self.device.unsupported
                        or isinstance(reported, bool)
                        or not isinstance(reported, (int, float))
                        or not math.isfinite(reported)
                    ):
                        raise ValueError(
                            f"{NATIVE_SETTINGS[key].label} is unavailable on this controller"
                        )
                supply_keys = ("min_supply_temperature", "max_supply_temperature")
                if any(key in native_settings for key in supply_keys):
                    pair = [native_settings.get(key, self.value(key)) for key in supply_keys]
                    if (
                        any(
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(value)
                            for value in pair
                        )
                        or pair[0] > pair[1]
                    ):
                        raise ValueError(
                            "Heating supply minimum must not exceed maximum; both settings must be readable"
                        )
                if {"hot_water_start", "hot_water_target"}.intersection(changes):
                    start = changes.get("hot_water_start", self.engine.state["hot_water_start"])
                    stop = changes.get("hot_water_target", self.engine.state["hot_water_target"])
                    if start is None or stop is None or not 30 <= start < stop <= 60:
                        raise ValueError(
                            "Normal hot-water start must be below stop, within 30–60°C"
                        )
                    for value, cap in (
                        (start, settings["max_start_temperature"]),
                        (stop, settings["max_hot_water_temperature"]),
                    ):
                        if cap is None or value > cap:
                            raise ValueError(
                                "Confirm the pump's hot-water maxima before changing normal temperatures"
                            )
                critical = {
                    "control_mode",
                    "enable_undocumented_controls",
                    "smart_grid_mode",
                    "max_hot_water_temperature",
                    "max_start_temperature",
                    "hot_water_boost_start",
                    "hot_water_boost_stop",
                }
                critical_changed = any(settings[key] != old_settings.get(key) for key in critical)
                apply_evening = (
                    self.engine.state["hot_water_mode"] == "evening"
                    and any(
                        settings[key] != old_settings.get(key)
                        for key in ("hot_water_evening_start", "hot_water_evening_stop")
                    )
                    and not critical_changed
                    and changes.get("hot_water_enabled") is not False
                    and changes.get("native_boost_enabled") is not True
                )
                if apply_evening:
                    self.engine._evening_water_limits(settings)
                    if not self.last_update_success:
                        raise ValueError(
                            "Connect the heat pump before changing the active Low Mode temperatures"
                        )
                apply_heating = self.engine.state["heating_preset"] in {"low", "vacation"} and any(
                    settings[key] != old_settings.get(key)
                    for key in (
                        "heating_low_offset",
                        "heating_vacation_temperature",
                        "heating_vacation_cooling_offset",
                    )
                )
                apply_humidity = any(
                    settings[key] != old_settings.get(key)
                    for key in (
                        "inside_humidity_sensor",
                        "cooling_humidity_enabled",
                        "cooling_humidity_limit",
                        "cooling_vacation_humidity_limit",
                        "cooling_dew_point_margin",
                    )
                ) and (
                    bool(self.value("passive_cooling_enabled"))
                    or self.engine.state["cooling_humidity_requested"]
                    or "cooling_humidity" in self.engine.state["overrides"]
                )
                if (apply_heating or apply_humidity) and not self.last_update_success:
                    raise ValueError(
                        "Connect the heat pump before changing its active temperature protection"
                    )
                if critical_changed and settings["control_mode"] == old_settings["control_mode"]:
                    if not await self.engine.shutdown():
                        raise ValueError(
                            "Charging settings still need restoring; save again when the pump is reachable"
                        )
                await self.engine.set_control_mode(settings["control_mode"])
                if native_changes or apply_evening or apply_heating or apply_humidity:
                    keys = (
                        "smart_grid_request",
                        "hot_water_start",
                        "hot_water_stop",
                        "fixed_supply_target",
                        "fixed_supply_enabled",
                        "heating_enabled",
                        "passive_cooling_enabled",
                        "comfort_wheel",
                        "hot_water_enabled",
                        "immersion_heater",
                        "anti_legionella_enabled",
                        "hot_water_boost",
                    ) + tuple(NATIVE_SETTINGS)
                    backup = {
                        "native": {
                            key: self.value(key)
                            for key in keys
                            if self.value(key) is not None
                            and key in self.device.specs
                            and (key not in OPTIONAL_WRITES or self.value(key) in (0, 1))
                            and (
                                key not in NATIVE_SETTINGS
                                or (
                                    not isinstance(self.value(key), bool)
                                    and isinstance(self.value(key), (int, float))
                                    and math.isfinite(self.value(key))
                                )
                            )
                        },
                        "state": deepcopy(self.engine.state),
                        "settings": old_settings,
                        "thermal_action": self.engine._thermal_action,
                    }
                    self.engine.state["configuration_restore"] = backup
                    await self._async_save(self.engine.state)
                self.engine.settings = settings
                self._refresh_measurements()
                await self.engine._expire_excess()
                if apply_evening and self.engine.state["hot_water_mode"] == "evening":
                    # Configure edits apply the active profile immediately;
                    # only an explicit preset selection restarts its timer.
                    await self.engine._apply_evening_water()
                if apply_heating and self.engine.state["heating_preset"] in {"low", "vacation"}:
                    await self.engine._apply_heating_profile(self.engine.state["heating_preset"])
                if apply_humidity:
                    await self.engine._sync_cooling_humidity()
                    if self.engine.state["managed_heating"]:
                        await self.engine._normal_heating(self.engine.state["heating_mode"])
                if native_settings:
                    await self.engine.command("native_settings", native_settings)
                if {"hot_water_start", "hot_water_target"}.intersection(changes):
                    await self.engine.command("hot_water_range", (start, stop))
                # Independent function permissions do not clear one another.
                for key, (command, _) in switch_commands.items():
                    if key in native_changes:
                        await self.engine.command(command, native_changes[key])
                restore_backup = self.engine.state.pop("configuration_restore", None)
                try:
                    await self._async_save(self.engine.state)
                except BaseException:
                    # A failed commit must retain the original native values
                    # even when every hardware write was already confirmed.
                    if restore_backup is not None:
                        self.engine.state["configuration_restore"] = restore_backup
                    raise
                self.async_listen_sensors()
            except (ValueError, RuntimeError, OSError, ModbusError) as error:
                self.engine.settings = old_settings
                self.engine.state["control_warning"] = str(error)
                if "configuration_restore" in self.engine.state:
                    await self._async_restore_configuration()
                try:
                    await self._async_save(self.engine.state)
                except OSError as save_error:
                    _LOGGER.warning(
                        "Thermia failed configuration could not be saved: %s", save_error
                    )
                raise HomeAssistantError(str(error)) from error

    async def async_apply_pending_options(self):
        """Apply configuration only after its old overrides are restored.

        Caller holds the lock. Failed restoration retains access to the old
        registers and is retried after the next successful poll.
        """
        options = self._pending_options
        if options is None:
            return True
        options = normalize_options(options)
        for key in ("cooling_humidity_limit", "cooling_vacation_humidity_limit"):
            self.engine._validate_setting(key, options[key])
        for key in ("heating_excess_hours", "hot_water_excess_hours", "hot_water_low_hours"):
            if options[key] != self.engine.settings.get(key):
                self.engine._validate_new_duration(key, options[key])
        critical = {
            "control_mode",
            "enable_undocumented_controls",
            "smart_grid_mode",
            "max_hot_water_temperature",
            "max_start_temperature",
            "hot_water_boost_start",
            "hot_water_boost_stop",
        }
        if options["control_mode"] == self.engine.settings["control_mode"] and any(
            options[key] != self.engine.settings[key] for key in critical
        ):
            if not await self.engine.shutdown():
                return False
        await self.engine.set_control_mode(options["control_mode"])
        if (
            bool(OPTIONAL_WRITES.intersection(self.device.specs))
            != options["enable_undocumented_controls"]
        ):
            self.device = ThermiaDevice(
                self.device.unit, undocumented=options["enable_undocumented_controls"]
            )
        self.engine.settings.update(options)
        self._refresh_measurements()
        await self.engine._expire_excess()
        if self.engine.state["heating_preset"] in {"low", "vacation"}:
            await self.engine._apply_heating_profile(self.engine.state["heating_preset"])
        await self.engine._sync_cooling_humidity()
        self._pending_options = None
        self.async_listen_sensors()
        await self._async_save(self.engine.state)
        return True

    def _mode_preset_selection(self):
        """Return the selected modes and presets visible on the thermostats."""
        state = self.engine.state
        heating_mode = state.get("heating_mode")
        return (
            heating_mode,
            state.get("heating_preset") if heating_mode != "off" else None,
            state.get("hot_water_mode"),
        )

    async def async_command(self, action, value=None):
        if self._stopping:
            raise HomeAssistantError("Home Assistant is stopping")
        if not self.last_update_success:
            raise HomeAssistantError("The heat pump is unavailable; a request cannot be confirmed")
        async with self._lock:
            previous_selection = self._mode_preset_selection()
            try:
                if not await self._async_restore_configuration():
                    raise ValueError("Previous configuration is awaiting restoration")
                if not await self.async_apply_pending_options():
                    raise ValueError(
                        "Previous settings must be restored before applying the new configuration"
                    )
                if (
                    action == "heating_preset"
                    and value == "pv_charge"
                    and self.effective_inside is None
                ):
                    raise ValueError(
                        "Excess Energy heating needs a valid inside sensor to enforce the charging ceiling"
                    )
                self._refresh_measurements()
                await self.engine.command(action, value)
                if action == "setting":
                    key, setting_value = value
                    normalized = normalize_options(self.entry.options)
                    # Migrate existing keys before marking this user edit as
                    # current. An explicitly chosen 65% must stay 65% when
                    # the options listener or a restart normalizes it again.
                    options = {
                        name: normalized[name] for name in self.entry.options if name in normalized
                    } | {
                        key: setting_value,
                        "configuration_version": normalized["configuration_version"],
                    }
                    # Legacy unset entry fields may coexist with explicit
                    # custom limits in runtime storage. Keep those effective
                    # limits when an unrelated DEV edit stamps current options.
                    for limit in ("max_start_temperature", "max_hot_water_temperature"):
                        if self.entry.options.get(limit) is None:
                            options[limit] = self.engine.settings[limit]
                    # A DEV edit must not replace a legacy fractional clock
                    # with defaults when the options listener runs afterward.
                    for duration in (
                        "heating_excess_hours",
                        "hot_water_excess_hours",
                        "hot_water_low_hours",
                    ):
                        options.setdefault(duration, self.engine.settings[duration])
                    self.hass.config_entries.async_update_entry(self.entry, options=options)
                await self.engine.tick(
                    self.effective_inside, self.effective_outside, self.effective_inside_humidity
                )
            except (ValueError, RuntimeError, OSError, ModbusError) as error:
                raise HomeAssistantError(str(error)) from error
            if self._mode_preset_selection() != previous_selection:
                # Refresh requests can share one debounce window. Publish each
                # committed selection before a later command can replace it.
                self.async_update_listeners()
        await self.async_request_refresh()

    def async_listen_sensors(self):
        if self._unsub_sensors:
            self._unsub_sensors()
            self._unsub_sensors = None
        entities = [
            self.engine.settings.get(key)
            for key in ("inside_sensor", "outside_sensor", "inside_humidity_sensor")
        ]
        entities = list(dict.fromkeys(entity for entity in entities if entity))
        if entities:

            @callback
            def changed(event):
                if not self._stopping:
                    self._refresh_measurements()
                    # Local sensor diagnostics must also update when repeated
                    # failed Modbus polls suppress coordinator notifications.
                    self.async_update_listeners()
                    self.hass.async_create_task(self.async_request_refresh())

            @callback
            def reported(event):
                # Recover immediately when an unchanged report makes a sensor
                # fresh again. Routine unchanged reports need no Modbus poll.
                timeout = sensor_timeout_seconds(self.engine.settings.get("sensor_timeout", 0))
                previous = event.data.get("old_last_reported")
                if timeout and previous is not None:
                    try:
                        age = (dt_util.utcnow() - previous).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        return
                    if age > timeout:
                        changed(event)

            remove_changed = async_track_state_change_event(self.hass, entities, changed)
            remove_reported = async_track_state_report_event(self.hass, entities, reported)

            def remove_listeners():
                remove_changed()
                remove_reported()

            self._unsub_sensors = remove_listeners

    async def async_stop(self, event):
        """Restore while Home Assistant still has a live Modbus connection."""
        await self.async_shutdown()

    async def async_shutdown(self):
        self._stopping = True
        if self._unsub_sensors:
            self._unsub_sensors()
            self._unsub_sensors = None
        async with self._lock:
            try:
                await self._async_restore_configuration()
                restored = await self.engine.shutdown()
                if "outdoor_source_snapshot" in self.engine.state:
                    await self._async_write(
                        "outdoor_source", self.engine.state["outdoor_source_snapshot"]
                    )
                    self.engine.state.pop("outdoor_source_snapshot")
                    await self._async_save(self.engine.state)
                if not restored:
                    _LOGGER.warning("Thermia restoration is pending until the next connection")
            except (ValueError, RuntimeError, ModbusError):
                _LOGGER.warning(
                    "Thermia settings could not be restored on shutdown; restoration remains saved for the next connection"
                )
