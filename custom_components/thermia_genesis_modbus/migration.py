"""Retire replaced device controls while retaining the user's entity choices."""

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


async def async_migrate_configuration_entities(hass, entry, coordinator):
    """Upgrade the device controls once, retaining explicit user choices."""
    version = coordinator.engine.state.get("configuration_ui_version", 0)
    if version >= 5:
        return
    registry = er.async_get(hass)
    # Charging now follows the native supply maximum. Earlier UI migrations
    # also retired controls owned by the boiler climate or Configure page 2.
    obsolete = [("number", "charge_supply_temperature")]
    if version < 4:
        obsolete.extend(
            (
                ("number", "min_supply_temperature"),
                ("number", "max_supply_temperature"),
                ("number", "passive_cooling_supply_target"),
                *(("number", f"heat_curve_supply_{point}") for point in range(1, 8)),
                ("water_heater", "hot_water"),
                ("button", "boost_once"),
            )
        )
    for platform, key in obsolete:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        entity = registry.async_get(entity_id) if entity_id is not None else None
        if entity is not None and entity.config_entry_id == entry.entry_id:
            registry.async_remove(entity_id)

    retained_legacy_controls = (
        (
            ("switch", "auxiliary_heater"),
            ("switch", "anti_legionella"),
            ("number", "max_heating_temperature"),
            ("number", "heating_season_stop"),
        )
        if version < 4
        else ()
    )
    for platform, key in retained_legacy_controls:
        entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        entity = registry.async_get(entity_id) if entity_id is not None else None
        if entity is None or entity.config_entry_id != entry.entry_id:
            continue
        changes = {}
        if entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
            changes["disabled_by"] = None
        if entity.hidden_by is er.RegistryEntryHider.INTEGRATION:
            changes["hidden_by"] = None
        if changes:
            registry.async_update_entity(entity_id, **changes)
    state = coordinator.engine.state
    await coordinator._async_save(state | {"configuration_ui_version": 5})
    state["configuration_ui_version"] = 5
