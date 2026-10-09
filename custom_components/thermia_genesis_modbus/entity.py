"""Shared identity for entities belonging to one heat pump."""

from homeassistant.helpers import device_registry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, heat_pump_model_label


def async_update_device_model(hass, entry):
    """Update the display model of the existing device without touching its name."""
    registry = device_registry.async_get(hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, entry.unique_id or entry.entry_id), entry.entry_id
    )
    model = heat_pump_model_label(entry.options.get("heat_pump_model"))
    if device is not None and device.model != model:
        registry.async_update_device(device.id, model=model)


class ThermiaEntity(CoordinatorEntity):
    """An entity backed by the shared coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.unique_id or coordinator.entry.entry_id)},
            manufacturer="Thermia",
            model=heat_pump_model_label(coordinator.entry.options.get("heat_pump_model")),
            name=coordinator.entry.title,
        )
