"""Registry migration checks with only Home Assistant's registry API stubbed."""

import importlib
import sys
import unittest
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


class RegistryEntryDisabler(Enum):
    INTEGRATION = "integration"
    USER = "user"


class RegistryEntryHider(Enum):
    INTEGRATION = "integration"
    USER = "user"


def _module(name, **symbols):
    module = ModuleType(name)
    module.__dict__.update(symbols)
    return module


def _load_migration():
    package_name = "thermia_migration_test"
    package = _module(package_name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus")
    ]
    registry_module = _module(
        "homeassistant.helpers.entity_registry",
        RegistryEntryDisabler=RegistryEntryDisabler,
        RegistryEntryHider=RegistryEntryHider,
        async_get=lambda hass: hass.entity_registry,
    )
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.helpers": _module("homeassistant.helpers", entity_registry=registry_module),
        "homeassistant.helpers.entity_registry": registry_module,
        package_name: package,
    }
    with patch.dict(sys.modules, stubs):
        return importlib.import_module(f"{package_name}.migration")


migration = _load_migration()


class Registry:
    def __init__(self):
        self.entities = {}
        self.lookup = {}
        self.updates = []
        self.removals = []

    def add(
        self, platform, key, *, entry_id="pump", integration="thermia_genesis_modbus", **choices
    ):
        entity_id = f"{platform}.{entry_id}_{integration}_{key}"
        entity = SimpleNamespace(
            entity_id=entity_id,
            unique_id=f"{entry_id}_{key}",
            config_entry_id=choices.get("config_entry_id", entry_id),
            disabled_by=choices.get("disabled_by"),
            hidden_by=choices.get("hidden_by"),
            name=choices.get("name"),
        )
        self.entities[entity_id] = entity
        self.lookup[platform, integration, entity.unique_id] = entity_id
        return entity

    def async_get_entity_id(self, platform, integration, unique_id):
        return self.lookup.get((platform, integration, unique_id))

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_remove(self, entity_id):
        self.removals.append(entity_id)
        del self.entities[entity_id]
        self.lookup = {key: value for key, value in self.lookup.items() if value != entity_id}

    def async_update_entity(self, entity_id, **changes):
        self.updates.append((entity_id, changes))
        entity = self.entities[entity_id]
        for key, value in changes.items():
            setattr(entity, key, value)
        return entity


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def context(self, *, version=None):
        registry = Registry()
        hass = SimpleNamespace(entity_registry=registry)
        entry = SimpleNamespace(entry_id="pump")
        state = {} if version is None else {"configuration_ui_version": version}
        coordinator = SimpleNamespace(engine=SimpleNamespace(state=state), _async_save=AsyncMock())
        return registry, hass, entry, coordinator

    async def test_only_this_entry_obsolete_boiler_advanced_numbers_and_button_are_removed(self):
        registry, hass, entry, coordinator = self.context(version=1)
        obsolete = [
            registry.add("number", "charge_supply_temperature"),
            registry.add("button", "boost_once"),
            registry.add("water_heater", "hot_water"),
            registry.add("number", "min_supply_temperature"),
            registry.add("number", "max_supply_temperature"),
            registry.add("number", "passive_cooling_supply_target"),
            *(registry.add("number", f"heat_curve_supply_{point}") for point in range(1, 8)),
        ]
        retained = [
            registry.add("number", "hot_water_start", name="My START"),
            registry.add("number", "hot_water_stop", name="My STOP"),
            registry.add("switch", "auxiliary_heater"),
            registry.add("switch", "anti_legionella"),
            registry.add("switch", "passive_cooling"),
            registry.add("switch", "heating_enabled"),
            registry.add("switch", "hot_water_enabled"),
            registry.add("switch", "hot_water_boost"),
            registry.add("switch", "cooling_humidity_enabled"),
            registry.add("number", "max_heating_temperature", name="My solar ceiling"),
            registry.add("number", "heating_season_stop"),
            registry.add("number", "hot_water_evening_start"),
            registry.add("climate", "heating_cooling"),
            registry.add("climate", "hot_water"),
            registry.add("sensor", "hot_water_start"),
            registry.add("sensor", "max_heating_temperature"),
            registry.add("number", "max_heating_temperature", entry_id="other_pump"),
            registry.add("number", "charge_supply_temperature", integration="other_integration"),
            registry.add("button", "boost_once", entry_id="other_pump"),
            registry.add("number", "hot_water_start", entry_id="other_pump"),
            registry.add("number", "hot_water_stop", entry_id="other_pump"),
            registry.add("water_heater", "hot_water", entry_id="other_pump"),
        ]
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(set(registry.removals), {entity.entity_id for entity in obsolete})
        self.assertEqual(set(registry.entities), {entity.entity_id for entity in retained})
        self.assertTrue(all(registry.entities[entity.entity_id] is entity for entity in retained))
        self.assertEqual(registry.updates, [])
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        coordinator._async_save.assert_awaited_once_with(coordinator.engine.state)

    async def test_integration_disabled_and_hidden_controls_restore_with_same_ids(self):
        registry, hass, entry, coordinator = self.context(version=1)
        controls = [
            registry.add(
                platform,
                key,
                disabled_by=RegistryEntryDisabler.INTEGRATION,
                hidden_by=RegistryEntryHider.INTEGRATION,
            )
            for platform, key in (
                ("switch", "auxiliary_heater"),
                ("switch", "anti_legionella"),
                ("number", "max_heating_temperature"),
                ("number", "heating_season_stop"),
            )
        ]
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(set(registry.entities), {entity.entity_id for entity in controls})
        self.assertTrue(
            all(entity.disabled_by is None and entity.hidden_by is None for entity in controls)
        )
        self.assertTrue(all(registry.entities[entity.entity_id] is entity for entity in controls))
        self.assertEqual(len(registry.updates), 4)
        self.assertEqual(registry.removals, [])

    async def test_old_disabled_boost_button_removed_without_changing_new_switch_choices(self):
        registry, hass, entry, coordinator = self.context(version=1)
        old_button = registry.add("button", "boost_once", disabled_by=RegistryEntryDisabler.USER)
        new_switch = registry.add(
            "switch",
            "hot_water_boost",
            disabled_by=RegistryEntryDisabler.USER,
            hidden_by=RegistryEntryHider.USER,
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(registry.removals, [old_button.entity_id])
        self.assertIs(registry.entities[new_switch.entity_id], new_switch)
        self.assertIs(new_switch.disabled_by, RegistryEntryDisabler.USER)
        self.assertIs(new_switch.hidden_by, RegistryEntryHider.USER)
        self.assertEqual(registry.updates, [])

    async def test_later_user_disabling_and_hiding_are_not_overridden_on_reload(self):
        registry, hass, entry, coordinator = self.context(version=1)
        entity = registry.add(
            "switch", "auxiliary_heater", disabled_by=RegistryEntryDisabler.INTEGRATION
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIsNone(entity.disabled_by)
        entity.disabled_by = RegistryEntryDisabler.USER
        entity.hidden_by = RegistryEntryHider.USER
        registry.updates.clear()
        coordinator._async_save.reset_mock()
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIs(entity.disabled_by, RegistryEntryDisabler.USER)
        self.assertIs(entity.hidden_by, RegistryEntryHider.USER)
        self.assertEqual(registry.updates, [])
        coordinator._async_save.assert_not_awaited()

    async def test_existing_user_disabled_and_hidden_choices_are_preserved(self):
        registry, hass, entry, coordinator = self.context(version=1)
        entity = registry.add(
            "switch",
            "auxiliary_heater",
            disabled_by=RegistryEntryDisabler.USER,
            hidden_by=RegistryEntryHider.USER,
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIs(entity.disabled_by, RegistryEntryDisabler.USER)
        self.assertIs(entity.hidden_by, RegistryEntryHider.USER)
        self.assertEqual(registry.updates, [])

    async def test_mixed_ownership_clears_only_integration_choices(self):
        registry, hass, entry, coordinator = self.context(version=1)
        user_disabled = registry.add(
            "switch",
            "auxiliary_heater",
            disabled_by=RegistryEntryDisabler.USER,
            hidden_by=RegistryEntryHider.INTEGRATION,
        )
        user_hidden = registry.add(
            "switch",
            "anti_legionella",
            disabled_by=RegistryEntryDisabler.INTEGRATION,
            hidden_by=RegistryEntryHider.USER,
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIs(user_disabled.disabled_by, RegistryEntryDisabler.USER)
        self.assertIsNone(user_disabled.hidden_by)
        self.assertIsNone(user_hidden.disabled_by)
        self.assertIs(user_hidden.hidden_by, RegistryEntryHider.USER)

    async def test_direct_upgrade_from_first_release_restores_controls(self):
        registry, hass, entry, coordinator = self.context()
        entity = registry.add(
            "switch", "auxiliary_heater", hidden_by=RegistryEntryHider.INTEGRATION
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIsNone(entity.disabled_by)
        self.assertIsNone(entity.hidden_by)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)

    async def test_missing_legacy_entities_complete_the_one_time_migration(self):
        registry, hass, entry, coordinator = self.context()
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(registry.updates, [])
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        coordinator._async_save.assert_awaited_once()

    async def test_newer_migration_version_is_not_changed(self):
        registry, hass, entry, coordinator = self.context(version=6)
        entity = registry.add("number", "hot_water_start")
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIsNone(entity.disabled_by)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 6)
        self.assertEqual(registry.updates, [])
        self.assertEqual(registry.removals, [])
        coordinator._async_save.assert_not_awaited()

    async def test_version_two_upgrade_removes_old_boiler_entities_and_restores_auxiliary_controls(
        self,
    ):
        registry, hass, entry, coordinator = self.context(version=2)
        obsolete = [
            registry.add("water_heater", "hot_water"),
        ]
        thresholds = [
            registry.add("number", "hot_water_start", name="My START"),
            registry.add("number", "hot_water_stop", disabled_by=RegistryEntryDisabler.USER),
        ]
        climate = registry.add("climate", "hot_water")
        anti = registry.add(
            "switch",
            "anti_legionella",
            disabled_by=RegistryEntryDisabler.INTEGRATION,
            hidden_by=RegistryEntryHider.INTEGRATION,
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(set(registry.removals), {entity.entity_id for entity in obsolete})
        for threshold in thresholds:
            self.assertIs(registry.entities[threshold.entity_id], threshold)
        self.assertEqual(thresholds[0].name, "My START")
        self.assertIs(thresholds[1].disabled_by, RegistryEntryDisabler.USER)
        self.assertIs(registry.entities[climate.entity_id], climate)
        self.assertIs(registry.entities[anti.entity_id], anti)
        self.assertIsNone(anti.disabled_by)
        self.assertIsNone(anti.hidden_by)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        coordinator._async_save.assert_awaited_once_with(coordinator.engine.state)

    async def test_unique_id_match_never_changes_an_entity_owned_by_another_entry(self):
        registry, hass, entry, coordinator = self.context(version=2)
        obsolete = registry.add("number", "hot_water_start", config_entry_id="other_pump")
        auxiliary = registry.add(
            "switch",
            "auxiliary_heater",
            config_entry_id="other_pump",
            disabled_by=RegistryEntryDisabler.INTEGRATION,
            hidden_by=RegistryEntryHider.INTEGRATION,
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIs(registry.entities[obsolete.entity_id], obsolete)
        self.assertIs(registry.entities[auxiliary.entity_id], auxiliary)
        self.assertIs(auxiliary.disabled_by, RegistryEntryDisabler.INTEGRATION)
        self.assertIs(auxiliary.hidden_by, RegistryEntryHider.INTEGRATION)
        self.assertEqual(registry.updates, [])
        self.assertEqual(registry.removals, [])

    async def test_version_three_upgrades_once_and_preserves_current_user_choices(self):
        registry, hass, entry, coordinator = self.context(version=3)
        auxiliary = registry.add(
            "switch",
            "auxiliary_heater",
            disabled_by=RegistryEntryDisabler.USER,
            hidden_by=RegistryEntryHider.USER,
        )
        for _ in range(2):
            await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertIs(auxiliary.disabled_by, RegistryEntryDisabler.USER)
        self.assertIs(auxiliary.hidden_by, RegistryEntryHider.USER)
        self.assertEqual(registry.updates, [])
        self.assertEqual(registry.removals, [])
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        coordinator._async_save.assert_awaited_once()

    async def test_retained_preferences_preserve_user_names_and_disabled_choices(self):
        registry, hass, entry, coordinator = self.context(version=2)
        number = registry.add(
            "number",
            "max_heating_temperature",
            name="Solar room cap",
            disabled_by=RegistryEntryDisabler.USER,
            hidden_by=RegistryEntryHider.INTEGRATION,
        )
        supply = registry.add(
            "number",
            "heating_season_stop",
            name="My flow setting",
            disabled_by=RegistryEntryDisabler.INTEGRATION,
            hidden_by=RegistryEntryHider.USER,
        )
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(registry.removals, [])
        self.assertEqual(number.name, "Solar room cap")
        self.assertIs(number.disabled_by, RegistryEntryDisabler.USER)
        self.assertIsNone(number.hidden_by)
        self.assertEqual(supply.name, "My flow setting")
        self.assertIsNone(supply.disabled_by)
        self.assertIs(supply.hidden_by, RegistryEntryHider.USER)

    async def test_failed_save_retries_migration_without_deleting_recovery_journal(self):
        registry, hass, entry, coordinator = self.context(version=3)
        journal = {"original_start": 45, "original_stop": 55, "pending": True}
        coordinator.engine.state["saved"] = journal
        obsolete = registry.add("number", "heat_curve_supply_3")
        coordinator._async_save.side_effect = OSError("Storage unavailable")
        with self.assertRaises(OSError):
            await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 3)
        self.assertIs(coordinator.engine.state["saved"], journal)
        self.assertEqual(registry.removals, [obsolete.entity_id])
        coordinator._async_save.side_effect = None
        coordinator._async_save.reset_mock()
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        self.assertIs(coordinator.engine.state["saved"], journal)
        self.assertEqual(registry.removals, [obsolete.entity_id])
        coordinator._async_save.assert_awaited_once_with(coordinator.engine.state)
        coordinator._async_save.reset_mock()
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        coordinator._async_save.assert_not_awaited()

    async def test_version_four_retires_only_this_entries_charging_supply_slider(self):
        registry, hass, entry, coordinator = self.context(version=4)
        journal = {"heating_season_stop": 17.23, "comfort_wheel": 22.34}
        coordinator.engine.state["overrides"] = {"heating": journal}
        obsolete = registry.add(
            "number",
            "charge_supply_temperature",
            name="Old PV flow",
            disabled_by=RegistryEntryDisabler.USER,
        )
        retained = [
            registry.add("number", "charge_supply_temperature", entry_id="other_pump"),
            registry.add("number", "charge_supply_temperature", integration="other_integration"),
            registry.add(
                "number",
                "max_heating_temperature",
                name="My solar cap",
                disabled_by=RegistryEntryDisabler.USER,
                hidden_by=RegistryEntryHider.USER,
            ),
            registry.add("number", "hot_water_hysteresis", name="My restart gap"),
            registry.add(
                "switch",
                "anti_legionella",
                name="Hygiene",
                disabled_by=RegistryEntryDisabler.INTEGRATION,
                hidden_by=RegistryEntryHider.INTEGRATION,
            ),
        ]
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(registry.removals, [obsolete.entity_id])
        self.assertEqual(set(registry.entities), {entity.entity_id for entity in retained})
        self.assertEqual(registry.updates, [])
        self.assertEqual(retained[2].name, "My solar cap")
        self.assertIs(retained[2].disabled_by, RegistryEntryDisabler.USER)
        self.assertIs(retained[2].hidden_by, RegistryEntryHider.USER)
        self.assertEqual(retained[3].name, "My restart gap")
        self.assertIs(retained[4].disabled_by, RegistryEntryDisabler.INTEGRATION)
        self.assertIs(coordinator.engine.state["overrides"]["heating"], journal)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        coordinator._async_save.reset_mock()
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(registry.removals, [obsolete.entity_id])
        coordinator._async_save.assert_not_awaited()

    async def test_version_four_failed_save_retries_supply_retirement_safely(self):
        registry, hass, entry, coordinator = self.context(version=4)
        obsolete = registry.add("number", "charge_supply_temperature")
        coordinator._async_save.side_effect = OSError("Storage unavailable")
        with self.assertRaises(OSError):
            await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 4)
        self.assertEqual(registry.removals, [obsolete.entity_id])
        coordinator._async_save.side_effect = None
        await migration.async_migrate_configuration_entities(hass, entry, coordinator)
        self.assertEqual(coordinator.engine.state["configuration_ui_version"], 5)
        self.assertEqual(registry.removals, [obsolete.entity_id])


if __name__ == "__main__":
    unittest.main()
