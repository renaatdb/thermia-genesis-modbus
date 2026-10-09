"""Native heat-pump enable controls."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ThermiaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the native switches."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ThermiaSwitch(
                coordinator,
                "native_heating_enabled",
                "A1.01 Heating",
                "heating_enabled",
                "mdi:radiator",
                entity_key="heating_enabled",
            ),
            ThermiaSwitch(
                coordinator,
                "native_cooling_enabled",
                "A1.06 Cooling (Passive)",
                "passive_cooling_enabled",
                "mdi:snowflake",
                entity_key="passive_cooling",
            ),
            ThermiaCoolingHumiditySwitch(coordinator),
            ThermiaSwitch(
                coordinator,
                "hot_water_enabled",
                "A1.03 Hot Water Enabled",
                "hot_water_enabled",
                "mdi:water-boiler",
            ),
            ThermiaBoostSwitch(coordinator),
            ThermiaSwitch(
                coordinator,
                "auxiliary_heater",
                "A1.02 Electric Auxiliary Heater Allowed",
                "immersion_heater",
                "mdi:radiator",
                on_value=2,
                valid_values=(0, 1, 2),
            ),
            ThermiaSwitch(
                coordinator,
                "anti_legionella",
                "A1.05 Anti-Legionella Programme",
                "anti_legionella_enabled",
                "mdi:bacteria-outline",
            ),
        ]
    )


class ThermiaSwitch(ThermiaEntity, SwitchEntity):
    """Expose native permission flags and the managed hot-water function."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: Any,
        action: str,
        name: str,
        read_key: str,
        icon: str,
        *,
        on_value: int = 1,
        valid_values: tuple[int, ...] = (0, 1),
        entity_key: str | None = None,
    ) -> None:
        super().__init__(coordinator, entity_key or action, name)
        self._action = action
        self._read_key = read_key
        self._on_value = on_value
        self._valid_values = valid_values
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = True
        self._attr_entity_registry_visible_default = True

    @property
    def available(self) -> bool:
        return super().available and self._native_value is not None

    @property
    def _native_value(self) -> int | float | bool | None:
        value = self.coordinator.value(self._read_key)
        if isinstance(value, (bool, int, float)) and value in self._valid_values:
            return value
        return None

    @property
    def is_on(self) -> bool | None:
        value = self._native_value
        if value is None:
            return None
        if self._action == "hot_water_enabled" and self.coordinator.engine.external_control:
            return value == self._on_value
        mode_key = {"hot_water_enabled": "hot_water_mode"}.get(self._action)
        if mode_key is not None:
            mode = self.coordinator.engine.state.get(mode_key)
            return None if mode is None else mode != "off"
        return value == self._on_value

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(self._action, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command(self._action, False)


class ThermiaBoostSwitch(ThermiaSwitch):
    """Expose Thermia's native Boost flag without changing its temperatures."""

    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator,
            "native_hot_water_boost",
            "A1.04 Thermia Boost",
            "hot_water_boost",
            "mdi:water-boiler-auto",
            entity_key="hot_water_boost",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.engine.state
        native = self._native_value
        if native is None:
            native_status = "Unavailable on controller"
        elif native:
            native_status = "Active"
        elif state.get("boost_enabled", False) and any(
            isinstance(temperature := self.coordinator.value(key), (int, float))
            and not isinstance(temperature, bool)
            and math.isfinite(temperature)
            and temperature >= 60
            for key in ("hot_water_top_temperature", "hot_water_weighted_temperature")
        ):
            native_status = "Paused at 60°C"
        else:
            native_status = "Off"
        return {
            "normal_start_temperature": state.get("hot_water_start"),
            "normal_stop_temperature": state.get("hot_water_target"),
            "native_boost_enabled": native,
            "native_boost_available": native is not None,
            "native_boost_status": native_status,
            "native_boost_coupled": native is not None
            and bool(state.get("native_boost_coupled", False)),
        }


class ThermiaCoolingHumiditySwitch(ThermiaEntity, SwitchEntity):
    """Enable the optional Normal cooling guard through saved preferences."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-percent"
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True

    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator, "cooling_humidity_enabled", "A1.07 Humidity Protection In Normal Cooling"
        )

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.engine.settings.get("cooling_humidity_enabled", False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self.coordinator.engine.cooling_humidity_info(
            inside=self.coordinator.effective_inside,
            humidity=self.coordinator.effective_inside_humidity,
        )
        return {
            "indoor_humidity_limit": info["humidity_limit"],
            "indoor_humidity_sensor": self.coordinator.engine.settings.get("inside_humidity_sensor")
            or None,
            "scope": (
                "Normal cooling in Cool and Heat/Cool, including Low Mode's cooling, uses the "
                "Normal humidity limit when this switch is on. Vacation uses its own humidity "
                "limit with a selected sensor regardless of this switch."
            ),
            **info,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_command("setting", ("cooling_humidity_enabled", True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_command("setting", ("cooling_humidity_enabled", False))
