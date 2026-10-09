"""Device-label checks using narrow stands-ins for the HA registry API.

These verify model-only registry updates and retained integration identities;
they are not a Home Assistant runtime test.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus"


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _module(name, **symbols):
    module = ModuleType(name)
    module.__dict__.update(symbols)
    return module


def _load_entity():
    package = _module("thermia_device_label_test")
    package.__path__ = [str(COMPONENT)]
    registry = _module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=dict,
        async_get=lambda hass: hass.registry,
    )
    stubs = {
        package.__name__: package,
        "homeassistant": _module("homeassistant"),
        "homeassistant.helpers": _module("homeassistant.helpers", device_registry=registry),
        registry.__name__: registry,
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity
        ),
    }
    with patch.dict(sys.modules, stubs):
        entity = importlib.import_module(f"{package.__name__}.entity")
        constants = importlib.import_module(f"{package.__name__}.const")
    return entity, constants


ENTITY, CONST = _load_entity()


class DeviceLabelTests(unittest.TestCase):
    def test_default_hot_water_caps_fill_missing_or_unset_fields_without_replacing_custom_caps(
        self,
    ):
        for options, expected in (
            ({}, (55, 60)),
            ({"max_start_temperature": None, "max_hot_water_temperature": None}, (55, 60)),
            ({"configuration_version": 7, "max_start_temperature": None}, (55, 60)),
            (
                {
                    "configuration_version": 6,
                    "max_start_temperature": 65,
                    "max_hot_water_temperature": 70,
                },
                (65, 70),
            ),
            (
                {
                    "configuration_version": 7,
                    "max_start_temperature": 60,
                    "max_hot_water_temperature": 60,
                },
                (60, 60),
            ),
        ):
            with self.subTest(options=options):
                normalized = CONST.normalize_options(options)
                self.assertEqual(
                    (normalized["max_start_temperature"], normalized["max_hot_water_temperature"]),
                    expected,
                )
                self.assertEqual(normalized["configuration_version"], 7)
                self.assertEqual(CONST.normalize_options(normalized), normalized)

    def test_one_degree_custom_gap_and_legacy_fractional_timers_survive_v7_migration(self):
        for version in (0, 3, 6, 7):
            with self.subTest(version=version):
                options = {
                    "configuration_version": version,
                    "hot_water_hysteresis": 1,
                    "heating_excess_hours": 1.5,
                    "hot_water_excess_hours": 0.5,
                    "hot_water_low_hours": 12.25,
                }
                normalized = CONST.normalize_options(options)
                self.assertEqual(normalized["hot_water_hysteresis"], 1)
                for key in (
                    "heating_excess_hours",
                    "hot_water_excess_hours",
                    "hot_water_low_hours",
                ):
                    self.assertEqual(normalized[key], options[key])

    def test_water_gap_default_migration_and_retired_supply_setting_removal(self):
        for options, expected in (
            ({}, 2),
            ({"configuration_version": 5, "hot_water_hysteresis": 3}, 2),
            ({"configuration_version": 5, "hot_water_hysteresis": 4}, 4),
            ({"configuration_version": 5, "hot_water_hysteresis": 3.5}, 3.5),
            ({"configuration_version": 6, "hot_water_hysteresis": 3}, 3),
        ):
            with self.subTest(options=options):
                normalized = CONST.normalize_options(options | {"charge_supply_temperature": 35})
                self.assertEqual(normalized["hot_water_hysteresis"], expected)
                self.assertEqual(normalized["configuration_version"], 7)
                self.assertNotIn("charge_supply_temperature", normalized)
                self.assertEqual(CONST.normalize_options(normalized), normalized)
        self.assertNotIn("charge_supply_temperature", CONST.DEFAULT_OPTIONS)

    def test_configuration_upgrade_keeps_fractional_legacy_durations_exact(self):
        legacy = {
            "configuration_version": 5,
            "heating_excess_hours": 1.5,
            "hot_water_excess_hours": 0.5,
            "hot_water_low_hours": 12.25,
        }
        normalized = CONST.normalize_options(legacy)
        for key in ("heating_excess_hours", "hot_water_excess_hours", "hot_water_low_hours"):
            self.assertEqual(normalized[key], legacy[key])

    def test_humidity_defaults_and_legacy_default_migration_are_independent(self):
        for options in (
            {},
            {"cooling_humidity_limit": 65},
            {"cooling_humidity_limit": 65.0, "configuration_version": 4},
        ):
            with self.subTest(options=options):
                normalized = CONST.normalize_options(options)
                self.assertEqual(normalized["cooling_humidity_limit"], 80)
                self.assertEqual(normalized["cooling_vacation_humidity_limit"], 65)
                self.assertEqual(normalized["configuration_version"], 7)
                self.assertEqual(CONST.normalize_options(normalized), normalized)

    def test_humidity_custom_limits_survive_migration_and_current_default_sized_choice(self):
        for options, normal, vacation in (
            ({"cooling_humidity_limit": 72, "configuration_version": 4}, 72, 65),
            ({"cooling_humidity_limit": 65, "configuration_version": 5}, 65, 65),
            (
                {
                    "cooling_humidity_limit": 65,
                    "cooling_vacation_humidity_limit": 70,
                    "configuration_version": 4,
                },
                80,
                70,
            ),
        ):
            with self.subTest(options=options):
                normalized = CONST.normalize_options(options)
                self.assertEqual(normalized["cooling_humidity_limit"], normal)
                self.assertEqual(normalized["cooling_vacation_humidity_limit"], vacation)
                self.assertEqual(CONST.normalize_options(normalized), normalized)

    def test_humidity_migration_retains_previous_timeout_and_water_gap_upgrades(self):
        normalized = CONST.normalize_options(
            {
                "cooling_humidity_limit": 65,
                "sensor_timeout": 900,
                "hot_water_hysteresis": 1.5,
                "configuration_version": 2,
            }
        )
        self.assertEqual(normalized["sensor_timeout"], 0)
        self.assertEqual(normalized["hot_water_hysteresis"], 1.5)
        self.assertEqual(normalized["cooling_humidity_limit"], 80)
        current = CONST.normalize_options(
            {
                "cooling_humidity_limit": 65,
                "sensor_timeout": 900,
                "hot_water_hysteresis": 2.5,
                "configuration_version": 5,
            }
        )
        self.assertEqual(current["sensor_timeout"], 900)
        self.assertEqual(current["hot_water_hysteresis"], 2.5)
        self.assertEqual(current["cooling_humidity_limit"], 65)

    def entry(self, options=None, *, unique_id="192.0.2.1:502:1"):
        return SimpleNamespace(
            entry_id="pump-entry",
            unique_id=unique_id,
            title="Thermia Genesis Modbus",
            options=options or {},
        )

    def test_new_and_legacy_entries_use_generic_model_without_a_selected_label(self):
        for options in ({}, {"heat_pump_model": " "}, {"heat_pump_model": None}):
            with self.subTest(options=options):
                coordinator = SimpleNamespace(entry=self.entry(options))
                entity = ENTITY.ThermiaEntity(coordinator, "indoor_temperature", "Indoor")
                self.assertEqual(entity._attr_device_info["model"], "Genesis Heat Pump")

    def test_custom_model_is_trimmed_without_changing_device_or_entity_identity(self):
        original = ENTITY.ThermiaEntity(SimpleNamespace(entry=self.entry()), "heating", "Heating")
        updated = ENTITY.ThermiaEntity(
            SimpleNamespace(entry=self.entry({"heat_pump_model": "  Atlas 12 400V  "})),
            "heating",
            "Heating",
        )
        self.assertEqual(updated._attr_device_info["model"], "Atlas 12 400V")
        self.assertEqual(updated._attr_unique_id, original._attr_unique_id)
        self.assertEqual(
            updated._attr_device_info["identifiers"], original._attr_device_info["identifiers"]
        )
        self.assertEqual(updated._attr_device_info["name"], original._attr_device_info["name"])

    def test_registry_update_changes_only_model_and_preserves_user_name(self):
        entry = self.entry({"heat_pump_model": "  Calibra Eco 8  "})
        device = SimpleNamespace(
            id="pump-device", model="Genesis Heat Pump", name_by_user="Basement"
        )
        registry = SimpleNamespace(
            async_get_device_by_identifier=Mock(return_value=device),
            async_update_device=Mock(),
        )
        ENTITY.async_update_device_model(SimpleNamespace(registry=registry), entry)
        registry.async_get_device_by_identifier.assert_called_once_with(
            (CONST.DOMAIN, entry.unique_id), entry.entry_id
        )
        registry.async_update_device.assert_called_once_with("pump-device", model="Calibra Eco 8")
        self.assertEqual(device.name_by_user, "Basement")

    def test_registry_update_uses_entry_id_fallback_and_skips_unknown_device(self):
        entry = self.entry({"heat_pump_model": "Atlas"}, unique_id=None)
        registry = SimpleNamespace(
            async_get_device_by_identifier=Mock(return_value=None),
            async_update_device=Mock(),
        )
        ENTITY.async_update_device_model(SimpleNamespace(registry=registry), entry)
        registry.async_get_device_by_identifier.assert_called_once_with(
            (CONST.DOMAIN, entry.entry_id), entry.entry_id
        )
        registry.async_update_device.assert_not_called()

    def test_unchanged_model_does_not_rewrite_registry(self):
        entry = self.entry({"heat_pump_model": " Atlas "})
        registry = SimpleNamespace(
            async_get_device_by_identifier=Mock(
                return_value=SimpleNamespace(id="pump", model="Atlas")
            ),
            async_update_device=Mock(),
        )
        ENTITY.async_update_device_model(SimpleNamespace(registry=registry), entry)
        registry.async_update_device.assert_not_called()

    def test_model_edit_does_not_select_or_modify_register_control_settings(self):
        original = CONST.normalize_options({"configuration_version": 3})
        custom = CONST.normalize_options(
            {"configuration_version": 3, "heat_pump_model": "  Diplomat Inverter  "}
        )
        self.assertEqual(custom.pop("heat_pump_model"), "Diplomat Inverter")
        original.pop("heat_pump_model")
        self.assertEqual(custom, original)
