"""Room and hot-water thermostats for Thermia Genesis Modbus."""

from __future__ import annotations

import math
from typing import Any, ClassVar

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.unit_conversion import TemperatureConverter

from .entity import ThermiaEntity
from .excess_time import excess_time_attributes, low_time_attributes

PRESET_NORMAL = "Normal"
PRESET_EXCESS_ENERGY = "Excess Energy"
PRESET_EXCESS_HEATING_ONLY = "Excess Energy (Heating Only)"
PRESET_LOW_MODE = "Low Mode"
PRESET_LOW_HEATING_ONLY = "Low Mode (Heating Only)"
PRESET_VACATION = "Vacation"
PRESET_EVENING = "Evening"


def _finite_temperature(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _current_temperature_in_display_units(entity: ClimateEntity) -> float | None:
    """Keep measured precision while retaining Home Assistant's unit conversion."""
    value = entity.current_temperature
    if value is None:
        return None
    hass = getattr(entity, "hass", None)
    if hass is None:
        return value
    return _finite_temperature(
        TemperatureConverter.convert(
            value, entity.temperature_unit, hass.config.units.temperature_unit
        )
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the room thermostat and paired hot-water thermostat."""
    async_add_entities(
        [ThermiaClimate(entry.runtime_data), ThermiaHotWaterClimate(entry.runtime_data)]
    )


class ThermiaClimate(ThermiaEntity, ClimateEntity):
    """Expose requested mode separately from the pump's actual activity."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
    ]
    _attr_preset_modes: ClassVar[list[str]] = [
        PRESET_NORMAL,
        PRESET_EXCESS_ENERGY,
        PRESET_LOW_MODE,
        PRESET_VACATION,
    ]
    _attr_min_temp = 10.0
    _attr_max_temp = 35.0
    _attr_target_temperature_step = 1.0
    # Core target/min/max formatting; measured values are supplied unrounded below.
    _attr_precision = PRECISION_TENTHS
    _attr_icon = "mdi:home-thermometer"
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, "heating_cooling", "Heating And Cooling")

    @property
    def available(self) -> bool:
        return super().available and not self.coordinator.engine.external_control

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode(self.coordinator.engine.state["heating_mode"])

    @property
    def preset_mode(self) -> str | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        return {
            "pv_charge": (
                PRESET_EXCESS_HEATING_ONLY
                if self.hvac_mode == HVACMode.HEAT_COOL
                else PRESET_EXCESS_ENERGY
            ),
            "low": (
                PRESET_LOW_HEATING_ONLY if self.hvac_mode == HVACMode.HEAT_COOL else PRESET_LOW_MODE
            ),
            "vacation": PRESET_VACATION,
        }.get(self.coordinator.engine.state["heating_preset"], PRESET_NORMAL)

    @property
    def preset_modes(self) -> list[str]:
        if self.hvac_mode == HVACMode.OFF:
            return []
        if self.hvac_mode == HVACMode.COOL:
            return [PRESET_NORMAL, PRESET_VACATION]
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return [
                PRESET_NORMAL,
                PRESET_EXCESS_HEATING_ONLY,
                PRESET_LOW_HEATING_ONLY,
                PRESET_VACATION,
            ]
        return list(self._attr_preset_modes)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        if self.hvac_mode == HVACMode.OFF:
            return self._attr_supported_features & ~ClimateEntityFeature.PRESET_MODE
        return self._attr_supported_features

    @property
    def min_temp(self) -> float:
        return 10.0

    @property
    def current_temperature(self) -> float | None:
        return _finite_temperature(self.coordinator.effective_inside)

    @property
    def current_humidity(self) -> float | None:
        if not self.coordinator.engine.settings.get("inside_humidity_sensor"):
            return None
        return self.coordinator.effective_inside_humidity

    @property
    def target_temperature(self) -> float | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        if self.coordinator.engine.state["heating_preset"] == "pv_charge":
            return self.coordinator.engine.settings.get("max_heating_temperature")
        if self.hvac_mode == HVACMode.HEAT_COOL:
            return None
        return self.coordinator.engine.effective_heating_target()

    @property
    def target_temperature_low(self) -> float | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        if (
            self.hvac_mode != HVACMode.HEAT_COOL
            or self.coordinator.engine.state["heating_preset"] == "pv_charge"
        ):
            return self.target_temperature
        return self.coordinator.engine.effective_heating_range()[0]

    @property
    def target_temperature_high(self) -> float | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        if (
            self.hvac_mode != HVACMode.HEAT_COOL
            or self.coordinator.engine.state["heating_preset"] == "pv_charge"
        ):
            return self.target_temperature
        return self.coordinator.engine.effective_heating_range()[1]

    @property
    def hvac_action(self) -> HVACAction | None:
        # Never infer actual activity solely from our requested mode/preset.
        demands = {
            str(value).lower().replace(" ", "_") for value in self.coordinator.active_demands
        }
        if "heating" in demands or "space_heating" in demands:
            return HVACAction.HEATING
        if demands.intersection({"cooling", "passive_cooling", "active_cooling"}):
            return HVACAction.COOLING
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        status = self.coordinator.status
        if status is None or str(status).lower().startswith(("unknown", "unavailable")):
            return None
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.engine.state
        native_target = self.coordinator.value("comfort_wheel")
        if (
            isinstance(native_target, bool)
            or not isinstance(native_target, (int, float))
            or not 10 <= native_target <= 40
            or not math.isfinite(native_target)
        ):
            native_target = None
        charging = state["heating_preset"] == "pv_charge"
        expected_target = (
            self.coordinator.engine.settings.get("max_heating_temperature")
            if charging
            else self.coordinator.engine.effective_heating_range()[0]
            if self.hvac_mode == HVACMode.HEAT_COOL
            else self.coordinator.engine.effective_heating_target()
        )
        if charging:
            paused = not state.get("charge_active", {}).get("heating", False)
            if native_target is None:
                sync = "Thermia heating target unavailable; Excess Energy synchronization cannot be confirmed."
            elif native_target == expected_target:
                sync = "Thermia heating target matches the Excess Energy ceiling."
            else:
                sync = "Thermia heating target differs from the Excess Energy ceiling."
            if paused:
                sync = "Excess Energy is paused. " + sync
        elif self.hvac_mode == HVACMode.COOL:
            sync = "Cool keeps its room target in Home Assistant; it does not copy a native heating target."
        elif self.hvac_mode == HVACMode.OFF:
            sync = "Off disables both permissions; Heat copies its target and Auto copies its lower target."
        elif native_target is None:
            sync = "Thermia heating target unavailable; synchronization cannot be confirmed."
        elif native_target == expected_target:
            sync = (
                "Thermia heating target matches the Auto lower target."
                if self.hvac_mode == HVACMode.HEAT_COOL
                else "Thermia heating target matches the Heat target."
            )
        else:
            sync = "Thermia heating target differs from the selected Home Assistant heating target."
        return {
            # Extra attributes are merged after Core's rounded climate attributes.
            "current_temperature": _current_temperature_in_display_units(self),
            **excess_time_attributes(self.coordinator, "heating"),
            "normal_target_temperature": state["heating_target"],
            "normal_target_temperature_low": state["heating_low"],
            "normal_target_temperature_high": state["heating_high"],
            "low_mode_temperature_setback": self.coordinator.engine.settings.get(
                "heating_low_offset", 2.0
            ),
            "vacation_heating_temperature": self.coordinator.engine.settings.get(
                "heating_vacation_temperature", 17.0
            ),
            "vacation_cooling_temperature_reduction": self.coordinator.engine.settings.get(
                "heating_vacation_cooling_offset", 0.0
            ),
            "cooling_humidity_protection": self.coordinator.engine.cooling_humidity_info(
                inside=self.current_temperature,
                humidity=self.coordinator.effective_inside_humidity,
            ),
            "native_heating_target_temperature": native_target,
            "heating_target_sync": sync,
            "cooling_room_target_scope": (
                "Home Assistant only: Genesis 17.1 has no documented writable cooling room target. "
                "Auto and Vacation use the inside reading to control cooling at the room target. "
                "The cooling supply setting controls water temperature."
            ),
            "charging_temperature_limit": self.coordinator.engine.settings.get(
                "max_heating_temperature"
            ),
            "inside_temperature_source": self.coordinator.inside_source,
            "outside_temperature_source": self.coordinator.outside_source,
            "outside_temperature": self.coordinator.effective_outside,
            "active_demands": self.coordinator.active_demands,
            "heat_pump_status": self.coordinator.status,
            "automatic_mode": "heat_cool",
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_command("heating_mode", hvac_mode.value)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if self.hvac_mode == HVACMode.OFF:
            raise HomeAssistantError("Turn on heating or cooling before selecting a preset.")
        modes = {
            PRESET_NORMAL: "normal",
            PRESET_EXCESS_ENERGY: "pv_charge",
            PRESET_EXCESS_HEATING_ONLY: "pv_charge",
            PRESET_LOW_MODE: "low",
            PRESET_LOW_HEATING_ONLY: "low",
            PRESET_VACATION: "vacation",
        }
        if preset_mode not in modes:
            raise HomeAssistantError(
                "Select Normal, Excess Energy, Low Mode or Vacation for heating and cooling."
            )
        if self.hvac_mode == HVACMode.COOL and modes[preset_mode] in {"low", "pv_charge"}:
            raise HomeAssistantError("Cooling supports Normal and Vacation presets.")
        await self.coordinator.async_command("heating_preset", modes[preset_mode])

    async def async_set_temperature(self, **kwargs: Any) -> None:
        fields = (ATTR_TEMPERATURE, ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH)
        payload = {key: kwargs[key] for key in fields if key in kwargs}
        if payload:
            requested_mode = HVACMode(kwargs.get(ATTR_HVAC_MODE, self.hvac_mode))
            if requested_mode == HVACMode.OFF:
                raise HomeAssistantError(
                    "Turn on heating or cooling before changing its temperature."
                )
            if ATTR_HVAC_MODE in kwargs:
                payload[ATTR_HVAC_MODE] = requested_mode.value
            await self.coordinator.async_command("heating_temperature_edit", payload)
        elif ATTR_HVAC_MODE in kwargs:
            await self.async_set_hvac_mode(HVACMode(kwargs[ATTR_HVAC_MODE]))

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)


class ThermiaHotWaterClimate(ThermiaEntity, ClimateEntity):
    """Keep the boiler's start and stop temperatures together on one card."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.OFF, HVACMode.AUTO]
    _attr_preset_modes: ClassVar[list[str]] = [
        PRESET_NORMAL,
        PRESET_EXCESS_ENERGY,
        PRESET_LOW_MODE,
    ]
    _attr_min_temp = 30.0
    _attr_max_temp = 60.0
    _attr_target_temperature_step = 1.0
    # Core target/min/max formatting; measured values are supplied unrounded below.
    _attr_precision = PRECISION_TENTHS
    _attr_icon = "mdi:water-boiler"
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, "hot_water", "Hot Water")

    def _temperature(self, key: str) -> float | None:
        return _finite_temperature(self.coordinator.value(key))

    @property
    def available(self) -> bool:
        enabled = self.coordinator.value("hot_water_enabled")
        return (
            super().available
            and not self.coordinator.engine.external_control
            and isinstance(enabled, (bool, int, float))
            and enabled in (0, 1)
            and self._temperature("hot_water_start") is not None
            and self._temperature("hot_water_stop") is not None
        )

    @property
    def hvac_mode(self) -> HVACMode:
        return (
            HVACMode.OFF
            if self.coordinator.engine.state.get("hot_water_mode") == "off"
            else HVACMode.AUTO
        )

    @property
    def preset_mode(self) -> str | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        return {
            "energy_excess": PRESET_EXCESS_ENERGY,
            "evening": PRESET_LOW_MODE,
        }.get(self.coordinator.engine.state.get("hot_water_mode"), PRESET_NORMAL)

    @property
    def preset_modes(self) -> list[str]:
        return [] if self.hvac_mode == HVACMode.OFF else list(self._attr_preset_modes)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        if self.hvac_mode == HVACMode.OFF:
            return self._attr_supported_features & ~ClimateEntityFeature.PRESET_MODE
        return self._attr_supported_features

    @property
    def current_temperature(self) -> float | None:
        temperature = self._temperature("hot_water_weighted_temperature")
        return (
            temperature
            if temperature is not None
            else self._temperature("hot_water_top_temperature")
        )

    @property
    def target_temperature_low(self) -> float | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        return self._temperature("hot_water_start")

    @property
    def target_temperature_high(self) -> float | None:
        if self.hvac_mode == HVACMode.OFF:
            return None
        return self._temperature("hot_water_stop")

    @property
    def hvac_action(self) -> HVACAction | None:
        demands = {
            str(value).lower().replace(" ", "_") for value in self.coordinator.active_demands
        }
        if demands.intersection({"hot_water", "domestic_hot_water"}):
            return HVACAction.HEATING
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        status = self.coordinator.status
        if status is None or str(status).lower().startswith(("unknown", "unavailable")):
            return None
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.engine.state
        excess = self.coordinator.engine.hot_water_excess_info()
        raw_boost = self.coordinator.value("hot_water_boost")
        native_boost = (
            bool(raw_boost)
            if isinstance(raw_boost, (bool, int, float)) and raw_boost in (0, 1)
            else None
        )
        return {
            "current_temperature": _current_temperature_in_display_units(self),
            **excess_time_attributes(self.coordinator, "hot_water"),
            **low_time_attributes(self.coordinator),
            "current_temperature_source": (
                "weighted"
                if self._temperature("hot_water_weighted_temperature") is not None
                else "top"
                if self._temperature("hot_water_top_temperature") is not None
                else "unavailable"
            ),
            "hot_water_top_temperature": self._temperature("hot_water_top_temperature"),
            "hot_water_lower_temperature": self._temperature("hot_water_lower_temperature"),
            "hot_water_weighted_temperature": self._temperature("hot_water_weighted_temperature"),
            "normal_start_temperature": state.get("hot_water_start"),
            "normal_stop_temperature": state.get("hot_water_target"),
            "low_mode_active": state.get("hot_water_mode") == "evening",
            "low_mode_start_temperature": self.coordinator.engine.settings.get(
                "hot_water_evening_start", 35.0
            ),
            "low_mode_stop_temperature": self.coordinator.engine.settings.get(
                "hot_water_evening_stop", 40.0
            ),
            "evening_active": state.get("hot_water_mode") == "evening",
            "evening_start_temperature": self.coordinator.engine.settings.get(
                "hot_water_evening_start", 35.0
            ),
            "evening_stop_temperature": self.coordinator.engine.settings.get(
                "hot_water_evening_stop", 40.0
            ),
            "energy_excess_active": state.get("hot_water_mode") == "energy_excess",
            "energy_excess_restart_gap": excess["configured_restart_gap"],
            "energy_excess_configured_restart_gap": excess["configured_restart_gap"],
            "energy_excess_requested_start_temperature": excess["requested_start_temperature"],
            "energy_excess_start_temperature": excess["effective_start_temperature"],
            "energy_excess_stop_temperature": excess["effective_stop_temperature"],
            "energy_excess_effective_restart_gap": excess["effective_restart_gap"],
            "energy_excess_limits_available": excess["available"],
            "energy_excess_limits_status": excess["reason"],
            "native_boost_enabled": native_boost,
            "native_boost_available": native_boost is not None,
            "native_boost_coupled": native_boost is not None
            and bool(state.get("native_boost_coupled", False)),
            "maximum_start_temperature": self.coordinator.engine.settings.get(
                "max_start_temperature"
            ),
            "maximum_stop_temperature": self.coordinator.engine.settings.get(
                "max_hot_water_temperature"
            ),
            "active_demands": self.coordinator.active_demands,
            "heat_pump_status": self.coordinator.status,
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in (HVACMode.OFF, HVACMode.AUTO):
            raise HomeAssistantError("Select Auto or Off for the hot-water thermostat.")
        await self.coordinator.async_command(
            "hot_water_mode", "off" if hvac_mode == HVACMode.OFF else "auto"
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if self.hvac_mode == HVACMode.OFF:
            raise HomeAssistantError("Turn on hot water before selecting a preset.")
        modes = {
            PRESET_NORMAL: "auto",
            PRESET_EXCESS_ENERGY: "energy_excess",
            PRESET_LOW_MODE: "evening",
            PRESET_EVENING: "evening",
        }
        if preset_mode not in modes:
            raise HomeAssistantError("Select Normal, Excess Energy or Low Mode for hot water.")
        await self.coordinator.async_command("hot_water_mode", modes[preset_mode])

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if ATTR_TEMPERATURE in kwargs:
            raise HomeAssistantError("Set both the hot-water start and stop temperatures.")
        fields = (ATTR_TARGET_TEMP_LOW, ATTR_TARGET_TEMP_HIGH)
        payload = {key: kwargs[key] for key in fields if key in kwargs}
        if payload:
            requested_mode = HVACMode(kwargs.get(ATTR_HVAC_MODE, self.hvac_mode))
            if requested_mode == HVACMode.OFF:
                raise HomeAssistantError("Turn on hot water before changing its temperatures.")
            if ATTR_HVAC_MODE in kwargs:
                payload[ATTR_HVAC_MODE] = requested_mode.value
            await self.coordinator.async_command("hot_water_temperature_edit", payload)
            # A capped edit may leave every value unchanged. Publish its accepted
            # readback once so the frontend clears its optimistic target.
            force_update = self.force_update
            self._attr_force_update = True
            try:
                self.async_write_ha_state()
            finally:
                self._attr_force_update = force_update
        elif ATTR_HVAC_MODE in kwargs:
            await self.async_set_hvac_mode(HVACMode(kwargs[ATTR_HVAC_MODE]))

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.AUTO)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
