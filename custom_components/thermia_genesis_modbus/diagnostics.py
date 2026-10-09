"""A local diagnostics download for validating the firmware register map."""

from homeassistant.components.diagnostics import async_redact_data

from .const import heat_pump_model_label


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    state = dict(coordinator.engine.state)
    return async_redact_data(
        {
            "integration_version": "0.1.18",
            "controller": heat_pump_model_label(coordinator.engine.settings.get("heat_pump_model")),
            "connection": dict(entry.data),
            "settings": coordinator.engine.settings,
            "controller_state": state,
            "values": coordinator.data,
            "unsupported_registers": sorted(coordinator.device.unsupported),
            "temperature_sources": {
                location: coordinator.temperature_source_info(location)
                for location in ("inside", "outside")
            },
            "humidity_protection": coordinator.engine.cooling_humidity_info(
                inside=coordinator.effective_inside,
                humidity=coordinator.effective_inside_humidity,
            ),
            "inside_source": "External sensor"
            if coordinator.inside_source == coordinator.engine.settings.get("inside_sensor")
            else coordinator.inside_source,
            "outside_source": "External sensor"
            if coordinator.outside_source == coordinator.engine.settings.get("outside_sensor")
            else coordinator.outside_source,
        },
        {"host", "inside_sensor", "outside_sensor", "inside_humidity_sensor"},
    )
