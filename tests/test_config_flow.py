"""Exercise Configure navigation and its no-unintended-writes contract.

Real voluptuous schemas run with narrow stand-ins for HA flow/selector objects.
This is not a full Home Assistant frontend or installation test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from enum import StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import voluptuous as vol

ROOT = Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus"


class HomeAssistantError(Exception):
    pass


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


class OptionsFlow:
    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


class NumberSelectorMode(StrEnum):
    BOX = "box"
    SLIDER = "slider"


class NumberSelector:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return vol.All(
            vol.Coerce(float), vol.Range(min=self.config["min"], max=self.config["max"])
        )(value)


class EntitySelector:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return vol.Schema(str)(value)


class SelectSelector:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        values = [option["value"] for option in self.config["options"]]
        return vol.In(values)(value)


def _module(name, **symbols):
    result = ModuleType(name)
    result.__dict__.update(symbols)
    return result


def _load_flow():
    config_entries = _module(
        "homeassistant.config_entries", ConfigFlow=ConfigFlow, OptionsFlow=OptionsFlow
    )
    selectors = SimpleNamespace(
        NumberSelector=NumberSelector,
        NumberSelectorConfig=dict,
        NumberSelectorMode=NumberSelectorMode,
        EntitySelector=EntitySelector,
        EntitySelectorConfig=dict,
        SelectSelector=SelectSelector,
        SelectSelectorConfig=dict,
    )
    stubs = {
        "homeassistant": _module("homeassistant", config_entries=config_entries),
        "homeassistant.config_entries": config_entries,
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.modbus": _module(
            "homeassistant.components.modbus", async_get_temporary_unit=object
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers", selector=selectors),
        "modbus_connection": _module(
            "modbus_connection", ModbusError=Exception, ModbusTcpParams=object
        ),
    }
    package = ModuleType("thermia_config_flow_smoke")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            f"{package.__name__}.config_flow", ROOT / "config_flow.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


FLOW = _load_flow()
NATIVE_SETTINGS = FLOW.NATIVE_SETTINGS
SWITCH_KEYS = [
    "heating_enabled",
    "passive_cooling_enabled",
    "hot_water_enabled",
    "native_boost_enabled",
    "auxiliary_heater_enabled",
    "anti_legionella_enabled",
]
NATIVE_UI_KEYS = [
    *(f"heat_curve_supply_{index}" for index in range(1, 8)),
    "min_supply_temperature",
    "max_supply_temperature",
    "passive_cooling_supply_target",
]


class Coordinator:
    def __init__(self):
        self.engine = SimpleNamespace(
            state={
                "heating_mode": "heat",
                "hot_water_mode": "auto",
                "hot_water_start": 45.0,
                "hot_water_target": 55.0,
            }
        )
        self.registers = {
            "heating_enabled": True,
            "hot_water_enabled": True,
            "passive_cooling_enabled": False,
            "immersion_heater": 2,
            "anti_legionella_enabled": True,
            "hot_water_boost": False,
            "heating_season_stop": 17,
            "min_supply_temperature": 20,
            "max_supply_temperature": 45,
            "passive_cooling_supply_target": 18,
            "selected_heat_curve": 35,
            **{f"heat_curve_supply_{i + 1}": 20 + i * 3 for i in range(7)},
        }
        self.applied = []
        self.failure = None

    def value(self, key):
        return self.registers.get(key)

    def temperature_source_details(self, location):
        return {
            "external_status": "valid" if location == "inside" else "not_selected",
            "fallback_status": "valid",
            "selected_sensor": "sensor.room" if location == "inside" else None,
            "effective_temperature": 22 if location == "inside" else 12,
        }

    async def async_configure_controls(self, options, *, control_changes):
        self.applied.append((dict(options), dict(control_changes)))
        if self.failure:
            raise HomeAssistantError(self.failure)


PREFERENCE_KEYS = [
    "control_mode",
    "heat_pump_model",
    "inside_sensor",
    "inside_humidity_sensor",
    "outside_sensor",
    "max_start_temperature",
    "max_hot_water_temperature",
    "sensor_timeout",
    "hysteresis",
]
DEVICE_SETTING_KEYS = [
    "heating_low_offset",
    "heating_vacation_temperature",
    "heating_vacation_cooling_offset",
    "max_heating_temperature",
    "heating_excess_hours",
    "heating_excess_heat_stop_offset",
    "cooling_humidity_enabled",
    "cooling_humidity_limit",
    "cooling_vacation_humidity_limit",
    "cooling_dew_point_margin",
    "hot_water_excess_hours",
    "hot_water_hysteresis",
    "hot_water_evening_start",
    "hot_water_evening_stop",
    "hot_water_low_hours",
    "heating_season_stop",
    *SWITCH_KEYS,
]
RETIRED_SETTING_KEYS = ["charge_supply_temperature"]


class ConfigureFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = Coordinator()
        self.flow = FLOW.ThermiaOptionsFlow()
        self.entry = SimpleNamespace(options={}, runtime_data=self.coordinator)
        self.flow.config_entry = self.entry

    @staticmethod
    def fields(form):
        return [key.schema for key in form["data_schema"].schema]

    async def open_controls(self, **preferences):
        first = await self.flow.async_step_init()
        values = first["data_schema"](preferences)
        return await self.flow.async_step_init(values)

    async def submit_controls(self, form, **changes):
        values = form["data_schema"](changes)
        return await self.flow.async_step_controls(values)

    async def test_approved_two_page_layout_has_one_location_per_setting(self):
        first = await self.flow.async_step_init()
        second = await self.open_controls()
        self.assertEqual(first["step_id"], "init")
        self.assertFalse(first["last_step"])
        self.assertEqual(self.fields(first), PREFERENCE_KEYS)
        self.assertEqual(second["step_id"], "controls")
        self.assertTrue(second["last_step"])
        self.assertEqual(self.fields(second), NATIVE_UI_KEYS + ["smart_grid_mode"])
        fields = self.fields(first) + self.fields(second)
        self.assertEqual(len(fields), len(set(fields)))
        self.assertTrue(set(fields).isdisjoint(DEVICE_SETTING_KEYS + RETIRED_SETTING_KEYS))
        self.assertEqual(self.coordinator.applied, [])

    async def test_external_ownership_is_explicit_and_preserved_without_native_edits(self):
        first = await self.flow.async_step_init()
        self.assertEqual(first["data_schema"]({})["control_mode"], "internal")
        with self.assertRaises(vol.Invalid):
            first["data_schema"]({"control_mode": "unknown"})
        second = await self.open_controls(control_mode="external")
        result = await self.submit_controls(second)
        self.assertEqual(result["data"]["control_mode"], "external")
        self.assertEqual(self.coordinator.applied[-1][1], {})
        self.entry.options = result["data"]
        first = await self.flow.async_step_init()
        self.assertEqual(first["data_schema"]({})["control_mode"], "external")

    async def test_native_sliders_use_supported_ranges_and_whole_degree_steps(self):
        second = await self.open_controls()
        for field, validator in second["data_schema"].schema.items():
            if field.schema == "smart_grid_mode":
                self.assertIsInstance(validator, SelectSelector)
                continue
            setting = NATIVE_SETTINGS[field.schema]
            self.assertIsInstance(validator, FLOW.NativeSettingSelector)
            self.assertEqual(validator.config["mode"], NumberSelectorMode.SLIDER)
            self.assertEqual(validator.config["min"], setting.min_value)
            self.assertEqual(validator.config["max"], setting.max_value)
            self.assertEqual(validator.config["step"], 1)
        for changes in (
            {"max_supply_temperature": 66},
            {"passive_cooling_supply_target": 31},
            {"heat_curve_supply_1": 20.5},
            {"min_supply_temperature": True},
            {"heat_curve_supply_2": float("nan")},
        ):
            with self.subTest(changes=changes), self.assertRaises(vol.Invalid):
                second["data_schema"](changes)
        self.assertEqual(self.coordinator.applied, [])

    async def test_device_controls_cannot_be_edited_through_either_form(self):
        first = await self.flow.async_step_init()
        second = await self.open_controls()
        for key in DEVICE_SETTING_KEYS + RETIRED_SETTING_KEYS:
            for form in (first, second):
                with self.subTest(key=key, page=form["step_id"]), self.assertRaises(vol.Invalid):
                    form["data_schema"]({key: 1})
        self.assertEqual(self.coordinator.applied, [])

    async def test_device_preferences_are_preserved_when_saving_sensor_options(self):
        saved = {
            "heat_pump_model": "Calibra E Cool 8",
            "heating_low_offset": 3,
            "heating_vacation_temperature": 16,
            "heating_vacation_cooling_offset": 2,
            "max_heating_temperature": 26,
            "heating_excess_hours": 18,
            "heating_excess_heat_stop_offset": 4,
            "cooling_humidity_enabled": True,
            "cooling_humidity_limit": 70,
            "cooling_vacation_humidity_limit": 60,
            "cooling_dew_point_margin": 3,
            "hot_water_excess_hours": 8,
            "hot_water_hysteresis": 4,
            "hot_water_evening_start": 36,
            "hot_water_evening_stop": 42,
            "hot_water_low_hours": 72,
            "heating_season_stop": 18,
            **{key: False for key in SWITCH_KEYS},
            "old_extra_setting": "retained",
            "configuration_version": 7,
        }
        self.entry.options = dict(saved)
        second = await self.open_controls(inside_sensor="sensor.room")
        result = await self.submit_controls(second)
        for key, value in saved.items():
            with self.subTest(key=key):
                self.assertEqual(result["data"][key], value)
        self.assertEqual(result["data"]["inside_sensor"], "sensor.room")
        self.assertEqual(self.coordinator.applied[-1][1], {})
        self.assertEqual(self.entry.options, saved)

    async def test_device_changes_while_form_is_open_are_not_overwritten(self):
        self.entry.options = {"max_heating_temperature": 23, "cooling_humidity_enabled": False}
        second = await self.open_controls(inside_sensor="sensor.room")
        self.entry.options.update(
            max_heating_temperature=27,
            cooling_humidity_enabled=True,
            cooling_humidity_limit=85,
            cooling_vacation_humidity_limit=55,
            hot_water_low_hours=48,
            hot_water_hysteresis=5,
            heating_season_stop=19,
            newly_saved_device_setting="retained",
            configuration_version=7,
        )
        result = await self.submit_controls(second, heat_curve_supply_1=21)
        for key, value in self.entry.options.items():
            with self.subTest(key=key):
                self.assertEqual(result["data"][key], value)
        self.assertEqual(result["data"]["inside_sensor"], "sensor.room")
        self.assertEqual(self.coordinator.applied[-1][1], {"heat_curve_supply_1": 21})

    async def test_removed_low_profile_is_not_validated_before_page_two(self):
        self.entry.options = {"hot_water_evening_start": 40, "hot_water_evening_stop": 35}
        second = await self.open_controls(inside_sensor="sensor.room")
        self.assertEqual(second["type"], "form")
        self.assertEqual(second["step_id"], "controls")
        self.assertEqual(self.flow._pending_options["hot_water_evening_start"], 40)
        self.assertEqual(self.flow._pending_options["hot_water_evening_stop"], 35)
        self.assertEqual(self.coordinator.applied, [])
        self.coordinator.failure = "Low Mode START must be below STOP"
        result = await self.submit_controls(second)
        self.assertEqual(result["errors"], {"base": "cannot_apply"})
        self.assertIn("below", result["description_placeholders"]["unavailable_controls"])

    async def test_optional_sensor_selectors_and_humidity_preference_clear_only_selection(self):
        self.entry.options = {
            "inside_humidity_sensor": "sensor.room_humidity",
            "cooling_humidity_enabled": True,
            "cooling_humidity_limit": 70,
            "cooling_vacation_humidity_limit": 60,
            "cooling_dew_point_margin": 3,
        }
        first = await self.flow.async_step_init()
        for name in ("inside_sensor", "inside_humidity_sensor", "outside_sensor"):
            marker = next(key for key in first["data_schema"].schema if key.schema == name)
            self.assertIsInstance(marker, vol.Optional)
            validator = first["data_schema"].schema[marker]
            self.assertEqual(
                validator.config,
                {
                    "domain": "sensor",
                    **({"device_class": "humidity"} if name == "inside_humidity_sensor" else {}),
                },
            )
        marker = next(
            key for key in first["data_schema"].schema if key.schema == "inside_humidity_sensor"
        )
        self.assertEqual(marker.description["suggested_value"], "sensor.room_humidity")
        second = await self.open_controls(inside_humidity_sensor="sensor.room_humidity")
        result = await self.submit_controls(second)
        self.assertEqual(result["data"]["inside_humidity_sensor"], "sensor.room_humidity")
        self.flow = FLOW.ThermiaOptionsFlow()
        self.flow.config_entry = self.entry
        second = await self.open_controls()
        result = await self.submit_controls(second)
        self.assertEqual(result["data"]["inside_humidity_sensor"], "")
        self.assertIs(result["data"]["cooling_humidity_enabled"], True)
        self.assertEqual(result["data"]["cooling_humidity_limit"], 70)
        self.assertEqual(result["data"]["cooling_vacation_humidity_limit"], 60)
        self.assertEqual(result["data"]["cooling_dew_point_margin"], 3)
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_source_status_and_read_only_curve_selection_are_visible_without_writes(self):
        first = await self.flow.async_step_init()
        status = first["description_placeholders"]["temperature_sources"]
        self.assertIn("Inside: External sensor in use (22°C)", status)
        self.assertIn("Outside: Thermia outside sensor in use (12°C)", status)
        second = await self.open_controls()
        self.assertNotIn("selected_heat_curve", self.fields(second))
        detail = second["description_placeholders"]["native_setting_status"]
        self.assertIn("Selected heat curve: 35 (read only)", detail)
        self.assertIn("seven heat-curve point sliders", detail)
        self.assertIn("Mixing Valve 1", detail)
        self.assertEqual(self.coordinator.applied, [])

    async def test_native_defaults_use_readback_and_only_deliberate_changes_are_applied(self):
        self.entry.options = {"heating_season_stop": 15, "heat_curve_supply_1": 19}
        second = await self.open_controls(inside_sensor="sensor.room")
        defaults = second["data_schema"]({})
        self.assertEqual(defaults["heat_curve_supply_1"], 20)
        self.assertEqual(defaults["min_supply_temperature"], 20)
        result = await self.submit_controls(
            second, min_supply_temperature=18, heat_curve_supply_1=21
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(
            self.coordinator.applied[-1][1],
            {"min_supply_temperature": 18, "heat_curve_supply_1": 21},
        )
        self.assertEqual(result["data"]["heating_season_stop"], 15)
        self.assertEqual(result["data"]["min_supply_temperature"], 18)
        self.assertEqual(result["data"]["heat_curve_supply_1"], 21)
        self.assertNotIn("heat_curve_supply_2", result["data"])
        self.assertEqual(result["data"]["inside_sensor"], "sensor.room")

    async def test_unsupported_and_invalid_numeric_settings_are_omitted(self):
        invalid_values = (None, True, float("nan"), float("inf"), "20")
        missing = NATIVE_UI_KEYS[: len(invalid_values)]
        for key, invalid in zip(missing, invalid_values, strict=True):
            self.coordinator.registers[key] = invalid
        second = await self.open_controls()
        self.assertTrue(set(missing).isdisjoint(self.fields(second)))
        detail = second["description_placeholders"]["native_setting_status"]
        self.assertIn("without a valid controller reading are omitted", detail)
        for key in missing:
            self.assertIn(NATIVE_SETTINGS[key].label, detail)
        self.assertNotIn(NATIVE_SETTINGS["heating_season_stop"].label, detail)
        result = await self.submit_controls(second)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_missing_native_readings_leave_power_preference_available_without_writes(self):
        self.coordinator.registers.update({key: None for key in NATIVE_SETTINGS})
        second = await self.open_controls()
        self.assertEqual(self.fields(second), ["smart_grid_mode"])
        self.assertIn(
            "Native temperature sliders are unavailable",
            second["description_placeholders"]["unavailable_controls"],
        )
        result = await self.submit_controls(second)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_unusual_native_readbacks_are_preserved_untouched_without_rounding(self):
        self.coordinator.registers["max_supply_temperature"] = 75
        self.coordinator.registers["heat_curve_supply_1"] = 20.25
        second = await self.open_controls()
        values = second["data_schema"]({})
        self.assertEqual(values["max_supply_temperature"], 75)
        self.assertEqual(values["heat_curve_supply_1"], 20.25)
        result = await self.submit_controls(second)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.coordinator.applied[-1][1], {})
        for changes in ({"max_supply_temperature": 74}, {"heat_curve_supply_1": 20.5}):
            with self.assertRaises(vol.Invalid):
                second["data_schema"](changes)
        self.assertEqual(len(self.coordinator.applied), 1)

    async def test_failed_native_write_preserves_attempted_slider_and_power_values_for_retry(self):
        second = await self.open_controls()
        self.coordinator.failure = "Controller did not confirm the supply temperature"
        result = await self.submit_controls(
            second, min_supply_temperature=18, smart_grid_mode="sg_ready"
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_apply"})
        self.assertEqual(result["data_schema"]({})["min_supply_temperature"], 18)
        self.assertEqual(result["data_schema"]({})["smart_grid_mode"], "sg_ready")
        self.assertEqual(self.coordinator.applied[-1][1], {"min_supply_temperature": 18})
        self.assertEqual(self.coordinator.applied[-1][0]["smart_grid_mode"], "sg_ready")
        self.assertIn("did not confirm", result["description_placeholders"]["unavailable_controls"])
        self.assertEqual(self.entry.options, {})
        self.coordinator.failure = None
        result = await self.submit_controls(result)
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["smart_grid_mode"], "sg_ready")

    async def test_power_control_preference_saves_on_page_two_without_native_requests(self):
        for mode in ("disabled", "sg_ready", "power_limit"):
            with self.subTest(mode=mode):
                self.flow = FLOW.ThermiaOptionsFlow()
                self.flow.config_entry = self.entry
                second = await self.open_controls(inside_sensor="sensor.room")
                result = await self.submit_controls(second, smart_grid_mode=mode)
                self.assertEqual(result["data"]["smart_grid_mode"], mode)
                self.assertEqual(self.coordinator.applied[-1][1], {})
        with self.assertRaises(vol.Invalid):
            second["data_schema"]({"smart_grid_mode": "unsupported"})
        first = await self.flow.async_step_init()
        with self.assertRaises(vol.Invalid):
            first["data_schema"]({"smart_grid_mode": "sg_ready"})

    async def test_existing_power_preference_is_retained_when_native_sliders_are_unchanged(self):
        self.entry.options = {"smart_grid_mode": "power_limit"}
        second = await self.open_controls()
        self.assertEqual(second["data_schema"]({})["smart_grid_mode"], "power_limit")
        result = await self.submit_controls(second)
        self.assertEqual(result["data"]["smart_grid_mode"], "power_limit")
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_invalid_active_profile_is_rejected_transactionally_without_saving(self):
        self.entry.options = {
            "heating_vacation_temperature": 17,
            "heating_vacation_cooling_offset": 0,
        }
        original = dict(self.entry.options)
        second = await self.open_controls(inside_sensor="sensor.room")
        self.coordinator.failure = (
            "Vacation heating target must stay below the resulting cooling target"
        )
        result = await self.submit_controls(second)
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_apply"})
        self.assertIn("below", result["description_placeholders"]["unavailable_controls"])
        self.assertEqual(self.entry.options, original)
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_configuration_text_matches_fields_and_directs_device_settings_to_device_page(
        self,
    ):
        strings = json.loads((ROOT / "strings.json").read_text())
        translation = json.loads((ROOT / "translations" / "en.json").read_text())
        self.assertEqual(strings, translation)
        self.assertNotIn("Excessive Energy", json.dumps(strings))
        first = await self.flow.async_step_init()
        second = await self.open_controls()
        for form in (first, second):
            text = strings["options"]["step"][form["step_id"]]
            self.assertEqual(list(text["data"]), self.fields(form))
            self.assertEqual(list(text["data_description"]), self.fields(form))
            for key in form["description_placeholders"]:
                self.assertIn("{" + key + "}", text["description"])
        init = strings["options"]["step"]["init"]
        self.assertIn("device page under Configuration", init["description"])
        self.assertIn("pauses cooling", init["data_description"]["inside_humidity_sensor"])
        self.assertIn(
            "hardware capability limit", init["data_description"]["max_start_temperature"]
        )
        self.assertIn(
            "hardware capability limit", init["data_description"]["max_hot_water_temperature"]
        )
        self.assertIn("temperature and humidity", init["data_description"]["sensor_timeout"])
        self.assertIn("restart gap is also on the device page", init["description"])
        self.assertIn("default 2°C", init["description"])
        self.assertIn("START 55°C and STOP 60°C", init["description"])
        self.assertIn("lower of this limit", init["data_description"]["max_start_temperature"])
        self.assertIn("check them for your model", init["description"])
        self.assertNotIn("requires it to allow", init["data_description"]["max_start_temperature"])
        controls = strings["options"]["step"]["controls"]
        self.assertIn("Smart Grid", controls["title"])
        self.assertIn("integration software limits", controls["description"])
        self.assertIn("device page under Configuration", controls["description"])
        self.assertIn("does not configure", controls["data_description"]["smart_grid_mode"])

    async def test_legacy_opt_in_and_old_boiler_sliders_are_not_offered_or_reapplied(self):
        self.entry.options = {
            "enable_undocumented_controls": False,
            "hot_water_boost_start": 55,
            "hot_water_boost_stop": 60,
            "hot_water_start": 45,
            "hot_water_target": 55,
        }
        first = await self.flow.async_step_init()
        second = await self.open_controls()
        fields = set(self.fields(first) + self.fields(second))
        self.assertTrue(
            fields.isdisjoint(
                {
                    "enable_undocumented_controls",
                    "hot_water_start",
                    "hot_water_target",
                    "hot_water_boost_start",
                    "hot_water_boost_stop",
                }
            )
        )
        result = await self.submit_controls(second)
        self.assertIsNone(result["data"]["hot_water_boost_start"])
        self.assertIsNone(result["data"]["hot_water_boost_stop"])
        self.assertEqual(result["data"]["hot_water_start"], 45)
        self.assertEqual(result["data"]["hot_water_target"], 55)
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_restart_gap_is_only_on_the_device_page_and_defaults_to_two(self):
        first = await self.flow.async_step_init()
        self.assertNotIn("hot_water_hysteresis", self.fields(first))
        self.assertNotIn("hot_water_hysteresis", FLOW.PREFERENCE_KEYS)
        second = await self.open_controls()
        self.assertNotIn("hot_water_hysteresis", self.fields(second))
        result = await self.submit_controls(second)
        self.assertEqual(result["data"]["hot_water_hysteresis"], 2)
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_maxima_have_model_defaults_and_keep_optional_whole_degree_edits(self):
        first = await self.flow.async_step_init()
        defaults = first["data_schema"]({})
        for key, lower, upper in (
            ("max_start_temperature", 20, 79),
            ("max_hot_water_temperature", 30, 80),
        ):
            field = next(marker for marker in first["data_schema"].schema if marker.schema == key)
            self.assertIsInstance(field, vol.Optional)
            validator = first["data_schema"].schema[field]
            self.assertEqual(validator.config["min"], lower)
            self.assertEqual(validator.config["max"], upper)
            self.assertEqual(validator.config["step"], 1)
            self.assertEqual(field.description["suggested_value"], FLOW.DEFAULT_OPTIONS[key])
            self.assertNotIn(key, defaults)
            for value in (lower, upper):
                self.assertEqual(first["data_schema"]({key: value})[key], value)
            for invalid in (lower - 1, upper + 1, lower + 0.5, True, float("inf")):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(vol.Invalid):
                    first["data_schema"]({key: invalid})
        self.assertEqual(self.coordinator.applied, [])

    async def test_empty_legacy_maxima_get_defaults_and_explicit_custom_maxima_are_retained(self):
        for saved, selected, expected in (
            ({}, {}, (55, 60)),
            (
                {
                    "max_start_temperature": None,
                    "max_hot_water_temperature": None,
                    "configuration_version": 6,
                },
                {},
                (55, 60),
            ),
            (
                {
                    "max_start_temperature": 60,
                    "max_hot_water_temperature": 65,
                    "configuration_version": 6,
                },
                {"max_start_temperature": 60, "max_hot_water_temperature": 65},
                (60, 65),
            ),
        ):
            with self.subTest(saved=saved):
                self.flow = FLOW.ThermiaOptionsFlow()
                self.flow.config_entry = self.entry
                self.entry.options = saved
                first = await self.flow.async_step_init()
                for key, value in zip(
                    ("max_start_temperature", "max_hot_water_temperature"), expected, strict=True
                ):
                    marker = next(
                        field for field in first["data_schema"].schema if field.schema == key
                    )
                    self.assertEqual(marker.description["suggested_value"], value)
                second = await self.open_controls(**selected)
                result = await self.submit_controls(second)
                self.assertEqual(
                    (
                        result["data"]["max_start_temperature"],
                        result["data"]["max_hot_water_temperature"],
                    ),
                    expected,
                )
                self.assertEqual(result["data"]["configuration_version"], 7)
                self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_unchanged_legacy_fractional_preferences_are_not_rounded_or_written(self):
        self.entry.options = {
            "max_heating_temperature": 23.5,
            "charge_supply_temperature": 45.5,
            "hot_water_hysteresis": 3.5,
            "configuration_version": 7,
        }
        first = await self.flow.async_step_init()
        self.assertNotIn("hot_water_hysteresis", self.fields(first))
        second = await self.open_controls()
        result = await self.submit_controls(second)
        for key in ("max_heating_temperature", "hot_water_hysteresis"):
            self.assertEqual(result["data"][key], self.entry.options[key])
        self.assertNotIn("charge_supply_temperature", result["data"])
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_legacy_fractional_gap_and_explicit_freshness_are_preserved(self):
        self.entry.options = {
            "hot_water_hysteresis": 1.5,
            "sensor_timeout": 900,
            "configuration_version": 3,
        }
        first = await self.flow.async_step_init()
        defaults = first["data_schema"]({})
        self.assertNotIn("hot_water_hysteresis", defaults)
        self.assertEqual(defaults["sensor_timeout"], 900)
        second = await self.open_controls()
        result = await self.submit_controls(second)
        self.assertEqual(result["data"]["configuration_version"], 7)
        self.assertEqual(result["data"]["hot_water_hysteresis"], 1.5)
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_old_default_gap_migrates_but_current_explicit_three_is_preserved(self):
        for version, expected in ((5, 2), (6, 3), (7, 3)):
            with self.subTest(version=version):
                self.flow = FLOW.ThermiaOptionsFlow()
                self.flow.config_entry = self.entry
                self.entry.options = {"hot_water_hysteresis": 3, "configuration_version": version}
                second = await self.open_controls()
                result = await self.submit_controls(second)
                self.assertEqual(result["data"]["hot_water_hysteresis"], expected)
                self.assertEqual(result["data"]["configuration_version"], 7)
                self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_unchanged_save_does_not_reapply_saved_native_flags(self):
        self.entry.options = {key: False for key in SWITCH_KEYS} | {"native_boost_enabled": True}
        second = await self.open_controls(
            inside_sensor="sensor.room", outside_sensor="sensor.garden"
        )
        result = await self.submit_controls(second)
        self.assertEqual(self.coordinator.applied[-1][1], {})
        for key, value in self.entry.options.items():
            self.assertEqual(result["data"][key], value)
        for key in NATIVE_UI_KEYS:
            self.assertNotIn(key, result["data"])

    async def test_unloaded_coordinator_cannot_save_unconfirmed_preferences(self):
        self.entry.runtime_data = None
        first = await self.flow.async_step_init()
        self.assertIn("unloaded", first["description_placeholders"]["temperature_sources"])
        second = await self.open_controls()
        self.assertEqual(self.fields(second), ["smart_grid_mode"])
        result = await self.submit_controls(second)
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_apply"})
        self.assertEqual(self.coordinator.applied, [])

    async def test_optional_fields_can_be_retained_then_cleared_without_losing_device_preferences(
        self,
    ):
        self.entry.options = {
            "inside_sensor": "sensor.room",
            "inside_humidity_sensor": "sensor.room_humidity",
            "outside_sensor": "sensor.garden",
            "max_hot_water_temperature": 65,
            "max_start_temperature": 60,
            "auxiliary_heater_enabled": False,
            "hot_water_low_hours": 48,
            "old_extra_setting": "retained",
        }
        selected = {
            key: self.entry.options[key]
            for key in (
                "inside_sensor",
                "inside_humidity_sensor",
                "outside_sensor",
                "max_hot_water_temperature",
                "max_start_temperature",
            )
        }
        second = await self.open_controls(**selected)
        result = await self.submit_controls(second)
        for key, value in self.entry.options.items():
            self.assertEqual(result["data"][key], value)
        self.flow = FLOW.ThermiaOptionsFlow()
        self.flow.config_entry = self.entry
        second = await self.open_controls()
        result = await self.submit_controls(second)
        for key in selected:
            self.assertEqual(result["data"][key], FLOW.DEFAULT_OPTIONS[key])
        for key in ("auxiliary_heater_enabled", "hot_water_low_hours", "old_extra_setting"):
            self.assertEqual(result["data"][key], self.entry.options[key])
        self.assertEqual(self.coordinator.applied[-1][1], {})

    async def test_room_restart_margin_remains_fractional_and_distinct_from_tank_gap(self):
        first = await self.flow.async_step_init()
        marker = next(key for key in first["data_schema"].schema if key.schema == "hysteresis")
        validator = first["data_schema"].schema[marker]
        self.assertEqual(validator.config["step"], 0.1)
        self.assertEqual(first["data_schema"]({})["hysteresis"], 0.3)
        for value in (0.1, 0.3, 2):
            self.assertEqual(first["data_schema"]({"hysteresis": value})["hysteresis"], value)
        for invalid in (0, 2.1):
            with self.subTest(invalid=invalid), self.assertRaises(vol.Invalid):
                first["data_schema"]({"hysteresis": invalid})

    async def test_sensor_timeout_default_and_explicit_old_values_keep_migration_contract(self):
        for saved, expected in (
            ({}, 0),
            ({"sensor_timeout": 900}, 0),
            ({"sensor_timeout": 900, "configuration_version": 3}, 900),
            ({"sensor_timeout": 1200}, 1200),
        ):
            with self.subTest(saved=saved):
                self.flow = FLOW.ThermiaOptionsFlow()
                self.flow.config_entry = self.entry
                self.entry.options = saved
                first = await self.flow.async_step_init()
                self.assertEqual(first["data_schema"]({})["sensor_timeout"], expected)
                second = await self.open_controls()
                result = await self.submit_controls(second)
                self.assertEqual(result["data"]["sensor_timeout"], expected)
        with self.assertRaises(vol.Invalid):
            first["data_schema"]({"sensor_timeout": -1})


class ConnectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_domain_entry_blocks_a_second_controller_before_probing(self):
        flow = FLOW.ThermiaConfigFlow()
        old_entry = SimpleNamespace(data={"host": " Pump.LOCAL "})
        entries = Mock(return_value=[old_entry])
        flow.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=entries))
        flow.async_abort = lambda **kwargs: {"type": "abort", **kwargs}
        with patch.object(FLOW, "async_get_temporary_unit") as probe:
            result = await flow.async_step_user({"host": "pump.local", "port": 502, "unit_id": 1})
        self.assertEqual(result, {"type": "abort", "reason": "legacy_entry_exists"})
        entries.assert_called_once_with("thermia_pv")
        probe.assert_not_called()

    async def test_another_pump_port_or_unit_does_not_block_the_new_connection(self):
        for old_data in (
            {"host": "different.local"},
            {"host": "pump.local", "port": 503},
            {"host": "pump.local", "unit_id": 2},
        ):
            with self.subTest(old_data=old_data):
                flow = FLOW.ThermiaConfigFlow()
                flow.hass = SimpleNamespace(
                    config_entries=SimpleNamespace(
                        async_entries=lambda domain: [SimpleNamespace(data=old_data)]
                    )
                )
                flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
                with (
                    patch.object(FLOW, "ModbusTcpParams", side_effect=lambda **data: data),
                    patch.object(
                        FLOW, "async_get_temporary_unit", side_effect=ValueError("Offline")
                    ) as probe,
                ):
                    result = await flow.async_step_user(
                        {"host": "pump.local", "port": 502, "unit_id": 1}
                    )
                probe.assert_called_once()
                self.assertEqual(result["type"], "form")
                self.assertEqual(result["errors"], {"base": "cannot_connect"})


if __name__ == "__main__":
    unittest.main()
