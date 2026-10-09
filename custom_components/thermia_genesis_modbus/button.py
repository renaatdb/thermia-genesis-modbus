"""One-cycle hot-water boost action."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import ThermiaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the optional boost button."""
    async_add_entities([ThermiaBoostButton(entry.runtime_data)])


class ThermiaBoostButton(ThermiaEntity, ButtonEntity):
    """Temporarily request 60/60°C, then restore the captured automatic targets."""

    _attr_icon = "mdi:water-boiler-auto"
    _attr_entity_registry_enabled_default = True
    _attr_entity_registry_visible_default = True

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, "boost_once", "Hot Water Boost Once")

    @property
    def available(self) -> bool:
        settings = self.coordinator.engine.settings
        limits = (settings.get("max_hot_water_temperature"), settings.get("max_start_temperature"))
        return (
            super().available
            and all(
                isinstance(limit, (int, float)) and math.isfinite(limit) and limit >= 60
                for limit in limits
            )
            and self.coordinator.value("hot_water_enabled") is not None
            and self.coordinator.value("hot_water_start") is not None
            and self.coordinator.value("hot_water_stop") is not None
        )

    async def async_press(self) -> None:
        await self.coordinator.async_command("boost_once")
