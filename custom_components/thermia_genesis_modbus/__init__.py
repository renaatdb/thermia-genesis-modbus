"""Thermia Genesis Modbus integration for a local Genesis Modbus controller."""

from homeassistant.components.modbus import async_get_unit
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from modbus_connection import ModbusTcpParams

from .const import CONF_UNIT_ID, NAME, PLATFORMS, normalize_options
from .coordinator import ThermiaCoordinator
from .device import ThermiaDevice
from .entity import async_update_device_model
from .migration import async_migrate_configuration_entities


async def async_setup_entry(hass, entry):
    # Keep existing entity identities while updating only our old default name.
    # Custom entry names and device name_by_user remain the user's choice.
    if entry.title == "Thermia PV":
        hass.config_entries.async_update_entry(entry, title=NAME)
    unit = async_get_unit(
        hass,
        entry,
        ModbusTcpParams(host=entry.data["host"], port=entry.data["port"]),
        entry.data[CONF_UNIT_ID],
    )
    device = ThermiaDevice(unit, undocumented=True)
    coordinator = ThermiaCoordinator(hass, entry, device)
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await async_migrate_configuration_entities(hass, entry, coordinator)
    coordinator.async_listen_sensors()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop)
    )
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(name) for name in PLATFORMS]
    )
    return True


async def _async_options_updated(hass, entry):
    coordinator = entry.runtime_data
    async with coordinator._lock:
        coordinator._pending_options = normalize_options(entry.options)
        applied = await coordinator.async_apply_pending_options()
    if applied:
        async_update_device_model(hass, entry)
    await coordinator.async_request_refresh()


async def async_unload_entry(hass, entry):
    # Restore while this entry still holds its shared Modbus connection.
    await entry.runtime_data.async_shutdown()
    return await hass.config_entries.async_unload_platforms(
        entry, [Platform(name) for name in PLATFORMS]
    )
