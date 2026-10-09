"""Domestic hot-water control for Thermia Genesis Modbus."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.water_heater import WaterHeaterEntity, WaterHeaterEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ThermiaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add domestic hot-water controls."""
    async_add_entities([ThermiaWaterHeater(entry.runtime_data)])


class ThermiaWaterHeater(ThermiaEntity, WaterHeaterEntity):
    """On requests the verified configurable maximum; Auto restores normal targets."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_operation_list: ClassVar[list[str]] = [STATE_OFF, "auto", "manual_on"]
    _attr_min_temp = 5.0
    _attr_target_temperature_step = 1.0
    _attr_precision = 0.1
    _attr_icon = "mdi:water-boiler"
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.ON_OFF
    )

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, "hot_water", "Hot Water")

    @property
    def current_operation(self) -> str:
        return self.coordinator.engine.state["hot_water_mode"]

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.value("hot_water_weighted_temperature")

    @property
    def target_temperature(self) -> float | None:
        if self.coordinator.engine.state.get("boost_enabled") or self.coordinator.engine.state.get(
            "boost_once"
        ):
            return 60.0
        if self.current_operation == "manual_on":
            settings = self.coordinator.engine.settings
            return settings.get("hot_water_boost_stop") or settings.get("max_hot_water_temperature")
        return self.coordinator.engine.state["hot_water_target"]

    @property
    def max_temp(self) -> float:
        maximum = self.coordinator.engine.settings.get("max_hot_water_temperature")
        # Until a maximum is verified, display the current target as the upper
        # bound. The command engine rejects requests requiring unverified limits.
        if maximum is None:
            target = self.coordinator.engine.state.get("hot_water_target")
            return self.min_temp if target is None else max(self.min_temp, float(target))
        return float(maximum)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.value("hot_water_enabled") is not None
            and self.coordinator.engine.state.get("hot_water_target") is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        maximum = self.coordinator.engine.settings.get("max_hot_water_temperature")
        return {
            "normal_target_temperature": self.coordinator.engine.state["hot_water_target"],
            "normal_start_temperature": self.coordinator.engine.state["hot_water_start"],
            "maximum_temperature_verified": maximum is not None,
            "configurable_maximum_temperature": maximum,
            "boost_start_temperature": self.coordinator.engine.settings.get(
                "hot_water_boost_start"
            ),
            "boost_stop_temperature": self.coordinator.engine.settings.get("hot_water_boost_stop"),
            "boost_once_active": self.coordinator.engine.state.get("boost_once", False),
            "boost_active": self.coordinator.engine.state.get("boost_enabled", False),
            "native_boost_coupled": self.coordinator.engine.state.get(
                "native_boost_coupled", False
            ),
            "active_start_temperature": self.coordinator.value("hot_water_start"),
            "active_stop_temperature": self.coordinator.value("hot_water_stop"),
            "active_demands": self.coordinator.active_demands,
            "heat_pump_status": self.coordinator.status,
        }

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        await self.coordinator.async_command("hot_water_mode", operation_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        # Changing the slider during Manual on changes the saved Auto target;
        # the active charging target stays at the verified configurable maximum.
        if ATTR_TEMPERATURE in kwargs:
            await self.coordinator.async_command("hot_water_target", kwargs[ATTR_TEMPERATURE])
        if "operation_mode" in kwargs:
            await self.async_set_operation_mode(kwargs["operation_mode"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_operation_mode("manual_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_operation_mode(STATE_OFF)
