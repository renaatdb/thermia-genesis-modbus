"""Native controller settings, with actual readbacks and bounded sliders."""

from __future__ import annotations

import math
from typing import Any, NamedTuple

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfRatio, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEFAULT_OPTIONS
from .entity import ThermiaEntity
from .native_settings import NATIVE_SETTINGS, NativeSetting


def _native_reading(coordinator: Any, key: str) -> float | None:
    value = coordinator.value(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


class SettingSlider(NamedTuple):
    """A device-page preference, using the controller's saved command path."""

    key: str
    label: str
    min_value: float
    max_value: float
    step: float = 1.0
    unit: str = UnitOfTemperature.CELSIUS
    device_class: str = NumberDeviceClass.TEMPERATURE


DEVICE_SETTINGS = {
    item.key: item
    for item in (
        SettingSlider(
            "heating_low_offset",
            "B1.01 Heating — Low Mode Reduction",
            0,
            25,
            device_class=NumberDeviceClass.TEMPERATURE_DELTA,
        ),
        SettingSlider("heating_vacation_temperature", "B1.02 Heating — Vacation Target", 10, 35),
        SettingSlider("max_heating_temperature", "B1.03 Heating — Excess Energy Ceiling", 10, 35),
        SettingSlider(
            "heating_excess_heat_stop_offset",
            "B1.04 Heating — Excess Energy Heat Stop Increase",
            0,
            25,
            device_class=NumberDeviceClass.TEMPERATURE_DELTA,
        ),
        SettingSlider(
            "heating_excess_hours",
            "B1.05 Heating — Excess Energy Duration",
            1,
            168,
            1,
            UnitOfTime.HOURS,
            NumberDeviceClass.DURATION,
        ),
        SettingSlider(
            "heating_vacation_cooling_offset",
            "B3.01 Cooling — Vacation Reduction",
            0,
            25,
            device_class=NumberDeviceClass.TEMPERATURE_DELTA,
        ),
        SettingSlider(
            "cooling_humidity_limit",
            "B3.02 Cooling — Normal Humidity Limit",
            30,
            90,
            1,
            UnitOfRatio.PERCENTAGE,
            NumberDeviceClass.HUMIDITY,
        ),
        SettingSlider(
            "cooling_vacation_humidity_limit",
            "B3.03 Cooling — Vacation Humidity Limit",
            30,
            90,
            1,
            UnitOfRatio.PERCENTAGE,
            NumberDeviceClass.HUMIDITY,
        ),
        SettingSlider(
            "cooling_dew_point_margin",
            "B3.04 Cooling — Dew Point Margin",
            1,
            5,
            device_class=NumberDeviceClass.TEMPERATURE_DELTA,
        ),
        SettingSlider(
            "hot_water_hysteresis",
            "B2.01 Hot Water — Excess Energy Restart Gap",
            1,
            30,
            device_class=NumberDeviceClass.TEMPERATURE_DELTA,
        ),
        SettingSlider(
            "hot_water_excess_hours",
            "B2.02 Hot Water — Excess Energy Duration",
            1,
            168,
            1,
            UnitOfTime.HOURS,
            NumberDeviceClass.DURATION,
        ),
        SettingSlider("hot_water_evening_start", "B2.03 Hot Water — Low Mode Start", 30, 60),
        SettingSlider("hot_water_evening_stop", "B2.04 Hot Water — Low Mode Stop", 30, 60),
        SettingSlider(
            "hot_water_low_hours",
            "B2.05 Hot Water — Low Mode Duration",
            1,
            168,
            1,
            UnitOfTime.HOURS,
            NumberDeviceClass.DURATION,
        ),
    )
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add everyday preferences and the supported native Heat stop slider."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ThermiaHeatingChargeLimit(coordinator)
            if spec.key == "max_heating_temperature"
            else ThermiaSettingNumber(coordinator, spec)
            for spec in DEVICE_SETTINGS.values()
        ]
        + (
            [
                ThermiaNativeSettingNumber(
                    coordinator,
                    NATIVE_SETTINGS["heating_season_stop"],
                    name="B1.06 Heating — Heat Stop",
                )
            ]
            if _native_reading(coordinator, "heating_season_stop") is not None
            else []
        )
        + [
            ThermiaNativeHeatingTarget(coordinator),
            ThermiaHotWaterStart(coordinator),
            ThermiaHotWaterStop(coordinator),
        ]
    )


class ThermiaSettingNumber(ThermiaEntity, NumberEntity):
    """Edit a saved preference without rebasing an active preset's snapshot."""

    _attr_entity_registry_visible_default = True
    _attr_entity_registry_enabled_default = True
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, spec: SettingSlider) -> None:
        super().__init__(coordinator, spec.key, spec.label)
        self.setting_spec = spec
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.engine.settings.get(
            self.setting_spec.key, DEFAULT_OPTIONS[self.setting_spec.key]
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            result = float(value)
        except OverflowError:
            return None
        return result if math.isfinite(result) else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    async def async_set_native_value(self, value: float) -> None:
        spec = self.setting_spec
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not spec.min_value <= value <= spec.max_value
            or not math.isfinite(value)
        ):
            raise ValueError(
                f"{spec.label} must be {spec.min_value:g}–{spec.max_value:g}{spec.unit}"
            )
        steps = (value - spec.min_value) / spec.step
        if not math.isclose(steps, round(steps), abs_tol=1e-8):
            raise ValueError(f"{spec.label} must use {spec.step:g}{spec.unit} steps")
        await self.coordinator.async_command("setting", (spec.key, value))


class ThermiaHeatingChargeLimit(ThermiaSettingNumber):
    """Keep the existing charging-ceiling identity for surplus automations."""

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, DEVICE_SETTINGS["max_heating_temperature"])


class ThermiaNativeSettingNumber(ThermiaEntity, NumberEntity):
    """Change a native pump setting through the shared verified command path."""

    _attr_entity_registry_visible_default = True
    _attr_entity_registry_enabled_default = True
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator: Any, spec: NativeSetting, *, name: str | None = None) -> None:
        super().__init__(coordinator, spec.key, name or spec.label)
        self.spec = spec
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step

    @property
    def native_value(self) -> float | None:
        return _native_reading(self.coordinator, self.spec.key)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attributes = {"holding_register_address": self.spec.address}
        if self.spec.key.startswith("heat_curve_supply_"):
            point = self.spec.key.removeprefix("heat_curve_supply_")
            attributes["outdoor_temperature"] = _native_reading(
                self.coordinator, f"heat_curve_outdoor_{point}"
            )
        return attributes

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_command("native_setting", (self.spec.key, value))


class ThermiaHotWaterStart(ThermiaEntity, NumberEntity):
    """Edit one native threshold without changing another setting or permission."""

    _attr_native_min_value = 30.0
    _attr_native_max_value = 60.0
    _attr_entity_registry_visible_default = True
    _attr_entity_registry_enabled_default = True
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-thermometer"

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, "hot_water_start", "B2.06 Hot Water - Native START")
        self._state_key = "hot_water_start"
        self._read_key = "hot_water_start"
        self._action = "native_hot_water_start"

    @property
    def native_max_value(self) -> float:
        limit_key = (
            "max_start_temperature"
            if self._read_key == "hot_water_start"
            else "max_hot_water_temperature"
        )
        maximum = self.coordinator.engine.settings.get(limit_key)
        if (
            isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and math.isfinite(maximum)
        ):
            return max(30.0, min(60.0, math.floor(maximum)))
        return 60.0

    @property
    def native_value(self) -> float | None:
        return _native_reading(self.coordinator, self._read_key)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "normal_temperature": self.coordinator.engine.state.get(self._state_key),
            "boost_active": bool(self.coordinator.engine.state.get("boost_enabled", False)),
            "boost_once_active": bool(self.coordinator.engine.state.get("boost_once", False)),
        }

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_command(self._action, value)


class ThermiaHotWaterStop(ThermiaHotWaterStart):
    """Show the actual stop threshold; edits update the saved normal target."""

    def __init__(self, coordinator: Any) -> None:
        ThermiaEntity.__init__(self, coordinator, "hot_water_stop", "B2.07 Hot Water - Native STOP")
        self._state_key = "hot_water_target"
        self._read_key = "hot_water_stop"
        self._action = "native_hot_water_stop"


class ThermiaNativeHeatingTarget(ThermiaHotWaterStart):
    """Prepare the native heating dial before enabling space heating."""

    _attr_native_min_value = 10.0
    _attr_native_max_value = 35.0
    _attr_native_step = 0.01
    _attr_icon = "mdi:home-thermometer"

    def __init__(self, coordinator: Any) -> None:
        ThermiaEntity.__init__(
            self, coordinator, "native_heating_target", "B1.07 Heating - Native Target"
        )
        self._state_key = "heating_target"
        self._read_key = "comfort_wheel"
        self._action = "native_heating_target"

    @property
    def native_max_value(self) -> float:
        return 35.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "normal_temperature": self.coordinator.engine.state.get("heating_target"),
            "thermostat_managed": self.coordinator.engine.state["managed_heating"],
            "holding_register_address": 5,
        }
