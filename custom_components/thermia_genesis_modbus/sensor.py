"""Read-only Thermia telemetry and temperature-source diagnostics."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from homeassistant.components.climate.const import HVACMode
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from .climate import ThermiaClimate, ThermiaHotWaterClimate
from .control_guide import control_guide_attributes
from .entity import ThermiaEntity
from .excess_time import excess_time_info, low_time_info
from .registers import REGISTERS, SMART_GRID_STATUS_BY_CODE, STATUS_BY_CODE, TEMPERATURE_POINTS
from .sensor_names import sensor_name
from .temperature import temperature_source_status


async def async_setup_entry(hass, entry, async_add_entities):
    """Expose every supported numeric register without making it writable."""
    coordinator = entry.runtime_data
    entities = [
        ThermiaRegisterSensor(coordinator, spec)
        for spec in REGISTERS
        if spec.space in ("input", "holding") and spec.key != "main_demand"
    ]
    entities.extend(
        (
            ThermiaStatusSensor(coordinator),
            ThermiaModePresetSensor(coordinator, "heating"),
            ThermiaModePresetSensor(coordinator, "hot_water"),
            ThermiaEffectiveTemperature(coordinator, "inside"),
            ThermiaEffectiveTemperature(coordinator, "outside"),
            ThermiaTemperatureSource(coordinator, "inside"),
            ThermiaTemperatureSource(coordinator, "outside"),
            ThermiaInsideHumidity(coordinator),
            ThermiaInsideDewPoint(coordinator),
            ThermiaCoolingHumidityProtection(coordinator),
            ThermiaCOPSensor(coordinator),
            ThermiaControlWarning(coordinator),
            ThermiaExcessTimeRemaining(coordinator, "heating"),
            ThermiaExcessTimeRemaining(coordinator, "hot_water"),
            ThermiaLowTimeRemaining(coordinator),
            ThermiaControlLogic(coordinator),
        )
    )
    async_add_entities(entities)


class ThermiaModePresetSensor(ThermiaEntity, SensorEntity):
    """Record selections as state changes rather than only climate attributes."""

    _attr_entity_category = None
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True
    _attr_native_unit_of_measurement = None
    _attr_state_class = None
    _attr_device_class = None

    def __init__(self, coordinator, function: str) -> None:
        self.function = function
        if function == "heating":
            self._attr_icon = "mdi:thermostat"
            view = ThermiaClimate
        elif function == "hot_water":
            self._attr_icon = "mdi:water-boiler"
            view = ThermiaHotWaterClimate
        else:
            raise ValueError("Select heating or hot_water")
        key = f"{function}_mode_and_preset"
        super().__init__(coordinator, key, sensor_name(key))
        # This presentation view is never added to HA, so it adds no listeners.
        self._climate_view = view(coordinator)

    def _selection(self) -> tuple[HVACMode | None, str | None]:
        state = self.coordinator.engine.state
        if self.function == "heating":
            mode = state.get("heating_mode")
            preset = state.get("heating_preset")
            if not isinstance(mode, str) or mode not in {"off", "heat", "cool", "heat_cool"}:
                return None, None
            if mode != "off" and (
                not isinstance(preset, str)
                or preset not in {"normal", "pv_charge", "low", "vacation"}
            ):
                return None, None
        else:
            mode = state.get("hot_water_mode")
            if not isinstance(mode, str) or mode not in {
                "off",
                "auto",
                "energy_excess",
                "evening",
                "manual_on",
            }:
                return None, None
        hvac_mode = self._climate_view.hvac_mode
        preset_mode = self._climate_view.preset_mode
        if hvac_mode != HVACMode.OFF and preset_mode not in self._climate_view.preset_modes:
            return None, None
        return hvac_mode, preset_mode

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        mode, preset = self._selection()
        if mode is None:
            return None
        label = self._mode_label(mode)
        return label if preset is None else f"{label} · {preset}"

    @staticmethod
    def _mode_label(mode: HVACMode | None) -> str | None:
        return {
            HVACMode.OFF: "Off",
            HVACMode.HEAT: "Heat",
            HVACMode.COOL: "Cool",
            HVACMode.HEAT_COOL: "Heat/Cool",
            HVACMode.AUTO: "Auto",
        }.get(mode)

    @property
    def extra_state_attributes(self):
        mode, preset = self._selection()
        return {
            "mode": self._mode_label(mode),
            "hvac_mode": mode.value if mode is not None else None,
            "preset": preset,
            "controller_available": bool(self.coordinator.last_update_success),
        }


class ThermiaRegisterSensor(ThermiaEntity, SensorEntity):
    """One cached register value, with unavailable values kept distinct from zero."""

    def __init__(self, coordinator, spec) -> None:
        super().__init__(coordinator, spec.key, sensor_name(spec.key))
        self.spec = spec
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_entity_registry_enabled_default = spec.enabled
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_suggested_display_precision = 2 if spec.scale < 1 else 0
        if spec.kind == "number":
            self._attr_device_class = {
                "°C": SensorDeviceClass.TEMPERATURE,
                "K": SensorDeviceClass.TEMPERATURE_DELTA,
                "kW": SensorDeviceClass.POWER,
                "W": SensorDeviceClass.POWER,
                "kWh": SensorDeviceClass.ENERGY,
                "A": SensorDeviceClass.CURRENT,
                "V": SensorDeviceClass.VOLTAGE,
                "bar": SensorDeviceClass.PRESSURE,
                "s": SensorDeviceClass.DURATION,
                "min": SensorDeviceClass.DURATION,
                "h": SensorDeviceClass.DURATION,
            }.get(spec.unit)
            self._attr_state_class = (
                SensorStateClass.TOTAL_INCREASING
                if spec.unit in ("h", "kWh")
                else SensorStateClass.MEASUREMENT
            )

    @property
    def native_value(self):
        value = self.coordinator.value(self.spec.key)
        if value is None:
            return None
        if self.spec.key in ("second_demand", "third_demand") or self.spec.key.startswith(
            "queued_demand_"
        ):
            return STATUS_BY_CODE.get(int(value), f"Unknown ({value})")
        if self.spec.key == "smart_grid_status":
            return SMART_GRID_STATUS_BY_CODE.get(int(value), f"Unknown ({value})")
        return value

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self):
        point = TEMPERATURE_POINTS.get(self.spec.key)
        if point is None:
            return None
        attributes = {
            **point,
            "input_register_address": self.spec.address,
        }
        if self.native_value is None:
            unsupported = getattr(getattr(self.coordinator, "device", None), "unsupported", ())
            attributes["availability_reason"] = (
                "unsupported_register"
                if self.spec.key in unsupported
                else "no_valid_controller_reading"
            )
        return attributes


class ThermiaControlLogic(ThermiaEntity, SensorEntity):
    """Expose the explanation in Home Assistant's native entity information."""

    _attr_icon = "mdi:information-outline"
    _attr_entity_category = None
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "control_logic", sensor_name("control_logic"))

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return "Open for explanation"

    @property
    def extra_state_attributes(self):
        return control_guide_attributes(self.coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_update_guide, timedelta(seconds=30))
        )

    @callback
    def _async_update_guide(self, _now: datetime) -> None:
        self.async_write_ha_state()


class _ThermiaTimeRemaining(ThermiaEntity, SensorEntity):
    """Keep the saved countdown visible independently of pump availability."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True
    _attr_icon = "mdi:timer-outline"

    def _time_info(self):
        raise NotImplementedError

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self._time_info()["remaining_hours"]

    @property
    def extra_state_attributes(self):
        info = self._time_info()
        return {key: value for key, value in info.items() if key != "remaining_hours"}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_update_countdown, timedelta(seconds=30)
            )
        )

    @callback
    def _async_update_countdown(self, _now: datetime) -> None:
        self.async_write_ha_state()


class ThermiaExcessTimeRemaining(_ThermiaTimeRemaining):
    """Keep the existing Excess Energy sensor identities."""

    def __init__(self, coordinator, group) -> None:
        key = f"{group}_excess_time_remaining"
        super().__init__(
            coordinator,
            key,
            sensor_name(key),
        )
        self.group = group

    def _time_info(self):
        return excess_time_info(self.coordinator, self.group)


class ThermiaLowTimeRemaining(_ThermiaTimeRemaining):
    """Show the independently timed Low Mode hot-water profile."""

    def __init__(self, coordinator) -> None:
        super().__init__(
            coordinator,
            "hot_water_low_time_remaining",
            sensor_name("hot_water_low_time_remaining"),
        )

    def _time_info(self):
        return low_time_info(self.coordinator)


class ThermiaStatusSensor(ThermiaEntity, SensorEntity):
    """Main priority plus concurrent demand, queue and control diagnostics."""

    _attr_icon = "mdi:heat-pump"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "main_demand", sensor_name("main_demand"))

    @property
    def native_value(self):
        return self.coordinator.status

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.value("main_demand") is not None

    @property
    def extra_state_attributes(self):
        queued = [self.coordinator.value(f"queued_demand_{i}") for i in range(1, 6)]
        return {
            "active_demands": self.coordinator.active_demands,
            "queued_demands": [
                STATUS_BY_CODE.get(int(value), f"Unknown ({value})")
                for value in queued
                if value is not None
            ],
            "alarm_classes": self.coordinator.alarm_classes,
            "compressor_start_restriction_seconds": self.coordinator.value(
                "start_restriction_timer"
            ),
            "control_warning": self.coordinator.engine.state.get("control_warning"),
        }


class ThermiaEffectiveTemperature(ThermiaEntity, SensorEntity):
    """Selected external or native temperature with its active source."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, location) -> None:
        key = f"effective_{location}_temperature"
        super().__init__(coordinator, key, sensor_name(key))
        self.location = location

    @property
    def native_value(self):
        return getattr(self.coordinator, f"effective_{self.location}")

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self):
        return {
            "source": getattr(self.coordinator, f"{self.location}_source"),
            **self.coordinator.temperature_source_details(self.location),
        }


class ThermiaTemperatureSource(ThermiaEntity, SensorEntity):
    """Keep the source diagnosis readable when no temperature is available."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True
    _attr_icon = "mdi:thermometer-alert"

    def __init__(self, coordinator, location) -> None:
        key = f"{location}_temperature_source"
        super().__init__(coordinator, key, sensor_name(key))
        self.location = location

    @property
    def available(self) -> bool:
        # This explains the selected local HA sensor and any cached native
        # reading, even while the heat pump itself is disconnected.
        return True

    @property
    def native_value(self):
        info = self.coordinator.temperature_source_info(self.location)
        return temperature_source_status(
            info["external_status"], info["fallback_status"], inside=self.location == "inside"
        )

    @property
    def extra_state_attributes(self):
        return {
            "source": getattr(self.coordinator, f"{self.location}_source"),
            **self.coordinator.temperature_source_details(self.location),
        }


class _ThermiaHumidityDiagnostic(ThermiaEntity, SensorEntity):
    """Read local humidity diagnostics without issuing controller requests."""

    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True

    def _guard_info(self):
        # A selected sensor can expire between failed polls and their backoff.
        # Read current local values without changing the engine's cached policy.
        return self.coordinator.engine.cooling_humidity_info(
            inside=self.coordinator.effective_inside,
            humidity=self.coordinator.effective_inside_humidity,
        )

    @property
    def extra_state_attributes(self):
        return {
            **self._guard_info(),
            **self.coordinator.humidity_source_details(),
            "inside_source": self.coordinator.inside_source,
            "controller_available": bool(self.coordinator.last_update_success),
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_update_humidity, timedelta(seconds=30)
            )
        )

    @callback
    def _async_update_humidity(self, _now: datetime) -> None:
        # Local sensor validity and a chosen report timeout can change while
        # controller polling is offline. This only updates the displayed state.
        self.async_write_ha_state()


class ThermiaInsideHumidity(_ThermiaHumidityDiagnostic):
    """Expose only the optional external indoor relative humidity reading."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(
            coordinator, "inside_relative_humidity", sensor_name("inside_relative_humidity")
        )

    @property
    def native_value(self):
        return self.coordinator.effective_inside_humidity

    @property
    def available(self) -> bool:
        return self.native_value is not None


class ThermiaInsideDewPoint(_ThermiaHumidityDiagnostic):
    """Derived indoor dew point; it has no fallback when humidity is missing."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "inside_dew_point", sensor_name("inside_dew_point"))

    @property
    def native_value(self):
        return self._guard_info()["dew_point"]

    @property
    def available(self) -> bool:
        return self.native_value is not None


class ThermiaCoolingHumidityProtection(_ThermiaHumidityDiagnostic):
    """Keep the pause explanation visible even with unavailable inputs."""

    _attr_icon = "mdi:water-thermometer-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(
            coordinator, "cooling_humidity_protection", sensor_name("cooling_humidity_protection")
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self):
        info = self._guard_info()
        if info.get("restore_pending"):
            return "Restoration pending"
        if (
            info["status"] == "high_humidity"
            and info["relative_humidity"] is not None
            and info["relative_humidity"] < info["humidity_limit"]
        ):
            return "Paused: waiting for humidity to fall"
        return {
            "no_sensor_selected": "No humidity sensor selected",
            "disabled_in_normal": "Disabled in Normal",
            "ready": "Ready",
            "high_humidity": "Paused: humidity limit reached",
            "missing_humidity": "Paused: humidity unavailable",
            "missing_inside_temperature": "Paused: inside temperature unavailable",
            "invalid_settings": "Paused: invalid protection settings",
            "humidity_hysteresis": "Paused: waiting for humidity to fall",
            "supply_limit": "Paused: cooling supply limit exceeded",
            "cooling_supply_unavailable": "Paused: cooling supply target unavailable",
            "dew_point_above_supply_limit": "Paused: required cooling supply exceeds limit",
            "restore_pending": "Restoration pending",
        }.get(info["status"], str(info["status"]).replace("_", " ").capitalize())


class ThermiaCOPSensor(ThermiaEntity, SensorEntity):
    """Ratio of reported thermal and electrical power, when both are valid."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-line"
    _attr_suggested_display_precision = 2
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "instantaneous_cop", sensor_name("instantaneous_cop"))

    @property
    def native_value(self):
        thermal = self.coordinator.value("thermal_power")
        electrical = self.coordinator.value("electric_power")
        if thermal is None or electrical is None:
            return None
        if (
            not math.isfinite(thermal)
            or not math.isfinite(electrical)
            or thermal < 0
            or electrical <= 0
        ):
            return None
        return round(thermal / electrical, 2)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class ThermiaControlWarning(ThermiaEntity, SensorEntity):
    """Keep a failed request or incomplete restoration visible to the user."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "control_warning", sensor_name("control_warning"))

    @property
    def native_value(self):
        warning = self.coordinator.engine.state.get("control_warning")
        return warning[:255] if warning else "Ready"
