"""Control-platform smoke tests against the real pure control policy.

These small Home Assistant API stand-ins let the policy/entity boundary run on
Python 3.12. They are not a full Home Assistant runtime test. Import locations,
feature names and enums were separately checked against Core 2026.9.4 sources.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import unittest
from abc import ABCMeta
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import IntFlag, StrEnum
from functools import cached_property
from operator import attrgetter
from pathlib import Path
from types import FunctionType, ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

COMPONENT = Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus"


class HVACMode(StrEnum):
    OFF = "off"
    AUTO = "auto"
    HEAT = "heat"
    COOL = "cool"
    HEAT_COOL = "heat_cool"


class HVACAction(StrEnum):
    OFF = "off"
    HEATING = "heating"
    COOLING = "cooling"
    IDLE = "idle"


class ClimateEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    TARGET_TEMPERATURE_RANGE = 2
    TARGET_HUMIDITY = 4
    FAN_MODE = 8
    PRESET_MODE = 16
    SWING_MODE = 32
    TURN_OFF = 128
    TURN_ON = 256
    SWING_HORIZONTAL_MODE = 512


class WaterHeaterEntityFeature(IntFlag):
    TARGET_TEMPERATURE = 1
    OPERATION_MODE = 2
    ON_OFF = 8


class Entity:
    def __init__(self):
        self._on_remove = []
        self.state_updates = []
        self.attribute_updates = []

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, remove):
        self._on_remove.append(remove)

    def _call_on_remove_callbacks(self):
        while self._on_remove:
            self._on_remove.pop()()

    def async_write_ha_state(self):
        self.state_updates.append(self.native_value)
        self.attribute_updates.append(deepcopy(self.extra_state_attributes))

    @property
    def available(self):
        return True

    @property
    def min_temp(self):
        return self._attr_min_temp

    @property
    def native_min_value(self):
        return self._attr_native_min_value

    @property
    def native_max_value(self):
        return self._attr_native_max_value


class ClimateEntity(Entity):
    def __init__(self):
        super().__init__()
        self.force_update_publications = []

    def async_write_ha_state(self):
        self.state_updates.append(self.hvac_mode.value)
        self.attribute_updates.append(
            {
                "current_temperature": self.current_temperature,
                "target_temp_low": self.target_temperature_low,
                "target_temp_high": self.target_temperature_high,
                **deepcopy(self.extra_state_attributes),
            }
        )
        self.force_update_publications.append(self.force_update)

    @property
    def force_update(self):
        return getattr(self, "_attr_force_update", False)

    @property
    def supported_features(self):
        return self._attr_supported_features

    @property
    def target_temperature(self):
        return None

    @property
    def max_temp(self):
        return self._attr_max_temp

    @property
    def precision(self):
        return self._attr_precision

    @property
    def target_temperature_step(self):
        return self._attr_target_temperature_step

    @property
    def temperature_unit(self):
        return self._attr_temperature_unit

    @property
    def current_humidity(self):
        return None


class WaterHeaterEntity(Entity):
    pass


class NumberEntity(Entity):
    pass


class SwitchEntity(Entity):
    pass


class ButtonEntity(Entity):
    pass


class SensorEntity(Entity):
    pass


class BinarySensorEntity(Entity):
    pass


class CoordinatorEntity(Entity):
    def __init__(self, coordinator):
        super().__init__()
        self.coordinator = coordinator

    @property
    def available(self):
        return self.coordinator.last_update_success


class HomeAssistantError(Exception):
    pass


class EntityCategory(StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class UnitOfTemperature(StrEnum):
    CELSIUS = "°C"
    FAHRENHEIT = "°F"


class TemperatureConverter:
    @staticmethod
    def convert(value, from_unit, to_unit):
        if from_unit == to_unit:
            return value
        if from_unit == UnitOfTemperature.CELSIUS and to_unit == UnitOfTemperature.FAHRENHEIT:
            return value * 1.8 + 32.0
        if from_unit == UnitOfTemperature.FAHRENHEIT and to_unit == UnitOfTemperature.CELSIUS:
            return (value - 32.0) / 1.8
        raise HomeAssistantError("Unsupported temperature unit")


class UnitOfTime(StrEnum):
    HOURS = "h"


class UnitOfRatio(StrEnum):
    PERCENTAGE = "%"


class NumberDeviceClass(StrEnum):
    TEMPERATURE = "temperature"
    TEMPERATURE_DELTA = "temperature_delta"
    DURATION = "duration"
    HUMIDITY = "humidity"


class NumberMode(StrEnum):
    BOX = "box"
    SLIDER = "slider"


class SensorDeviceClass(StrEnum):
    HUMIDITY = "humidity"
    TEMPERATURE = "temperature"
    TEMPERATURE_DELTA = "temperature_delta"
    POWER = "power"
    ENERGY = "energy"
    CURRENT = "current"
    VOLTAGE = "voltage"
    PRESSURE = "pressure"
    DURATION = "duration"


class SensorStateClass(StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class BinarySensorDeviceClass(StrEnum):
    PROBLEM = "problem"
    RUNNING = "running"


def _module(name, **symbols):
    module = ModuleType(name)
    module.__dict__.update(symbols)
    return module


def callback(function):
    function._hass_callback = True
    return function


def _load_platforms():
    """Supply only actual public names, including the exact feature import."""
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.climate": _module(
            "homeassistant.components.climate", ClimateEntity=ClimateEntity
        ),
        "homeassistant.components.climate.const": _module(
            "homeassistant.components.climate.const",
            HVACMode=HVACMode,
            HVACAction=HVACAction,
            ClimateEntityFeature=ClimateEntityFeature,
            ATTR_HVAC_MODE="hvac_mode",
            ATTR_TARGET_TEMP_LOW="target_temp_low",
            ATTR_TARGET_TEMP_HIGH="target_temp_high",
        ),
        "homeassistant.components.water_heater": _module(
            "homeassistant.components.water_heater",
            WaterHeaterEntity=WaterHeaterEntity,
            WaterHeaterEntityFeature=WaterHeaterEntityFeature,
        ),
        # Core 2026.9.4 does not export WaterHeaterEntityFeature from const.
        "homeassistant.components.water_heater.const": _module(
            "homeassistant.components.water_heater.const"
        ),
        "homeassistant.components.number": _module(
            "homeassistant.components.number",
            NumberEntity=NumberEntity,
            NumberDeviceClass=NumberDeviceClass,
            NumberMode=NumberMode,
        ),
        "homeassistant.components.switch": _module(
            "homeassistant.components.switch", SwitchEntity=SwitchEntity
        ),
        "homeassistant.components.button": _module(
            "homeassistant.components.button", ButtonEntity=ButtonEntity
        ),
        "homeassistant.components.sensor": _module(
            "homeassistant.components.sensor",
            SensorEntity=SensorEntity,
            SensorDeviceClass=SensorDeviceClass,
            SensorStateClass=SensorStateClass,
        ),
        "homeassistant.components.binary_sensor": _module(
            "homeassistant.components.binary_sensor",
            BinarySensorEntity=BinarySensorEntity,
            BinarySensorDeviceClass=BinarySensorDeviceClass,
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=SimpleNamespace
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            ATTR_TEMPERATURE="temperature",
            PRECISION_TENTHS=0.1,
            STATE_OFF="off",
            UnitOfTemperature=UnitOfTemperature,
            UnitOfTime=UnitOfTime,
            UnitOfRatio=UnitOfRatio,
            EntityCategory=EntityCategory,
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=SimpleNamespace, callback=callback
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.event": _module(
            "homeassistant.helpers.event",
            async_track_time_interval=lambda *args, **kwargs: lambda: None,
        ),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
        ),
        "homeassistant.util": _module("homeassistant.util"),
        "homeassistant.util.unit_conversion": _module(
            "homeassistant.util.unit_conversion", TemperatureConverter=TemperatureConverter
        ),
        "homeassistant.helpers.device_registry": _module(
            "homeassistant.helpers.device_registry", DeviceInfo=dict
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity
        ),
    }
    package_name = "thermia_platform_smoke_test"
    package = ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    sys.modules[package_name] = package
    output = {}
    with patch.dict(sys.modules, stubs):
        for name in (
            "const",
            "entity",
            "native_settings",
            "control",
            "registers",
            "excess_time",
            "control_guide",
            "climate",
            "water_heater",
            "number",
            "switch",
            "button",
            "sensor",
            "binary_sensor",
        ):
            module_name = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(module_name, COMPONENT / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            output[name] = module
    return output


MODULES = _load_platforms()


class Coordinator:
    """Real policy, simulated register transport and reported activity."""

    def __init__(self):
        self.entry = SimpleNamespace(
            entry_id="pump-id", unique_id="192.0.2.1:502:1", title="Thermia PV", options={}
        )
        self.last_update_success = True
        self.effective_inside = 22.0
        self.effective_inside_humidity = None
        self.effective_outside = 12.0
        self.inside_source = "sensor.living_room"
        self.outside_source = "Thermia physical outside sensor"
        self.status = "Idle"
        self.active_demands = []
        self.alarm_classes = []
        self.device = SimpleNamespace(specs=MODULES["registers"].REGISTERS_BY_KEY)
        self.registers = {
            "heating_enabled": True,
            "passive_cooling_enabled": False,
            "hot_water_enabled": True,
            "hot_water_boost": False,
            "hot_water_start": 45,
            "hot_water_stop": 55,
            "hot_water_weighted_temperature": 50,
            "hot_water_top_temperature": 51,
            "fixed_supply_enabled": False,
            "fixed_supply_target": 30,
            "comfort_wheel": 20,
            "max_supply_temperature": 45,
            "min_supply_temperature": 20,
            "heating_season_stop": 17,
            "passive_cooling_supply_target": 18,
            "selected_heat_curve": 35,
            **{f"heat_curve_supply_{i + 1}": 20 + i * 3 for i in range(7)},
            **{f"heat_curve_outdoor_{i + 1}": 20 - i * 10 for i in range(7)},
            "immersion_heater": 0,
            "anti_legionella_enabled": None,
            "smart_grid_request": 0,
        }
        self.writes = []
        self.commands = []
        settings = MODULES["const"].DEFAULT_OPTIONS | {
            "max_hot_water_temperature": 65,
            "max_start_temperature": 60,
        }
        self.engine = MODULES["control"].ControlEngine(
            self.value, self.write, self.persist, settings
        )

    def value(self, key):
        return self.registers.get(key)

    def humidity_source_details(self):
        return {
            "selected_sensor": self.engine.settings.get("inside_humidity_sensor") or None,
            "external_status": "valid"
            if self.effective_inside_humidity is not None
            else "not_selected",
            "effective_humidity": self.effective_inside_humidity,
            "controller_available": self.last_update_success,
        }

    def temperature_source_info(self, location):
        return {
            "external_status": "valid" if location == "inside" else "not_selected",
            "fallback_status": "valid",
            "external_unit": "°C" if location == "inside" else None,
            "external_report_age_seconds": 10 if location == "inside" else None,
            "freshness_limit_seconds": 0,
        }

    def temperature_source_details(self, location):
        return {
            **self.temperature_source_info(location),
            "selected_sensor": self.inside_source if location == "inside" else None,
            "selected_sensor_state": "22" if location == "inside" else None,
            "selected_sensor_unit": "°C" if location == "inside" else None,
            "effective_temperature": getattr(self, f"effective_{location}"),
            "thermia_temperature": self.value(
                "indoor_temperature" if location == "inside" else "outdoor_temperature"
            ),
            "controller_available": self.last_update_success,
        }

    async def write(self, key, value):
        if self.value(key) is None:
            raise ValueError(f"Unavailable native register: {key}")
        self.writes.append((key, value))
        self.registers[key] = value

    async def persist(self, state):
        pass

    async def async_command(self, action, value=None):
        self.commands.append((action, value))
        await self.engine.tick(
            self.effective_inside, self.effective_outside, self.effective_inside_humidity
        )
        await self.engine.command(action, value)
        await self.engine.tick(
            self.effective_inside, self.effective_outside, self.effective_inside_humidity
        )


class PlatformPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = Coordinator()

    def test_thermostats_preserve_source_readings_and_keep_whole_degree_target_steps(
        self,
    ):
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.effective_inside = 22.34
        self.coordinator.engine.state.update(heating_target=23.34, heating_mode="heat")
        self.coordinator.registers.update(
            hot_water_weighted_temperature=50.26,
            hot_water_start=45.23,
            hot_water_stop=55.46,
        )
        for entity in (heating, boiler):
            self.assertEqual(entity.precision, 0.1)
            self.assertEqual(entity.target_temperature_step, 1)
        self.assertEqual(heating.current_temperature, 22.34)
        self.assertEqual(heating.extra_state_attributes["current_temperature"], 22.34)
        self.assertEqual(heating.target_temperature, 23.34)
        self.assertEqual(boiler.current_temperature, 50.26)
        self.assertEqual(boiler.extra_state_attributes["current_temperature"], 50.26)
        self.assertEqual(
            (boiler.target_temperature_low, boiler.target_temperature_high), (45.23, 55.46)
        )
        self.coordinator.engine.state["heating_mode"] = "cool"
        self.assertEqual(heating.target_temperature, 23.34)
        self.assertEqual(self.coordinator.writes, [])

    def test_current_temperature_attributes_preserve_precision_and_preferred_units(self):
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        for entity in (heating, boiler):
            entity.hass = SimpleNamespace(
                config=SimpleNamespace(
                    units=SimpleNamespace(temperature_unit=UnitOfTemperature.FAHRENHEIT)
                )
            )
        for value in (22.34, 22.345, 22.34567, 25.0):
            with self.subTest(value=value):
                self.coordinator.effective_inside = value
                self.assertEqual(heating.current_temperature, value)
                self.assertEqual(
                    heating.extra_state_attributes["current_temperature"], value * 1.8 + 32.0
                )
        self.coordinator.registers.update(
            hot_water_weighted_temperature=50.26, hot_water_top_temperature=51.34
        )
        attrs = boiler.extra_state_attributes
        self.assertEqual(attrs["current_temperature"], 50.26 * 1.8 + 32.0)
        self.assertEqual(attrs["hot_water_weighted_temperature"], 50.26)
        self.assertEqual(attrs["hot_water_top_temperature"], 51.34)
        self.coordinator.registers["hot_water_weighted_temperature"] = None
        attrs = boiler.extra_state_attributes
        self.assertEqual(attrs["current_temperature"], 51.34 * 1.8 + 32.0)
        self.assertEqual(attrs["current_temperature_source"], "top")
        self.assertEqual(self.coordinator.writes, [])

    def test_current_temperature_rejects_invalid_values_and_keeps_tank_fallback(self):
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        for invalid in (None, True, "22.34", float("nan"), float("inf"), 10**500):
            with self.subTest(invalid=invalid):
                self.coordinator.effective_inside = invalid
                self.assertIsNone(heating.current_temperature)
                self.assertIsNone(heating.extra_state_attributes["current_temperature"])
                self.coordinator.registers.update(
                    hot_water_weighted_temperature=invalid, hot_water_top_temperature=51.34
                )
                self.assertEqual(boiler.current_temperature, 51.34)
                self.assertEqual(boiler.extra_state_attributes["current_temperature"], 51.34)
                self.coordinator.registers["hot_water_top_temperature"] = invalid
                self.assertIsNone(boiler.current_temperature)
                self.assertIsNone(boiler.extra_state_attributes["current_temperature"])
        self.assertEqual(self.coordinator.writes, [])

    async def test_decimal_display_precision_does_not_allow_fractional_thermostat_edits(self):
        for entity_class, request in (
            (MODULES["climate"].ThermiaClimate, {"temperature": 23.5}),
            (
                MODULES["climate"].ThermiaClimate,
                {"target_temp_low": 19.5, "target_temp_high": 25, "hvac_mode": "heat_cool"},
            ),
            (
                MODULES["climate"].ThermiaHotWaterClimate,
                {"target_temp_low": 45.5, "target_temp_high": 55},
            ),
            (
                MODULES["climate"].ThermiaHotWaterClimate,
                {"target_temp_low": 45, "target_temp_high": 55.5},
            ),
        ):
            with self.subTest(request=request):
                coordinator = Coordinator()
                entity = entity_class(coordinator)
                with self.assertRaisesRegex(ValueError, "whole-degree"):
                    await entity.async_set_temperature(**request)
                self.assertEqual(coordinator.writes, [])
                self.assertEqual(coordinator.value("hot_water_start"), 45)
                self.assertEqual(coordinator.value("hot_water_stop"), 55)
                self.assertEqual(coordinator.value("comfort_wheel"), 20)

    async def test_off_thermostats_hide_presets_and_targets_but_keep_readings_and_activation_features(
        self,
    ):
        for entity_class in (
            MODULES["climate"].ThermiaClimate,
            MODULES["climate"].ThermiaHotWaterClimate,
        ):
            with self.subTest(entity=entity_class.__name__):
                coordinator = Coordinator()
                entity = entity_class(coordinator)
                active_features = entity.supported_features
                reading = entity.current_temperature
                await entity.async_turn_off()
                self.assertTrue(entity.available)
                self.assertEqual(entity.hvac_mode, HVACMode.OFF)
                self.assertEqual(entity.preset_modes, [])
                self.assertIsNone(entity.preset_mode)
                self.assertIsNone(entity.target_temperature)
                self.assertIsNone(entity.target_temperature_low)
                self.assertIsNone(entity.target_temperature_high)
                self.assertEqual(entity.current_temperature, reading)
                self.assertEqual(
                    entity.supported_features,
                    active_features & ~ClimateEntityFeature.PRESET_MODE,
                )
                self.assertTrue(
                    entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                )
                self.assertTrue(entity.supported_features & ClimateEntityFeature.TURN_ON)
                self.assertTrue(entity.supported_features & ClimateEntityFeature.TURN_OFF)
                await entity.async_turn_on()
                self.assertEqual(entity.supported_features, active_features)
                self.assertTrue(entity.preset_modes)
                self.assertEqual(entity.preset_mode, "Normal")

    async def test_heating_cooling_off_leaves_hot_water_permission_unchanged(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.registers.update(
            heating_enabled=True,
            passive_cooling_enabled=True,
            hot_water_enabled=True,
        )

        await entity.async_set_hvac_mode(HVACMode.OFF)

        self.assertEqual(entity.hvac_mode, HVACMode.OFF)
        self.assertFalse(self.coordinator.value("heating_enabled"))
        self.assertFalse(self.coordinator.value("passive_cooling_enabled"))
        self.assertTrue(self.coordinator.value("hot_water_enabled"))
        self.assertNotIn(("hot_water_enabled", False), self.coordinator.writes)
        self.assertNotIn(("hot_water_enabled", True), self.coordinator.writes)

    async def test_off_thermostats_reject_preset_and_temperature_calls_without_commands(self):
        for entity_class, presets, temperatures in (
            (
                MODULES["climate"].ThermiaClimate,
                ("Normal", "Excess Energy", "Low Mode", "Vacation"),
                ({"temperature": 23}, {"target_temp_low": 21, "target_temp_high": 25}),
            ),
            (
                MODULES["climate"].ThermiaHotWaterClimate,
                ("Normal", "Excess Energy", "Low Mode"),
                ({"target_temp_low": 46, "target_temp_high": 56},),
            ),
        ):
            with self.subTest(entity=entity_class.__name__):
                coordinator = Coordinator()
                entity = entity_class(coordinator)
                await entity.async_turn_off()
                coordinator.commands.clear()
                coordinator.writes.clear()
                original = deepcopy(coordinator.engine.state)
                for preset in presets:
                    with self.assertRaisesRegex(HomeAssistantError, "Turn on"):
                        await entity.async_set_preset_mode(preset)
                for temperature in temperatures:
                    for request in (temperature, temperature | {"hvac_mode": "off"}):
                        with self.assertRaisesRegex(HomeAssistantError, "Turn on"):
                            await entity.async_set_temperature(**request)
                self.assertEqual(coordinator.engine.state, original)
                self.assertEqual(coordinator.commands, [])
                self.assertEqual(coordinator.writes, [])

    async def test_temperature_calls_requesting_off_do_not_cancel_active_presets_or_edit_settings(
        self,
    ):
        for entity_class, preset, request in (
            (MODULES["climate"].ThermiaClimate, "Excess Energy", {"temperature": 24}),
            (
                MODULES["climate"].ThermiaHotWaterClimate,
                "Low Mode",
                {"target_temp_low": 36, "target_temp_high": 40},
            ),
        ):
            with self.subTest(entity=entity_class.__name__):
                coordinator = Coordinator()
                entity = entity_class(coordinator)
                await entity.async_set_preset_mode(preset)
                coordinator.commands.clear()
                coordinator.writes.clear()
                original = deepcopy(coordinator.engine.state)
                with self.assertRaisesRegex(HomeAssistantError, "Turn on"):
                    await entity.async_set_temperature(**(request | {"hvac_mode": "off"}))
                self.assertEqual(coordinator.engine.state, original)
                self.assertEqual(coordinator.commands, [])
                self.assertEqual(coordinator.writes, [])

    async def test_combined_active_mode_and_temperature_reactivate_off_heating_atomically(self):
        for mode, temperature, expected in (
            (HVACMode.HEAT, {"temperature": 23}, (23, 23)),
            (HVACMode.COOL, {"temperature": 25}, (25, 25)),
            (HVACMode.HEAT_COOL, {"target_temp_low": 19, "target_temp_high": 26}, (19, 26)),
        ):
            with self.subTest(mode=mode):
                coordinator = Coordinator()
                entity = MODULES["climate"].ThermiaClimate(coordinator)
                await entity.async_turn_off()
                coordinator.commands.clear()
                request = temperature | {"hvac_mode": mode.value}
                await entity.async_set_temperature(**request)
                self.assertEqual(coordinator.commands, [("heating_temperature_edit", request)])
                self.assertEqual(entity.hvac_mode, mode)
                self.assertEqual(entity.preset_mode, "Normal")
                self.assertEqual(
                    (entity.target_temperature_low, entity.target_temperature_high), expected
                )
                self.assertTrue(entity.supported_features & ClimateEntityFeature.PRESET_MODE)
                if mode != HVACMode.COOL:
                    self.assertEqual(coordinator.value("comfort_wheel"), expected[0])

    async def test_combined_auto_range_and_mode_only_actions_reactivate_off_boiler(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await boiler.async_turn_off()
        self.coordinator.commands.clear()
        request = {"target_temp_low": 46, "target_temp_high": 56, "hvac_mode": "auto"}
        await boiler.async_set_temperature(**request)
        self.assertEqual(self.coordinator.commands, [("hot_water_temperature_edit", request)])
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (46, 56))
        await boiler.async_turn_off()
        self.coordinator.commands.clear()
        await boiler.async_set_temperature(hvac_mode="auto")
        self.assertEqual(self.coordinator.commands, [("hot_water_mode", "auto")])
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        await heating.async_turn_off()
        self.coordinator.commands.clear()
        await heating.async_set_temperature(hvac_mode="heat")
        self.assertEqual(self.coordinator.commands, [("heating_mode", "heat")])
        self.assertEqual(heating.hvac_mode, HVACMode.HEAT)

    async def test_off_boiler_availability_validates_raw_pair_independently_of_hidden_targets(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await boiler.async_turn_off()
        self.assertTrue(boiler.available)
        self.assertEqual(
            (boiler.target_temperature_low, boiler.target_temperature_high), (None, None)
        )
        for key in ("hot_water_start", "hot_water_stop"):
            original = self.coordinator.registers[key]
            for invalid in (None, True, float("nan"), float("inf"), "45"):
                with self.subTest(key=key, invalid=invalid):
                    self.coordinator.registers[key] = invalid
                    self.assertFalse(boiler.available)
            self.coordinator.registers[key] = original
        self.assertTrue(boiler.available)
        self.coordinator.last_update_success = False
        self.assertFalse(boiler.available)

    async def test_current_humidity_is_read_only_selected_sensor_data_in_active_and_off_modes(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.effective_inside_humidity = 55.7
        self.assertIsNone(entity.current_humidity)
        self.coordinator.engine.settings["inside_humidity_sensor"] = "sensor.room_humidity"
        self.assertEqual(entity.current_humidity, 55.7)
        self.assertFalse(entity.supported_features & ClimateEntityFeature.TARGET_HUMIDITY)
        await entity.async_turn_off()
        self.assertEqual(entity.current_humidity, 55.7)
        self.assertFalse(entity.supported_features & ClimateEntityFeature.TARGET_HUMIDITY)
        self.coordinator.effective_inside_humidity = None
        self.assertIsNone(entity.current_humidity)
        self.coordinator.engine.settings["inside_humidity_sensor"] = ""
        self.coordinator.effective_inside_humidity = 55.7
        self.assertIsNone(entity.current_humidity)

    async def test_scalar_heating_modes_publish_equal_history_bounds_for_each_available_preset(
        self,
    ):
        for mode, presets in (
            (HVACMode.HEAT, ("Normal", "Excess Energy", "Low Mode", "Vacation")),
            (HVACMode.COOL, ("Normal", "Vacation")),
        ):
            for preset in presets:
                with self.subTest(mode=mode, preset=preset):
                    coordinator = Coordinator()
                    entity = MODULES["climate"].ThermiaClimate(coordinator)
                    await entity.async_set_temperature(temperature=24, hvac_mode=mode.value)
                    await entity.async_set_preset_mode(preset)
                    self.assertIsNotNone(entity.target_temperature)
                    self.assertEqual(
                        (entity.target_temperature_low, entity.target_temperature_high),
                        (entity.target_temperature, entity.target_temperature),
                    )

    async def test_mixed_auto_and_scalar_history_keeps_target_bounds_until_off(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        history = []
        for request in (
            {"target_temp_low": 19, "target_temp_high": 26, "hvac_mode": "heat_cool"},
            {"temperature": 22, "hvac_mode": "heat"},
            {"temperature": 25, "hvac_mode": "cool"},
        ):
            await entity.async_set_temperature(**request)
            history.append((entity.target_temperature_low, entity.target_temperature_high))
        self.assertEqual(history, [(19, 26), (22, 22), (25, 25)])
        await entity.async_turn_off()
        self.assertEqual(
            (entity.target_temperature_low, entity.target_temperature_high), (None, None)
        )

    async def test_heating_presets_follow_the_hvac_mode_and_auto_suffixes(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
        self.assertEqual(
            entity.preset_modes,
            ["Normal", "Excess Energy (Heating Only)", "Low Mode (Heating Only)", "Vacation"],
        )
        await entity.async_set_preset_mode("Low Mode (Heating Only)")
        self.assertEqual(entity.preset_mode, "Low Mode (Heating Only)")
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (18, 24))
        await entity.async_set_hvac_mode(HVACMode.COOL)
        self.assertEqual(entity.preset_modes, ["Normal", "Vacation"])
        self.assertEqual(entity.preset_mode, "Normal")
        commands = list(self.coordinator.commands)
        for hidden in (
            "Low Mode",
            "Excess Energy",
            "Low Mode (Heating Only)",
            "Excess Energy (Heating Only)",
        ):
            with self.subTest(hidden=hidden):
                with self.assertRaisesRegex(HomeAssistantError, "Normal and Vacation"):
                    await entity.async_set_preset_mode(hidden)
        self.assertEqual(self.coordinator.commands, commands)
        await entity.async_set_preset_mode("Vacation")
        await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
        self.assertEqual(entity.preset_mode, "Vacation")
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (17, 24))
        await entity.async_set_hvac_mode(HVACMode.OFF)
        self.assertIsNone(entity.preset_mode)
        self.assertEqual(entity.preset_modes, [])
        self.assertFalse(self.coordinator.value("heating_enabled"))
        self.assertFalse(self.coordinator.value("passive_cooling_enabled"))

    async def test_temperature_changes_leave_every_available_heating_preset_in_each_hvac_mode(self):
        cases = {
            "heat": ["Normal", "Excess Energy", "Low Mode", "Vacation"],
            "cool": ["Normal", "Vacation"],
            "heat_cool": [
                "Normal",
                "Excess Energy (Heating Only)",
                "Low Mode (Heating Only)",
                "Vacation",
            ],
        }
        for mode, presets in cases.items():
            for preset in presets:
                endpoints = (
                    ("target_temp_low", "target_temp_high")
                    if mode == "heat_cool"
                    else ("temperature",)
                )
                for edited in endpoints:
                    with self.subTest(mode=mode, preset=preset, edited=edited):
                        coordinator = Coordinator()
                        entity = MODULES["climate"].ThermiaClimate(coordinator)
                        coordinator.engine.settings["heating_vacation_cooling_offset"] = 2
                        if mode == "heat_cool":
                            await entity.async_set_temperature(
                                target_temp_low=20, target_temp_high=26, hvac_mode=mode
                            )
                        else:
                            await entity.async_set_temperature(
                                temperature=24 if mode == "cool" else 22, hvac_mode=mode
                            )
                        await entity.async_set_preset_mode(preset)
                        active_mode = entity.hvac_mode.value
                        if mode == "heat_cool":
                            request = {
                                "target_temp_low": entity.target_temperature_low,
                                "target_temp_high": entity.target_temperature_high,
                            }
                            request[edited] = 21 if edited == "target_temp_low" else 27
                        else:
                            request = {"temperature": 25}
                        await entity.async_set_temperature(**request)
                        self.assertEqual(entity.preset_mode, "Normal")
                        self.assertEqual(entity.hvac_mode.value, active_mode)
                        self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
                        if mode == "heat_cool":
                            expected = (21, 26) if edited == "target_temp_low" else (20, 27)
                            self.assertEqual(
                                (entity.target_temperature_low, entity.target_temperature_high),
                                expected,
                            )
                            self.assertEqual(coordinator.value("comfort_wheel"), expected[0])
                        else:
                            self.assertEqual(entity.target_temperature, 25)
                            if mode == "heat":
                                self.assertEqual(coordinator.value("comfort_wheel"), 25)

    async def test_reselecting_low_mode_uses_saved_normal_and_vacation_cooling_offset(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_temperature(temperature=22, hvac_mode="heat")
        for _ in range(3):
            await entity.async_set_preset_mode("Low Mode")
            self.assertEqual(entity.target_temperature, 20)
            self.assertEqual(entity.extra_state_attributes["normal_target_temperature"], 22)
            self.assertEqual(self.coordinator.value("comfort_wheel"), 20)
        await entity.async_set_preset_mode("Vacation")
        self.assertEqual(entity.target_temperature, 17)
        await entity.async_set_preset_mode("Normal")
        self.assertEqual(entity.target_temperature, 22)
        await entity.async_set_temperature(temperature=24, hvac_mode="cool")
        self.coordinator.engine.settings["heating_vacation_cooling_offset"] = 2
        await entity.async_set_preset_mode("Vacation")
        self.assertEqual(entity.target_temperature, 22)
        self.assertEqual(entity.extra_state_attributes["vacation_cooling_temperature_reduction"], 2)
        await entity.async_set_temperature(temperature=23)
        self.assertEqual(entity.preset_mode, "Normal")
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)

    async def test_vacation_cool_edit_does_not_require_an_unused_valid_auto_range(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_temperature(temperature=24, hvac_mode="cool")
        self.coordinator.engine.settings["heating_vacation_cooling_offset"] = 10
        await entity.async_set_preset_mode("Vacation")
        self.assertEqual(entity.target_temperature, 14)
        await entity.async_set_temperature(temperature=15)
        self.assertEqual(entity.preset_mode, "Normal")
        self.assertEqual(entity.target_temperature, 15)
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)

    async def test_normal_humidity_switch_changes_saved_preference_without_replacing_temperature(
        self,
    ):
        entity = MODULES["switch"].ThermiaCoolingHumiditySwitch(self.coordinator)
        self.assertFalse(entity.is_on)
        normal = self.coordinator.engine.state["heating_target"]
        await entity.async_turn_on()
        self.assertTrue(entity.is_on)
        self.assertIn(("setting", ("cooling_humidity_enabled", True)), self.coordinator.commands)
        self.assertEqual(self.coordinator.engine.state["heating_target"], normal)
        self.assertIn("Vacation", entity.extra_state_attributes["scope"])
        await entity.async_turn_off()
        self.assertFalse(entity.is_on)
        self.assertIn(("setting", ("cooling_humidity_enabled", False)), self.coordinator.commands)

    async def test_normal_and_vacation_humidity_sliders_keep_independent_defaults_and_settings(
        self,
    ):
        numbers = MODULES["number"]
        normal = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["cooling_humidity_limit"]
        )
        vacation = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["cooling_vacation_humidity_limit"]
        )
        self.assertEqual((normal.native_value, vacation.native_value), (80, 65))
        self.assertEqual(normal._attr_name, "B3.02 Cooling — Normal Humidity Limit")
        self.assertEqual(vacation._attr_name, "B3.03 Cooling — Vacation Humidity Limit")
        self.assertEqual(vacation._attr_unique_id, "pump-id_cooling_vacation_humidity_limit")
        for entity in (normal, vacation):
            self.assertEqual(
                (entity.native_min_value, entity.native_max_value, entity._attr_native_step),
                (30, 90, 1),
            )
            self.assertEqual(entity._attr_native_unit_of_measurement, UnitOfRatio.PERCENTAGE)
            self.assertEqual(entity._attr_device_class, NumberDeviceClass.HUMIDITY)
        await vacation.async_set_native_value(70)
        self.assertEqual((normal.native_value, vacation.native_value), (80, 70))
        self.assertIn(
            ("setting", ("cooling_vacation_humidity_limit", 70)), self.coordinator.commands
        )
        await normal.async_set_native_value(75)
        self.assertEqual((normal.native_value, vacation.native_value), (75, 70))
        self.assertIn(("setting", ("cooling_humidity_limit", 75)), self.coordinator.commands)
        self.assertEqual(self.coordinator.writes, [])

    async def test_humidity_switch_diagnostics_follow_active_profile_even_when_normal_switch_is_off(
        self,
    ):
        switch = MODULES["switch"].ThermiaCoolingHumiditySwitch(self.coordinator)
        climate = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.engine.settings["inside_humidity_sensor"] = "sensor.indoor_humidity"
        self.coordinator.effective_inside_humidity = 70
        await climate.async_set_hvac_mode(HVACMode.COOL)
        self.assertFalse(switch.is_on)
        self.assertTrue(self.coordinator.value("passive_cooling_enabled"))
        for preset, profile, limit, resume in (
            ("Vacation", "vacation", 65, 63),
            ("Normal", "normal", 80, 78),
        ):
            await climate.async_set_preset_mode(preset)
            attrs = switch.extra_state_attributes
            self.assertEqual(attrs["normal_humidity_limit"], 80)
            self.assertEqual(attrs["vacation_humidity_limit"], 65)
            self.assertEqual(attrs["normal_resume_humidity_limit"], 78)
            self.assertEqual(attrs["vacation_resume_humidity_limit"], 63)
            self.assertEqual(attrs["active_humidity_profile"], profile)
            self.assertEqual(attrs["active_humidity_limit"], limit)
            self.assertEqual(attrs["indoor_humidity_limit"], limit)
            self.assertEqual(attrs["resume_humidity_limit"], resume)
            self.assertEqual(attrs["protection_enabled"], preset == "Vacation")
            self.assertFalse(switch.is_on)
            self.assertEqual(self.coordinator.value("passive_cooling_enabled"), preset == "Normal")
        self.coordinator.engine.settings.update(
            cooling_humidity_limit=75, cooling_vacation_humidity_limit=66
        )
        writes = list(self.coordinator.writes)
        attrs = switch.extra_state_attributes
        self.assertEqual(
            (attrs["normal_humidity_limit"], attrs["vacation_humidity_limit"]), (75, 66)
        )
        self.assertEqual(attrs["indoor_humidity_limit"], 75)
        self.assertEqual(attrs["resume_humidity_limit"], 73)
        self.assertEqual(self.coordinator.writes, writes)

    async def test_manual_on_displays_configured_boost_target_below_hardware_maximum(self):
        self.coordinator.engine.settings.update(hot_water_boost_start=55, hot_water_boost_stop=60)
        entity = MODULES["water_heater"].ThermiaWaterHeater(self.coordinator)
        await entity.async_turn_on()
        self.assertEqual(entity.target_temperature, 60)
        self.assertEqual(self.coordinator.value("hot_water_stop"), 60)
        self.assertEqual(entity.extra_state_attributes["configurable_maximum_temperature"], 65)
        await entity.async_set_operation_mode("auto")
        self.assertEqual(entity.target_temperature, 55)

    async def test_hot_water_on_uses_configurable_maximum_and_auto_restores(self):
        entity = MODULES["water_heater"].ThermiaWaterHeater(self.coordinator)
        await entity.async_turn_on()
        self.assertEqual(entity.current_operation, "manual_on")
        self.assertEqual(entity.target_temperature, 65)
        self.assertEqual(self.coordinator.value("hot_water_stop"), 65)
        self.assertEqual(entity.extra_state_attributes["normal_target_temperature"], 55)
        await entity.async_set_operation_mode("auto")
        self.assertEqual(entity.target_temperature, 55)
        self.assertEqual(self.coordinator.value("hot_water_start"), 45)
        self.assertEqual(self.coordinator.value("hot_water_stop"), 55)

    async def test_slider_changes_saved_auto_target_during_manual_on(self):
        entity = MODULES["water_heater"].ThermiaWaterHeater(self.coordinator)
        await entity.async_turn_on()
        await entity.async_set_temperature(temperature=57)
        self.assertEqual(entity.target_temperature, 65)
        self.assertEqual(self.coordinator.value("hot_water_stop"), 65)
        await entity.async_set_operation_mode("auto")
        self.assertEqual(entity.target_temperature, 57)
        self.assertEqual(self.coordinator.value("hot_water_stop"), 57)

    async def test_excessive_energy_preset_requests_heat_above_normal_target_without_claiming_running(
        self,
    ):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_preset_mode("Excess Energy")
        self.assertEqual(self.coordinator.value("heating_enabled"), True)
        self.assertEqual(self.coordinator.value("fixed_supply_enabled"), True)
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual(entity.hvac_action, HVACAction.IDLE)
        self.coordinator.active_demands = ["Hot water", "Heating"]
        self.assertEqual(entity.hvac_action, HVACAction.HEATING)

    async def test_thermostats_share_normal_and_excess_presets_and_boiler_adds_low_mode(self):
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.assertEqual(heating.preset_modes, ["Normal", "Excess Energy", "Low Mode", "Vacation"])
        self.assertEqual(boiler.preset_modes, ["Normal", "Excess Energy", "Low Mode"])
        self.assertEqual(heating.preset_mode, "Normal")
        self.assertEqual(boiler.preset_mode, "Normal")
        await heating.async_set_preset_mode("Excess Energy")
        self.assertEqual(heating.preset_mode, "Excess Energy")
        self.assertEqual(self.coordinator.engine.state["heating_preset"], "pv_charge")
        self.assertEqual(heating.target_temperature, 23)
        self.assertIn(("heating_preset", "pv_charge"), self.coordinator.commands)
        await heating.async_set_preset_mode("Normal")
        self.assertEqual(heating.preset_mode, "Normal")
        self.assertEqual(heating.target_temperature, 20)
        self.assertFalse(self.coordinator.value("fixed_supply_enabled"))
        self.assertIn(("heating_preset", "normal"), self.coordinator.commands)
        await boiler.async_set_preset_mode("Excess Energy")
        self.assertEqual(boiler.preset_mode, "Excess Energy")
        self.assertEqual(self.coordinator.engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (58, 60))
        self.assertIn(("hot_water_mode", "energy_excess"), self.coordinator.commands)
        await boiler.async_set_preset_mode("Normal")
        self.assertEqual(boiler.preset_mode, "Normal")
        self.assertEqual(self.coordinator.engine.state["hot_water_mode"], "auto")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))
        self.assertFalse(self.coordinator.value("hot_water_boost"))
        self.assertIn(("hot_water_mode", "auto"), self.coordinator.commands)

    async def test_unadvertised_legacy_preset_labels_are_rejected_without_commands(self):
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        for entity in (heating, boiler):
            for label in ("normal", "pv_charge", "Auto", "Energy Excess"):
                with self.subTest(entity=type(entity).__name__, label=label):
                    with self.assertRaisesRegex(HomeAssistantError, "Normal.*Excess Energy"):
                        await entity.async_set_preset_mode(label)
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])

    async def test_auto_range_reaches_engine_with_correct_keys(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_temperature(
            target_temp_low=19, target_temp_high=25, hvac_mode="heat_cool"
        )
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)
        self.assertEqual(entity.target_temperature_low, 19)
        self.assertEqual(entity.target_temperature_high, 25)
        self.assertIn(
            (
                "heating_temperature_edit",
                {"target_temp_low": 19, "target_temp_high": 25, "hvac_mode": "heat_cool"},
            ),
            self.coordinator.commands,
        )

    async def test_heat_cool_and_auto_use_distinct_native_permissions_and_target_scopes(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.effective_inside = 26
        await entity.async_set_temperature(temperature=22)
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        self.assertEqual(
            (
                self.coordinator.value("heating_enabled"),
                self.coordinator.value("passive_cooling_enabled"),
            ),
            (True, False),
        )
        self.assertEqual(self.coordinator.value("comfort_wheel"), 22)
        self.assertEqual(entity.hvac_action, HVACAction.IDLE)
        self.assertIn(
            "matches the Heat target", entity.extra_state_attributes["heating_target_sync"]
        )
        self.coordinator.active_demands = ["Heating"]
        self.assertEqual(entity.hvac_action, HVACAction.HEATING)
        self.coordinator.active_demands = []

        self.coordinator.effective_inside = 12
        await entity.async_set_hvac_mode(HVACMode.COOL)
        await entity.async_set_temperature(temperature=25)
        self.assertEqual(
            (
                self.coordinator.value("heating_enabled"),
                self.coordinator.value("passive_cooling_enabled"),
            ),
            (False, True),
        )
        self.assertEqual(entity.target_temperature, 25)
        self.assertEqual(self.coordinator.value("comfort_wheel"), 22)
        self.assertIn(
            "Home Assistant only", entity.extra_state_attributes["cooling_room_target_scope"]
        )

        self.coordinator.effective_inside = 22
        await entity.async_set_temperature(target_temp_low=19, target_temp_high=25)
        await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
        self.assertEqual(self.coordinator.value("comfort_wheel"), 19)
        self.assertIn(
            "matches the Auto lower target", entity.extra_state_attributes["heating_target_sync"]
        )
        self.assertEqual(
            (
                entity.extra_state_attributes["normal_target_temperature_low"],
                entity.extra_state_attributes["normal_target_temperature_high"],
            ),
            (19, 25),
        )
        self.assertEqual(
            (
                self.coordinator.value("heating_enabled"),
                self.coordinator.value("passive_cooling_enabled"),
            ),
            (False, False),
        )
        await self.coordinator.engine.tick(18, 12)
        self.assertTrue(self.coordinator.value("heating_enabled"))
        self.assertFalse(self.coordinator.value("passive_cooling_enabled"))
        await self.coordinator.engine.tick(26, 12)
        self.assertFalse(self.coordinator.value("heating_enabled"))
        self.assertTrue(self.coordinator.value("passive_cooling_enabled"))
        self.assertEqual(self.coordinator.value("comfort_wheel"), 19)
        await entity.async_turn_off()
        self.assertEqual(
            (
                self.coordinator.value("heating_enabled"),
                self.coordinator.value("passive_cooling_enabled"),
            ),
            (False, False),
        )

    async def test_manual_heat_and_cool_permissions_do_not_require_indoor_measurements(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.effective_inside = None
        await entity.async_set_hvac_mode(HVACMode.HEAT)
        self.assertTrue(self.coordinator.value("heating_enabled"))
        self.assertFalse(self.coordinator.value("passive_cooling_enabled"))
        await entity.async_set_hvac_mode(HVACMode.COOL)
        self.assertFalse(self.coordinator.value("heating_enabled"))
        self.assertTrue(self.coordinator.value("passive_cooling_enabled"))
        with self.assertRaisesRegex(ValueError, "measured inside temperature"):
            await entity.async_set_hvac_mode(HVACMode.HEAT_COOL)
        self.assertEqual(entity.hvac_mode, HVACMode.COOL)
        await entity.async_turn_off()
        self.assertEqual(
            (
                self.coordinator.value("heating_enabled"),
                self.coordinator.value("passive_cooling_enabled"),
            ),
            (False, False),
        )

    async def test_bundled_cool_temperature_does_not_copy_through_previous_heat_mode(self):
        for mode in (HVACMode.COOL,):
            with self.subTest(mode=mode):
                coordinator = Coordinator()
                entity = MODULES["climate"].ThermiaClimate(coordinator)
                await entity.async_set_temperature(temperature=22)
                await entity.async_set_hvac_mode(HVACMode.HEAT)
                coordinator.commands.clear()
                coordinator.writes.clear()
                await entity.async_set_temperature(hvac_mode=mode.value, temperature=25)
                self.assertEqual(entity.hvac_mode, mode)
                self.assertEqual(entity.target_temperature, 25)
                self.assertEqual(coordinator.value("comfort_wheel"), 22)
                self.assertFalse(any(key == "comfort_wheel" for key, _ in coordinator.writes))
                self.assertEqual(
                    coordinator.commands,
                    [("heating_temperature_edit", {"temperature": 25, "hvac_mode": mode.value})],
                )
                self.assertFalse(coordinator.value("heating_enabled"))
                self.assertEqual(
                    coordinator.value("passive_cooling_enabled"), mode == HVACMode.COOL
                )

    async def test_invalid_bundled_local_targets_do_not_change_native_permissions(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        for kwargs in (
            {"temperature": 9.5},
            {"temperature": 23.5},
            {"temperature": float("nan")},
            {"target_temp_low": 19.5, "target_temp_high": 25},
            {"target_temp_low": 25, "target_temp_high": 19},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await entity.async_set_temperature(hvac_mode="cool", **kwargs)
        self.assertEqual(len(self.coordinator.commands), 5)
        self.assertTrue(
            all(action == "heating_temperature_edit" for action, _ in self.coordinator.commands)
        )
        self.assertEqual(self.coordinator.writes, [])
        self.assertTrue(self.coordinator.value("heating_enabled"))
        self.assertFalse(self.coordinator.value("passive_cooling_enabled"))

    async def test_unset_room_target_adopts_verified_native_dial_without_temperature_offset(self):
        self.coordinator.engine.state["heating_target"] = None
        self.coordinator.registers["comfort_wheel"] = 23
        await self.coordinator.engine.tick(22, 12)
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.assertEqual((entity.min_temp, entity.max_temp), (10, 35))
        self.assertEqual(entity._attr_target_temperature_step, 1)
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual(entity.extra_state_attributes["native_heating_target_temperature"], 23)
        self.assertIn(
            "matches the Heat target", entity.extra_state_attributes["heating_target_sync"]
        )
        self.assertEqual(self.coordinator.writes, [])

    def test_room_native_target_diagnostics_allow_original_40_and_reject_invalid_readings(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        for value in (10, 23, 23.34, 35, 36, 40):
            with self.subTest(value=value):
                self.coordinator.registers["comfort_wheel"] = value
                self.assertEqual(
                    entity.extra_state_attributes["native_heating_target_temperature"], value
                )
                guide = MODULES["control_guide"].control_guide_attributes(self.coordinator)
                self.assertIn(
                    f"Thermia currently reports {value}°C", guide["Heating Targets And Thermia"]
                )
        for value in (None, True, "23", 9.99, 40.01, 20000, float("nan"), float("inf"), 10**500):
            with self.subTest(value=value):
                self.coordinator.registers["comfort_wheel"] = value
                self.assertIsNone(
                    entity.extra_state_attributes["native_heating_target_temperature"]
                )
        self.coordinator.registers["comfort_wheel"] = 23
        self.assertIn("differs", entity.extra_state_attributes["heating_target_sync"])
        self.assertEqual(self.coordinator.writes, [])

    async def test_legacy_excess_ceiling_below_native_minimum_is_refused_without_writes(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.engine.settings["max_heating_temperature"] = 8
        self.coordinator.effective_inside = 7
        with self.assertRaises(ValueError):
            await entity.async_set_preset_mode("Excess Energy")
        self.assertEqual(entity.min_temp, 10)
        self.assertEqual(entity.preset_mode, "Normal")
        self.assertEqual(entity.target_temperature, 20)
        self.assertEqual(self.coordinator.engine.settings["max_heating_temperature"], 8)
        self.assertEqual(entity.extra_state_attributes["native_heating_target_temperature"], 20)
        self.assertEqual(self.coordinator.writes, [])

    async def test_excess_native_ceiling_is_restored_then_thermostat_edit_copies_normal_target(
        self,
    ):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_preset_mode("Excess Energy")
        self.assertEqual(entity.min_temp, 10)
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual(entity.extra_state_attributes["native_heating_target_temperature"], 23)
        self.assertIn(
            "matches the Excess Energy ceiling",
            entity.extra_state_attributes["heating_target_sync"],
        )
        self.coordinator.commands.clear()
        await entity.async_set_temperature(temperature=22, entity_id="climate.example")
        self.assertEqual(
            self.coordinator.commands, [("heating_temperature_edit", {"temperature": 22})]
        )
        self.assertEqual(entity.preset_mode, "Normal")
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT)
        self.assertEqual(entity.min_temp, 10)
        self.assertEqual(entity.target_temperature, 22)
        self.assertEqual(self.coordinator.value("comfort_wheel"), 22)
        self.assertFalse(self.coordinator.value("fixed_supply_enabled"))
        self.assertEqual(self.coordinator.value("fixed_supply_target"), 30)
        self.assertIsNone(self.coordinator.engine.state["heating_excess_deadline"])

    async def test_heating_auto_range_edit_ends_excess_preserves_displayed_mode_and_copies_low(
        self,
    ):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_temperature(
            target_temp_low=19, target_temp_high=25, hvac_mode="heat_cool"
        )
        await entity.async_set_preset_mode("Excess Energy")
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (23, 23))
        self.assertEqual(self.coordinator.value("comfort_wheel"), 23)
        self.coordinator.commands.clear()
        await entity.async_set_temperature(target_temp_low=20, target_temp_high=25)
        self.assertEqual(
            self.coordinator.commands,
            [("heating_temperature_edit", {"target_temp_low": 20, "target_temp_high": 25})],
        )
        self.assertEqual(entity.preset_mode, "Normal")
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (20, 25))
        self.assertEqual(self.coordinator.value("comfort_wheel"), 20)
        self.assertFalse(self.coordinator.value("fixed_supply_enabled"))
        self.assertIsNone(self.coordinator.engine.state["heating_excess_deadline"])

    async def test_auto_excess_scalar_and_equal_history_bounds_preserve_normal_range(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.engine.now = Mock(return_value=1000)
        await entity.async_set_temperature(
            target_temp_low=19, target_temp_high=25, hvac_mode="heat_cool"
        )
        await entity.async_set_preset_mode("Excess Energy (Heating Only)")
        deadline = self.coordinator.engine.state["heating_excess_deadline"]
        writes = list(self.coordinator.writes)
        self.assertEqual(entity.hvac_mode, HVACMode.HEAT_COOL)
        self.assertEqual(entity.target_temperature, 23)
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (23, 23))
        self.assertEqual(
            (
                entity.extra_state_attributes["normal_target_temperature_low"],
                entity.extra_state_attributes["normal_target_temperature_high"],
            ),
            (19, 25),
        )
        await entity.async_set_temperature(target_temp_low=23, target_temp_high=23)
        self.assertEqual(entity.preset_mode, "Excess Energy (Heating Only)")
        self.assertEqual(self.coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertEqual(self.coordinator.writes, writes)
        await entity.async_set_temperature(temperature=22)
        self.assertEqual(entity.preset_mode, "Normal")
        self.assertIsNone(entity.target_temperature)
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (22, 25))
        self.assertEqual(self.coordinator.value("comfort_wheel"), 22)

    async def test_excess_pause_keeps_native_ceiling_until_normal_restores_original(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        await entity.async_set_preset_mode("Excess Energy")
        self.coordinator.effective_inside = 24
        await self.coordinator.engine.tick(24, self.coordinator.effective_outside)
        self.assertFalse(self.coordinator.value("fixed_supply_enabled"))
        self.assertFalse(self.coordinator.value("heating_enabled"))
        self.assertEqual(self.coordinator.value("comfort_wheel"), 23)
        self.assertIn(
            "Excess Energy is paused", entity.extra_state_attributes["heating_target_sync"]
        )
        self.assertIn("matches", entity.extra_state_attributes["heating_target_sync"])
        await entity.async_set_preset_mode("Normal")
        self.assertEqual(self.coordinator.value("comfort_wheel"), 20)
        self.assertEqual(entity.target_temperature, 20)

    def test_excess_sync_diagnostics_use_ceiling_in_auto_and_validate_native_readback(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.engine.state.update(
            heating_mode="heat_cool", heating_preset="pv_charge", charge_active={"heating": True}
        )
        self.coordinator.engine.settings["max_heating_temperature"] = 26
        for native, expected in (
            (26, "matches"),
            (20, "differs"),
            (None, "cannot be confirmed"),
            (float("nan"), "cannot be confirmed"),
        ):
            with self.subTest(native=native):
                self.coordinator.registers["comfort_wheel"] = native
                self.assertIn(expected, entity.extra_state_attributes["heating_target_sync"])
        self.assertEqual(entity.target_temperature, 26)
        self.assertEqual((entity.target_temperature_low, entity.target_temperature_high), (26, 26))
        self.assertEqual(self.coordinator.writes, [])

    async def test_charging_limit_slider_adjusts_active_ceiling_without_cancelling_excess(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.engine.now = Mock(return_value=1000)
        await entity.async_set_preset_mode("Excess Energy")
        deadline = self.coordinator.engine.state["heating_excess_deadline"]
        await (
            MODULES["number"].ThermiaHeatingChargeLimit(self.coordinator).async_set_native_value(25)
        )
        self.assertEqual(entity.preset_mode, "Excess Energy")
        self.assertEqual(entity.target_temperature, 25)
        self.assertEqual(self.coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertTrue(self.coordinator.value("fixed_supply_enabled"))
        self.assertEqual(self.coordinator.value("comfort_wheel"), 25)
        self.assertIn(
            "matches the Excess Energy ceiling",
            entity.extra_state_attributes["heating_target_sync"],
        )

    async def test_unchanged_heating_temperature_preserves_excess_and_invalid_edit_is_rejected(
        self,
    ):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.engine.now = Mock(return_value=1000)
        await entity.async_set_preset_mode("Excess Energy")
        deadline = self.coordinator.engine.state["heating_excess_deadline"]
        writes = list(self.coordinator.writes)
        await entity.async_set_temperature(temperature=entity.target_temperature)
        self.assertEqual(entity.preset_mode, "Excess Energy")
        self.assertEqual(self.coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertEqual(self.coordinator.writes, writes)
        with self.assertRaises(ValueError):
            await entity.async_set_temperature(temperature=23.5)
        self.assertEqual(entity.preset_mode, "Excess Energy")
        self.assertEqual(self.coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertEqual(self.coordinator.writes, writes)

    async def test_normal_room_target_limits_reject_before_native_writes(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        for value in (9.5, 23.5, 35.5, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await entity.async_set_temperature(temperature=value)
        with self.assertRaises(ValueError):
            await entity.async_set_temperature(target_temp_low=9.5, target_temp_high=25)
        with self.assertRaises(ValueError):
            await entity.async_set_temperature(target_temp_low=19, target_temp_high=25.5)
        self.assertEqual(self.coordinator.writes, [])

    def test_actual_cooling_and_unknown_activity(self):
        entity = MODULES["climate"].ThermiaClimate(self.coordinator)
        self.coordinator.active_demands = ["Active cooling"]
        self.assertEqual(entity.hvac_action, HVACAction.COOLING)
        self.coordinator.active_demands = ["Passive cooling"]
        self.assertEqual(entity.hvac_action, HVACAction.COOLING)
        self.coordinator.active_demands = []
        self.coordinator.status = "Unknown (999)"
        self.assertIsNone(entity.hvac_action)

    async def test_auxiliary_permission_never_forces_heater(self):
        entity = MODULES["switch"].ThermiaSwitch(
            self.coordinator,
            "auxiliary_heater",
            "Allowed",
            "immersion_heater",
            "mdi:radiator",
            on_value=2,
            valid_values=(0, 1, 2),
        )
        await entity.async_turn_on()
        self.assertEqual(self.coordinator.value("immersion_heater"), 2)
        self.assertTrue(entity.is_on)
        self.coordinator.registers["immersion_heater"] = 1
        self.assertFalse(entity.is_on)
        await entity.async_turn_off()
        self.assertEqual(self.coordinator.value("immersion_heater"), 0)

    def test_missing_hot_water_registers_do_not_break_entity_setup(self):
        self.coordinator.engine.state["hot_water_target"] = None
        self.coordinator.engine.state["hot_water_start"] = None
        self.coordinator.engine.settings["max_hot_water_temperature"] = None
        self.coordinator.engine.settings["max_start_temperature"] = None
        self.coordinator.registers["hot_water_enabled"] = None
        self.coordinator.registers["hot_water_start"] = None
        self.coordinator.registers["hot_water_stop"] = None
        water = MODULES["water_heater"].ThermiaWaterHeater(self.coordinator)
        start = MODULES["number"].ThermiaHotWaterStart(self.coordinator)
        self.assertEqual(water.max_temp, 5)
        self.assertEqual(start.native_max_value, 60)
        self.assertFalse(water.available)
        self.assertFalse(start.available)

    async def test_unverified_maximum_is_an_actionable_error(self):
        entity = MODULES["water_heater"].ThermiaWaterHeater(self.coordinator)
        self.coordinator.engine.settings["max_hot_water_temperature"] = None
        with self.assertRaisesRegex(ValueError, "Confirm the pump's maximum"):
            await entity.async_turn_on()
        self.assertEqual(entity.current_operation, "auto")
        self.assertFalse(any(key == "hot_water_stop" for key, value in self.coordinator.writes))

    async def test_all_control_platforms_add_entities_without_writes(self):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        entities = []
        for platform in ("climate", "number", "switch"):
            await MODULES[platform].async_setup_entry(None, entry, entities.extend)
        self.assertNotIn("button", MODULES["const"].PLATFORMS)
        self.assertEqual(len(entities), 27)
        self.assertEqual(len({entity._attr_unique_id for entity in entities}), 27)
        self.assertEqual(self.coordinator.writes, [])

    async def test_heating_charge_limit_updates_setting_and_rejects_invalid_values(self):
        entity = MODULES["number"].ThermiaHeatingChargeLimit(self.coordinator)
        original = self.coordinator.engine.state["heating_target"]
        await entity.async_set_native_value(25)
        self.assertEqual(entity._attr_native_step, 1)
        self.assertEqual((entity.native_min_value, entity.native_max_value), (10, 35))
        self.assertEqual(entity.native_value, 25)
        self.assertEqual(self.coordinator.engine.state["heating_target"], original)
        self.assertIn(("setting", ("max_heating_temperature", 25)), self.coordinator.commands)
        commands = list(self.coordinator.commands)
        for invalid in (
            4,
            8,
            9,
            36,
            23.5,
            25.1,
            True,
            None,
            "25",
            10**500,
            float("nan"),
            float("inf"),
        ):
            with self.assertRaises(ValueError):
                await entity.async_set_native_value(invalid)
        self.assertEqual(self.coordinator.commands, commands)

    async def test_device_configuration_default_names_put_toggles_before_grouped_sliders(
        self,
    ):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        entities = []
        await MODULES["number"].async_setup_entry(None, entry, entities.extend)
        await MODULES["switch"].async_setup_entry(None, entry, entities.extend)
        ordered = sorted(entities, key=lambda entity: entity._attr_name)
        self.assertEqual(
            [
                (entity._attr_unique_id.removeprefix("pump-id_"), entity._attr_name)
                for entity in ordered
            ],
            [
                ("heating_enabled", "A1.01 Heating"),
                ("auxiliary_heater", "A1.02 Electric Auxiliary Heater Allowed"),
                ("hot_water_enabled", "A1.03 Hot Water Enabled"),
                ("hot_water_boost", "A1.04 Thermia Boost"),
                ("anti_legionella", "A1.05 Anti-Legionella Programme"),
                ("passive_cooling", "A1.06 Cooling (Passive)"),
                ("cooling_humidity_enabled", "A1.07 Humidity Protection In Normal Cooling"),
                ("heating_low_offset", "B1.01 Heating — Low Mode Reduction"),
                ("heating_vacation_temperature", "B1.02 Heating — Vacation Target"),
                ("max_heating_temperature", "B1.03 Heating — Excess Energy Ceiling"),
                (
                    "heating_excess_heat_stop_offset",
                    "B1.04 Heating — Excess Energy Heat Stop Increase",
                ),
                ("heating_excess_hours", "B1.05 Heating — Excess Energy Duration"),
                ("heating_season_stop", "B1.06 Heating — Heat Stop"),
                ("native_heating_target", "B1.07 Heating - Native Target"),
                ("hot_water_hysteresis", "B2.01 Hot Water — Excess Energy Restart Gap"),
                ("hot_water_excess_hours", "B2.02 Hot Water — Excess Energy Duration"),
                ("hot_water_evening_start", "B2.03 Hot Water — Low Mode Start"),
                ("hot_water_evening_stop", "B2.04 Hot Water — Low Mode Stop"),
                ("hot_water_low_hours", "B2.05 Hot Water — Low Mode Duration"),
                ("hot_water_start", "B2.06 Hot Water - Native START"),
                ("hot_water_stop", "B2.07 Hot Water - Native STOP"),
                ("heating_vacation_cooling_offset", "B3.01 Cooling — Vacation Reduction"),
                ("cooling_humidity_limit", "B3.02 Cooling — Normal Humidity Limit"),
                ("cooling_vacation_humidity_limit", "B3.03 Cooling — Vacation Humidity Limit"),
                ("cooling_dew_point_margin", "B3.04 Cooling — Dew Point Margin"),
            ],
        )
        self.assertEqual(self.coordinator.writes, [])

    async def test_saved_setting_sliders_reject_invalid_numbers_before_commands(self):
        numbers = MODULES["number"]
        for spec in numbers.DEVICE_SETTINGS.values():
            entity = numbers.ThermiaSettingNumber(self.coordinator, spec)
            for invalid in (
                True,
                None,
                "25",
                float("nan"),
                float("inf"),
                10**500,
                spec.min_value - spec.step,
                spec.max_value + spec.step,
                spec.min_value + spec.step / 2,
            ):
                with self.subTest(key=spec.key, invalid=invalid), self.assertRaises(ValueError):
                    await entity.async_set_native_value(invalid)
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])

    def test_device_temperature_reductions_use_delta_units_and_keep_legacy_exact_values(self):
        numbers = MODULES["number"]
        for key in (
            "heating_low_offset",
            "heating_excess_heat_stop_offset",
            "heating_vacation_cooling_offset",
            "cooling_dew_point_margin",
            "hot_water_hysteresis",
        ):
            entity = numbers.ThermiaSettingNumber(self.coordinator, numbers.DEVICE_SETTINGS[key])
            self.assertEqual(entity._attr_device_class, NumberDeviceClass.TEMPERATURE_DELTA)
            self.assertEqual(entity._attr_native_unit_of_measurement, UnitOfTemperature.CELSIUS)
        self.coordinator.engine.settings["max_heating_temperature"] = 8
        ceiling = numbers.ThermiaHeatingChargeLimit(self.coordinator)
        self.assertEqual(ceiling.native_value, 8)
        self.assertEqual(ceiling.native_min_value, 10)
        self.coordinator.engine.settings["heating_low_offset"] = 2.5
        low = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["heating_low_offset"]
        )
        self.assertEqual(low.native_value, 2.5)
        self.assertEqual(self.coordinator.writes, [])

    async def test_low_water_profile_sliders_update_active_pair_without_rebasing_normal_or_timer(
        self,
    ):
        numbers = MODULES["number"]
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.engine.now = Mock(return_value=1000)
        await boiler.async_set_preset_mode("Low Mode")
        deadline = self.coordinator.engine.state["hot_water_low_deadline"]
        start = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["hot_water_evening_start"]
        )
        stop = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["hot_water_evening_stop"]
        )
        with self.assertRaises(ValueError):
            await start.async_set_native_value(40)
        self.assertEqual((start.native_value, stop.native_value), (35, 40))
        await start.async_set_native_value(36)
        await stop.async_set_native_value(41)
        self.assertEqual((start.native_value, stop.native_value), (36, 41))
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (36, 41))
        self.assertEqual(self.coordinator.engine.state["hot_water_low_deadline"], deadline)
        self.assertEqual(
            (
                self.coordinator.engine.state["hot_water_start"],
                self.coordinator.engine.state["hot_water_target"],
            ),
            (45, 55),
        )
        await boiler.async_set_preset_mode("Normal")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))

    async def test_duration_slider_changes_current_deadline_from_original_start(self):
        numbers = MODULES["number"]
        self.coordinator.engine.now = Mock(return_value=1000)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await boiler.async_set_preset_mode("Low Mode")
        self.coordinator.engine.now.return_value = 1100
        entity = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["hot_water_low_hours"]
        )
        await entity.async_set_native_value(12)
        self.assertEqual(entity.native_value, 12)
        self.assertEqual(entity._attr_native_unit_of_measurement, UnitOfTime.HOURS)
        self.assertEqual(entity._attr_device_class, NumberDeviceClass.DURATION)
        self.assertEqual(self.coordinator.engine.state["hot_water_low_started_at"], 1000)
        self.assertEqual(self.coordinator.engine.state["hot_water_low_deadline"], 44200)
        self.assertEqual(boiler.preset_mode, "Low Mode")

    async def test_duration_sliders_keep_legacy_values_but_require_whole_hour_edits(self):
        numbers = MODULES["number"]
        for key in ("heating_excess_hours", "hot_water_excess_hours", "hot_water_low_hours"):
            self.coordinator.engine.settings[key] = 12.5
            entity = numbers.ThermiaSettingNumber(self.coordinator, numbers.DEVICE_SETTINGS[key])
            self.assertEqual(entity.native_value, 12.5)
            self.assertEqual(
                (entity.native_min_value, entity.native_max_value, entity._attr_native_step),
                (1, 168, 1),
            )
            self.assertEqual(entity._attr_mode, NumberMode.SLIDER)
            commands = list(self.coordinator.commands)
            for invalid in (0.5, 0, 12.5, 168.5, float("nan"), True):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    await entity.async_set_native_value(invalid)
            self.assertEqual(self.coordinator.commands, commands)
            self.assertEqual(entity.native_value, 12.5)
            await entity.async_set_native_value(12)
            self.assertEqual(entity.native_value, 12)
            self.assertEqual(self.coordinator.commands[-1], ("setting", (key, 12)))
        self.assertEqual(self.coordinator.writes, [])

    async def test_restart_gap_slider_updates_paused_excess_without_resetting_timer_or_normal_pair(
        self,
    ):
        numbers = MODULES["number"]
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        gap = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["hot_water_hysteresis"]
        )
        self.assertEqual(gap.native_value, 2)
        self.assertEqual(
            (gap.native_min_value, gap.native_max_value, gap._attr_native_step), (1, 30, 1)
        )
        self.assertEqual(gap._attr_device_class, NumberDeviceClass.TEMPERATURE_DELTA)
        self.coordinator.engine.now = Mock(return_value=1000)
        await boiler.async_set_preset_mode("Excess Energy")
        deadline = self.coordinator.engine.state["hot_water_excess_deadline"]
        self.coordinator.registers.update(
            hot_water_top_temperature=60, hot_water_weighted_temperature=60
        )
        await self.coordinator.engine.tick(22, 12)
        await gap.async_set_native_value(5)
        self.assertEqual(gap.native_value, 5)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (55, 60))
        self.assertEqual(boiler.preset_mode, "Excess Energy")
        self.assertEqual(self.coordinator.engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(
            (
                self.coordinator.engine.state["hot_water_start"],
                self.coordinator.engine.state["hot_water_target"],
            ),
            (45, 55),
        )
        self.assertFalse(self.coordinator.value("hot_water_boost"))
        for temperature, expected_boost in ((56, False), (55, True)):
            self.coordinator.registers.update(
                hot_water_top_temperature=temperature, hot_water_weighted_temperature=temperature
            )
            await self.coordinator.engine.tick(22, 12)
            self.assertEqual(self.coordinator.value("hot_water_boost"), expected_boost)
        await boiler.async_set_preset_mode("Normal")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))

    async def test_default_limits_cap_one_degree_gap_and_report_effective_pair_without_writes(self):
        coordinator = Coordinator()
        self.assertEqual(
            (
                MODULES["const"].DEFAULT_OPTIONS["max_start_temperature"],
                MODULES["const"].DEFAULT_OPTIONS["max_hot_water_temperature"],
            ),
            (55, 60),
        )
        coordinator.engine.settings.update(max_start_temperature=55, max_hot_water_temperature=60)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
        gap = MODULES["number"].ThermiaSettingNumber(
            coordinator, MODULES["number"].DEVICE_SETTINGS["hot_water_hysteresis"]
        )
        original = deepcopy(coordinator.engine.state)
        for requested_gap in (1, 2):
            coordinator.engine.settings["hot_water_hysteresis"] = requested_gap
            attrs = boiler.extra_state_attributes
            self.assertEqual(attrs["energy_excess_restart_gap"], requested_gap)
            self.assertEqual(attrs["energy_excess_configured_restart_gap"], requested_gap)
            self.assertEqual(attrs["energy_excess_requested_start_temperature"], 60 - requested_gap)
            self.assertEqual(attrs["energy_excess_start_temperature"], 55)
            self.assertEqual(attrs["energy_excess_stop_temperature"], 60)
            self.assertEqual(attrs["energy_excess_effective_restart_gap"], 5)
            self.assertTrue(attrs["energy_excess_limits_available"])
            self.assertIsNone(attrs["energy_excess_limits_status"])
        self.assertEqual(coordinator.engine.state, original)
        self.assertEqual(coordinator.writes, [])
        self.assertEqual(coordinator.commands, [])

        coordinator.engine.now = Mock(return_value=1000)
        await boiler.async_set_preset_mode("Excess Energy")
        deadline = coordinator.engine.state["hot_water_excess_deadline"]
        await gap.async_set_native_value(1)
        self.assertEqual(gap.native_value, 1)
        self.assertEqual(coordinator.commands[-1], ("setting", ("hot_water_hysteresis", 1)))
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (55, 60))
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(boiler.extra_state_attributes["normal_start_temperature"], 45)
        self.assertEqual(boiler.extra_state_attributes["normal_stop_temperature"], 55)
        writes = list(coordinator.writes)
        for invalid in (0, 0.5, 1.5, 31, True, float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                await gap.async_set_native_value(invalid)
        self.assertEqual(coordinator.writes, writes)
        for temperature, expected_boost in ((60, False), (58, False), (55, True)):
            coordinator.registers.update(
                hot_water_top_temperature=temperature, hot_water_weighted_temperature=temperature
            )
            await coordinator.engine.tick(22, 12)
            self.assertEqual(coordinator.value("hot_water_boost"), expected_boost)
            self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], deadline)

    def test_boiler_excess_diagnostics_distinguish_profile_limits_from_native_support(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.engine.settings.update(hot_water_hysteresis=1)
        self.coordinator.registers["hot_water_boost"] = 20000
        self.coordinator.last_update_success = False
        attrs = boiler.extra_state_attributes
        self.assertEqual(
            (attrs["energy_excess_start_temperature"], attrs["energy_excess_stop_temperature"]),
            (59, 60),
        )
        self.assertEqual(attrs["energy_excess_effective_restart_gap"], 1)
        self.assertTrue(attrs["energy_excess_limits_available"])
        self.assertFalse(attrs["native_boost_available"])
        self.assertFalse(attrs["energy_excess_active"])
        for settings in (
            {"max_start_temperature": None},
            {"max_hot_water_temperature": 59},
            {"max_start_temperature": 29},
            {"hot_water_hysteresis": 0},
        ):
            with self.subTest(settings=settings):
                self.coordinator.engine.settings.update(
                    max_start_temperature=55, max_hot_water_temperature=60, hot_water_hysteresis=2
                )
                self.coordinator.engine.settings.update(settings)
                attrs = boiler.extra_state_attributes
                self.assertFalse(attrs["energy_excess_limits_available"])
                self.assertIsNone(attrs["energy_excess_start_temperature"])
                self.assertIsNone(attrs["energy_excess_stop_temperature"])
                self.assertIsNone(attrs["energy_excess_effective_restart_gap"])
                self.assertTrue(attrs["energy_excess_limits_status"])
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(self.coordinator.commands, [])

    async def test_heat_stop_increase_slider_uses_original_native_stop_and_preserves_excess_clock(
        self,
    ):
        numbers = MODULES["number"]
        self.coordinator.engine.now = Mock(return_value=1000)
        climate = MODULES["climate"].ThermiaClimate(self.coordinator)
        await climate.async_set_preset_mode("Excess Energy")
        deadline = self.coordinator.engine.state["heating_excess_deadline"]
        entity = numbers.ThermiaSettingNumber(
            self.coordinator, numbers.DEVICE_SETTINGS["heating_excess_heat_stop_offset"]
        )
        self.assertEqual(self.coordinator.value("heating_season_stop"), 19)
        await entity.async_set_native_value(4)
        self.assertEqual(self.coordinator.value("heating_season_stop"), 21)
        self.assertEqual(self.coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertEqual(climate.preset_mode, "Excess Energy")
        await climate.async_set_preset_mode("Normal")
        self.assertEqual(self.coordinator.value("heating_season_stop"), 17)

    async def test_legacy_hot_water_slider_classes_keep_compatibility(self):
        entities = [
            MODULES["number"].ThermiaHotWaterStart(self.coordinator),
            MODULES["number"].ThermiaHotWaterStop(self.coordinator),
        ]
        self.assertEqual(
            [entity._attr_unique_id for entity in entities],
            ["pump-id_hot_water_start", "pump-id_hot_water_stop"],
        )
        for entity in entities:
            self.assertEqual((entity.native_min_value, entity.native_max_value), (30, 60))
            self.assertEqual(entity._attr_mode, NumberMode.SLIDER)
            self.assertTrue(entity._attr_entity_registry_enabled_default)
            self.assertTrue(entity._attr_entity_registry_visible_default)
            self.assertEqual(entity._attr_entity_category, EntityCategory.CONFIG)
        start, stop = entities
        self.assertEqual((start.native_value, stop.native_value), (45, 55))
        await start.async_set_native_value(44)
        await stop.async_set_native_value(54)
        self.assertEqual((start.native_value, stop.native_value), (44, 54))
        self.assertEqual(start.extra_state_attributes["normal_temperature"], 44)
        self.assertEqual(stop.extra_state_attributes["normal_temperature"], 54)
        self.assertIn(("native_hot_water_start", 44), self.coordinator.commands)
        self.assertIn(("native_hot_water_stop", 54), self.coordinator.commands)

    async def test_device_setting_sliders_keep_saved_values_and_native_heat_stop_readback(
        self,
    ):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        numbers = []
        await MODULES["number"].async_setup_entry(None, entry, numbers.extend)
        metadata = MODULES["native_settings"].NATIVE_SETTINGS
        self.assertEqual(
            [entity.spec.key for entity in numbers if hasattr(entity, "spec")],
            ["heating_season_stop"],
        )
        self.assertEqual(len(numbers), 18)
        for entity in numbers:
            if isinstance(entity, MODULES["number"].ThermiaHotWaterStart):
                self.assertTrue(entity.available)
                continue
            if not hasattr(entity, "spec"):
                spec = entity.setting_spec
                self.assertEqual(entity.native_value, self.coordinator.engine.settings[spec.key])
                self.assertEqual(entity._attr_unique_id, f"pump-id_{spec.key}")
                self.assertEqual(entity._attr_name, spec.label)
                self.assertEqual(
                    (entity.native_min_value, entity.native_max_value),
                    (spec.min_value, spec.max_value),
                )
                self.assertEqual(entity._attr_native_step, spec.step)
                self.assertEqual(entity._attr_native_unit_of_measurement, spec.unit)
                self.assertEqual(entity._attr_device_class, spec.device_class)
                self.assertEqual(entity._attr_mode, NumberMode.SLIDER)
                self.assertEqual(entity._attr_entity_category, EntityCategory.CONFIG)
                self.assertTrue(entity._attr_entity_registry_enabled_default)
                self.assertTrue(entity._attr_entity_registry_visible_default)
                continue
            spec = metadata[entity.spec.key]
            self.assertEqual(entity._attr_unique_id, f"pump-id_{spec.key}")
            self.assertEqual(entity._attr_name, "B1.06 Heating — Heat Stop")
            self.assertEqual(
                (entity.native_min_value, entity.native_max_value), (spec.min_value, spec.max_value)
            )
            self.assertEqual(entity._attr_native_step, spec.step)
            self.assertEqual(entity._attr_mode, NumberMode.SLIDER)
            self.assertEqual(entity._attr_entity_category, EntityCategory.CONFIG)
            self.assertTrue(entity._attr_entity_registry_enabled_default)
            self.assertTrue(entity._attr_entity_registry_visible_default)
            self.assertEqual(entity.native_value, self.coordinator.value(spec.key))
            self.assertEqual(
                entity.extra_state_attributes["holding_register_address"], spec.address
            )
        self.assertTrue({"hot_water_start", "hot_water_stop"}.isdisjoint(metadata))
        self.assertEqual(self.coordinator.writes, [])
        heat_stop = next(entity for entity in numbers if hasattr(entity, "spec"))
        await heat_stop.async_set_native_value(18)
        self.assertEqual(heat_stop.native_value, 18)
        self.assertEqual(self.coordinator.writes, [("heating_season_stop", 18)])
        self.assertIn(("native_setting", ("heating_season_stop", 18)), self.coordinator.commands)

    async def test_native_numbers_create_only_supported_finite_readbacks(self):
        for invalid in (None, True, float("nan"), float("inf"), "20", 10**500):
            with self.subTest(invalid=invalid):
                self.coordinator.registers["heating_season_stop"] = invalid
                numbers = []
                await MODULES["number"].async_setup_entry(
                    None, SimpleNamespace(runtime_data=self.coordinator), numbers.extend
                )
                self.assertFalse(any(hasattr(entity, "spec") for entity in numbers))
                self.assertEqual(len(numbers), 17)
        self.coordinator.registers["heating_season_stop"] = 45
        numbers = []
        await MODULES["number"].async_setup_entry(
            None, SimpleNamespace(runtime_data=self.coordinator), numbers.extend
        )
        self.assertEqual(
            {entity.spec.key for entity in numbers if hasattr(entity, "spec")},
            {"heating_season_stop"},
        )
        heat_stop = next(
            entity
            for entity in numbers
            if hasattr(entity, "spec") and entity.spec.key == "heating_season_stop"
        )
        self.assertEqual(heat_stop.native_value, 45)
        self.assertTrue(heat_stop.available)
        self.coordinator.registers["heating_season_stop"] = None
        self.assertFalse(heat_stop.available)
        self.assertIsNone(heat_stop.native_value)
        self.coordinator.registers["heating_season_stop"] = 17
        self.coordinator.last_update_success = False
        self.assertFalse(heat_stop.available)
        self.assertEqual(self.coordinator.writes, [])

    async def test_native_sliders_reject_unsafe_crossed_supply_bounds_and_non_step_values(self):
        metadata = MODULES["native_settings"].NATIVE_SETTINGS
        number_class = MODULES["number"].ThermiaNativeSettingNumber
        for key, value in (
            ("min_supply_temperature", 50),
            ("max_supply_temperature", 19),
            ("heating_season_stop", 41),
            ("heat_curve_supply_1", 20.5),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                await number_class(self.coordinator, metadata[key]).async_set_native_value(value)
        self.assertEqual(self.coordinator.writes, [])

    async def test_changing_native_supply_limit_restores_excess_heating_first(self):
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        await heating.async_set_preset_mode("Excess Energy")
        self.assertTrue(self.coordinator.value("fixed_supply_enabled"))
        entity = MODULES["number"].ThermiaNativeSettingNumber(
            self.coordinator, MODULES["native_settings"].NATIVE_SETTINGS["max_supply_temperature"]
        )
        await entity.async_set_native_value(40)
        self.assertFalse(self.coordinator.value("fixed_supply_enabled"))
        self.assertEqual(heating.preset_mode, "Normal")
        self.assertEqual(entity.native_value, 40)
        self.assertLess(
            self.coordinator.writes.index(("fixed_supply_enabled", False)),
            self.coordinator.writes.index(("max_supply_temperature", 40)),
        )

    def test_both_climates_expose_duration_deadline_and_remaining_hours(self):
        self.coordinator.engine.settings.update(heating_excess_hours=12, hot_water_excess_hours=6)
        self.coordinator.engine.state.update(
            heating_preset="pv_charge",
            hot_water_mode="energy_excess",
            heating_excess_started_at=1000,
            heating_excess_deadline=44200,
            hot_water_excess_started_at=1000,
            hot_water_excess_deadline=22600,
        )
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        with patch.object(self.coordinator.engine, "now", return_value=4600):
            for entity, hours, remaining in ((heating, 12, 11), (boiler, 6, 5)):
                attrs = entity.extra_state_attributes
                self.assertEqual(attrs["excess_energy_duration_hours"], hours)
                self.assertEqual(attrs["excess_energy_remaining_hours"], remaining)
                self.assertTrue(attrs["excess_energy_started_at"].endswith("+00:00"))
                self.assertTrue(attrs["excess_energy_deadline"].endswith("+00:00"))
        with patch.object(self.coordinator.engine, "now", return_value=50000):
            self.assertEqual(heating.extra_state_attributes["excess_energy_remaining_hours"], 0)
        self.coordinator.engine.state.update(
            heating_excess_started_at=None, heating_excess_deadline=None
        )
        self.assertIsNone(heating.extra_state_attributes["excess_energy_deadline"])
        self.assertIsNone(heating.extra_state_attributes["excess_energy_remaining_hours"])

    async def test_countdown_sensors_are_visible_and_follow_independent_resets(self):
        self.coordinator.engine.now = Mock(return_value=1000.0)
        heating_thermostat = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler_thermostat = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await heating_thermostat.async_set_preset_mode("Excess Energy")
        await boiler_thermostat.async_set_preset_mode("Excess Energy")
        entities = []
        await MODULES["sensor"].async_setup_entry(
            None, SimpleNamespace(runtime_data=self.coordinator), entities.extend
        )
        countdowns = [
            entity
            for entity in entities
            if isinstance(entity, MODULES["sensor"].ThermiaExcessTimeRemaining)
        ]
        self.assertEqual(len(countdowns), 2)
        heating, boiler = countdowns
        for entity, key, hours in (
            (heating, "heating_excess_time_remaining", 12),
            (boiler, "hot_water_excess_time_remaining", 6),
        ):
            self.assertEqual(entity._attr_unique_id, f"pump-id_{key}")
            self.assertTrue(entity._attr_entity_registry_enabled_default)
            self.assertTrue(entity._attr_entity_registry_visible_default)
            self.assertEqual(entity._attr_device_class, SensorDeviceClass.DURATION)
            self.assertEqual(entity._attr_native_unit_of_measurement, UnitOfTime.HOURS)
            self.assertEqual(entity.native_value, hours)
            self.assertTrue(entity.extra_state_attributes["active"])
            self.assertFalse(entity.extra_state_attributes["reset_pending"])
        self.coordinator.engine.now.return_value = 4600.0
        self.assertEqual(heating.native_value, 11)
        self.assertEqual(boiler.native_value, 5)
        await heating_thermostat.async_set_preset_mode("Excess Energy")
        self.assertEqual(heating.native_value, 12)
        self.assertEqual(boiler.native_value, 5)
        await heating_thermostat.async_set_preset_mode("Normal")
        writes_after_control = list(self.coordinator.writes)
        self.assertEqual(heating.native_value, 0)
        self.assertFalse(heating.extra_state_attributes["active"])
        self.assertEqual(self.coordinator.writes, writes_after_control)

    async def test_countdown_and_guide_local_updates_continue_offline_and_remove_cleanly(self):
        self.coordinator.engine.now = Mock(return_value=1000.0)
        self.coordinator.engine.state.update(
            hot_water_mode="energy_excess",
            hot_water_excess_started_at=1000.0,
            hot_water_excess_deadline=22600.0,
        )
        entity = MODULES["sensor"].ThermiaExcessTimeRemaining(self.coordinator, "hot_water")
        entity.hass = SimpleNamespace()
        guide = MODULES["sensor"].ThermiaControlLogic(self.coordinator)
        guide.hass = entity.hass
        cancel = Mock()
        cancel_guide = Mock()
        with patch.object(
            MODULES["sensor"], "async_track_time_interval", side_effect=(cancel, cancel_guide)
        ) as track:
            await entity.async_added_to_hass()
            await guide.async_added_to_hass()
        self.assertEqual(track.call_count, 2)
        for invocation, callback in zip(
            track.call_args_list, (entity._async_update_countdown, guide._async_update_guide)
        ):
            self.assertEqual(invocation.args, (entity.hass, callback, timedelta(seconds=30)))
            self.assertTrue(callback._hass_callback)
        scheduled_callback = track.call_args_list[0].args[1]
        scheduled_guide_callback = track.call_args_list[1].args[1]
        self.assertTrue(scheduled_callback._hass_callback)
        self.coordinator.last_update_success = False
        original_state = deepcopy(self.coordinator.engine.state)
        original_settings = deepcopy(self.coordinator.engine.settings)
        for now in (1030.0, 1060.0, 22600.0, 22630.0):
            self.coordinator.engine.now.return_value = now
            scheduled_callback(datetime.fromtimestamp(now, UTC))
            scheduled_guide_callback(datetime.fromtimestamp(now, UTC))
            self.assertTrue(entity.available)
            self.assertTrue(guide.available)
            self.assertFalse(entity.extra_state_attributes["controller_available"])
        self.assertEqual(entity.state_updates[-2:], [0, 0])
        self.assertGreater(entity.state_updates[0], entity.state_updates[1])
        self.assertTrue(entity.extra_state_attributes["reset_pending"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.engine.settings, original_settings)
        self.coordinator.engine.settings["heating_excess_hours"] = 2
        scheduled_guide_callback(datetime.fromtimestamp(22660.0, UTC))
        self.assertIn("2 hours", guide.attribute_updates[-1]["Timers And Reset"])
        self.assertNotIn("12 hours", guide.attribute_updates[-1]["Timers And Reset"])
        self.assertEqual(guide.state_updates, ["Open for explanation"] * 5)
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(self.coordinator.commands, [])
        entity._call_on_remove_callbacks()
        guide._call_on_remove_callbacks()
        cancel.assert_called_once_with()
        cancel_guide.assert_called_once_with()

    def test_sliders_require_valid_physical_readback_and_successful_poll(self):
        entity = MODULES["number"].ThermiaHotWaterStart(self.coordinator)
        for invalid in (None, float("nan"), float("inf"), True, "unavailable"):
            self.coordinator.registers["hot_water_start"] = invalid
            self.assertFalse(entity.available)
            self.assertIsNone(entity.native_value)
        self.coordinator.registers["hot_water_start"] = 45
        self.coordinator.last_update_success = False
        self.assertFalse(entity.available)

    async def test_six_visible_configuration_switches_keep_existing_ids(self):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        switches = []
        await MODULES["switch"].async_setup_entry(None, entry, switches.extend)
        self.assertEqual(len(switches), 7)
        self.assertEqual(
            {entity._attr_unique_id for entity in switches},
            {
                "pump-id_heating_enabled",
                "pump-id_hot_water_enabled",
                "pump-id_passive_cooling",
                "pump-id_auxiliary_heater",
                "pump-id_anti_legionella",
                "pump-id_hot_water_boost",
                "pump-id_cooling_humidity_enabled",
            },
        )
        for entity in switches:
            self.assertTrue(entity._attr_entity_registry_enabled_default)
            self.assertTrue(entity._attr_entity_registry_visible_default)
            self.assertEqual(entity._attr_entity_category, EntityCategory.CONFIG)
        heating = next(
            entity
            for entity in switches
            if getattr(entity, "_action", None) == "native_heating_enabled"
        )
        cooling = next(
            entity
            for entity in switches
            if getattr(entity, "_action", None) == "native_cooling_enabled"
        )
        hot_water = next(
            entity for entity in switches if getattr(entity, "_action", None) == "hot_water_enabled"
        )
        self.assertEqual(heating._attr_name, "A1.01 Heating")
        self.assertEqual(cooling._attr_name, "A1.06 Cooling (Passive)")
        await cooling.async_turn_on()
        self.assertTrue(heating.is_on)
        self.assertTrue(cooling.is_on)
        await heating.async_turn_off()
        self.assertFalse(heating.is_on)
        self.assertTrue(cooling.is_on)
        await hot_water.async_turn_off()
        self.assertEqual(self.coordinator.engine.state["hot_water_mode"], "off")
        self.assertIn(("native_heating_enabled", False), self.coordinator.commands)
        self.assertIn(("native_cooling_enabled", True), self.coordinator.commands)
        self.assertIn(("hot_water_enabled", False), self.coordinator.commands)

    async def test_native_heating_switch_and_logical_hot_water_switch_report_their_functions(self):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        entities = []
        await MODULES["switch"].async_setup_entry(None, entry, entities.extend)
        heating = next(
            entity
            for entity in entities
            if getattr(entity, "_action", None) == "native_heating_enabled"
        )
        water = next(
            entity for entity in entities if getattr(entity, "_action", None) == "hot_water_enabled"
        )
        self.coordinator.engine.state.update(heating_mode="heat", hot_water_mode="energy_excess")
        self.coordinator.registers.update(heating_enabled=False, hot_water_enabled=False)
        self.assertFalse(heating.is_on)
        self.assertTrue(water.is_on)
        self.coordinator.engine.state["hot_water_mode"] = "off"
        self.assertFalse(water.is_on)
        self.coordinator.registers["passive_cooling_enabled"] = None
        self.assertTrue(heating.available)

    async def test_native_boost_switch_does_not_change_thresholds_or_require_charge_limits(self):
        boost = MODULES["switch"].ThermiaBoostSwitch(self.coordinator)
        self.coordinator.engine.settings.update(
            max_hot_water_temperature=None,
            max_start_temperature=None,
            enable_undocumented_controls=False,
        )
        self.coordinator.registers.update(
            hot_water_top_temperature=None, hot_water_weighted_temperature=None
        )
        self.assertTrue(boost.available)
        self.assertEqual(boost._attr_unique_id, "pump-id_hot_water_boost")
        self.assertEqual(boost._attr_name, "A1.04 Thermia Boost")
        await boost.async_turn_on()
        self.assertTrue(boost.is_on)
        self.assertEqual(self.coordinator.writes, [("hot_water_boost", True)])
        self.assertEqual(
            (self.coordinator.value("hot_water_start"), self.coordinator.value("hot_water_stop")),
            (45, 55),
        )
        await boost.async_turn_off()
        self.assertFalse(boost.is_on)
        self.assertEqual(self.coordinator.writes[-1], ("hot_water_boost", False))
        self.assertIn(("native_hot_water_boost", True), self.coordinator.commands)

    def test_native_boost_malformed_or_missing_flag_is_unavailable(self):
        self.coordinator.engine.state["native_boost_coupled"] = True
        boost = MODULES["switch"].ThermiaBoostSwitch(self.coordinator)
        for invalid in (None, 20000, 2, -1, float("nan"), "1"):
            self.coordinator.registers["hot_water_boost"] = invalid
            self.assertFalse(boost.available)
            self.assertIsNone(boost.is_on)
            attributes = boost.extra_state_attributes
            self.assertIsNone(attributes["native_boost_enabled"])
            self.assertFalse(attributes["native_boost_available"])
            self.assertFalse(attributes["native_boost_coupled"])
            self.assertEqual(attributes["native_boost_status"], "Unavailable on controller")
        self.coordinator.registers["hot_water_boost"] = False
        self.assertTrue(boost.available)
        self.assertTrue(boost.extra_state_attributes["native_boost_available"])
        self.assertEqual(boost.extra_state_attributes["native_boost_status"], "Off")
        self.coordinator.last_update_success = False
        self.assertFalse(boost.available)

    async def test_anti_legionella_uses_valid_native_flag_without_checkbox_gate(self):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        entities = []
        await MODULES["switch"].async_setup_entry(None, entry, entities.extend)
        anti = next(
            entity for entity in entities if getattr(entity, "_action", None) == "anti_legionella"
        )
        self.assertFalse(anti.available)
        self.coordinator.registers["anti_legionella_enabled"] = False
        self.assertTrue(anti.available)
        await anti.async_turn_on()
        self.assertTrue(anti.is_on)
        self.coordinator.registers["anti_legionella_enabled"] = 20000
        self.assertFalse(anti.available)
        self.assertIsNone(anti.is_on)

    async def test_boiler_auto_card_exposes_one_30_to_60_degree_range(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.assertEqual(boiler._attr_unique_id, "pump-id_hot_water")
        self.assertEqual(boiler._attr_hvac_modes, [HVACMode.OFF, HVACMode.AUTO])
        self.assertEqual(boiler.preset_modes, ["Normal", "Excess Energy", "Low Mode"])
        self.assertEqual((boiler.min_temp, boiler.max_temp), (30, 60))
        self.assertEqual(boiler._attr_target_temperature_step, 1)
        self.assertTrue(boiler.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)
        self.assertFalse(boiler.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE)
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        self.assertEqual(boiler.preset_mode, "Normal")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))
        self.assertEqual(boiler.current_temperature, 50)
        self.assertEqual(boiler.hvac_action, HVACAction.IDLE)
        self.coordinator.active_demands = ["Heating"]
        self.assertEqual(boiler.hvac_action, HVACAction.IDLE)
        self.coordinator.active_demands.append("Hot water")
        self.assertEqual(boiler.hvac_action, HVACAction.HEATING)
        await boiler.async_set_temperature(target_temp_low=56, target_temp_high=58)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (56, 58))
        self.assertIn(
            ("hot_water_temperature_edit", {"target_temp_low": 56, "target_temp_high": 58}),
            self.coordinator.commands,
        )

    def test_boiler_tank_fallback_and_invalid_native_values(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.registers["hot_water_lower_temperature"] = 24.0
        self.assertEqual(boiler.current_temperature, 50)
        attrs = boiler.extra_state_attributes
        self.assertEqual(attrs["current_temperature_source"], "weighted")
        self.assertEqual(attrs["hot_water_weighted_temperature"], 50)
        self.assertEqual(attrs["hot_water_top_temperature"], 51)
        self.assertEqual(attrs["hot_water_lower_temperature"], 24)
        for invalid in (None, float("nan"), float("inf"), True, "50"):
            with self.subTest(weighted=invalid):
                self.coordinator.registers["hot_water_weighted_temperature"] = invalid
                self.assertEqual(boiler.current_temperature, 51)
                attrs = boiler.extra_state_attributes
                self.assertEqual(attrs["current_temperature_source"], "top")
                self.assertIsNone(attrs["hot_water_weighted_temperature"])
                self.assertEqual(attrs["hot_water_top_temperature"], 51)
        self.coordinator.registers["hot_water_top_temperature"] = None
        self.assertIsNone(boiler.current_temperature)
        attrs = boiler.extra_state_attributes
        self.assertEqual(attrs["current_temperature_source"], "unavailable")
        self.assertIsNone(attrs["hot_water_top_temperature"])
        self.assertEqual(attrs["hot_water_lower_temperature"], 24)
        for invalid in (float("nan"), float("inf"), True, "24"):
            self.coordinator.registers["hot_water_lower_temperature"] = invalid
            self.assertIsNone(boiler.extra_state_attributes["hot_water_lower_temperature"])
        for invalid in (None, float("nan"), float("inf"), True, "45"):
            self.coordinator.registers["hot_water_start"] = invalid
            self.assertIsNone(boiler.target_temperature_low)
            self.assertFalse(boiler.available)
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(self.coordinator.commands, [])
        self.coordinator.registers["hot_water_start"] = 45
        self.coordinator.last_update_success = False
        self.assertFalse(boiler.available)

    async def test_boiler_energy_excess_holds_restart_pair_then_auto_restores_original(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        boost = MODULES["switch"].ThermiaBoostSwitch(self.coordinator)
        await boiler.async_set_preset_mode("Excess Energy")
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        self.assertEqual(boiler.preset_mode, "Excess Energy")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (58, 60))
        self.assertTrue(boost.is_on)
        self.assertTrue(boiler.extra_state_attributes["energy_excess_active"])
        with self.assertRaisesRegex(ValueError, "Normal"):
            await boost.async_turn_off()
        self.assertTrue(boost.is_on)
        self.assertEqual(
            (
                boiler.extra_state_attributes["normal_start_temperature"],
                boiler.extra_state_attributes["normal_stop_temperature"],
            ),
            (45, 55),
        )
        self.coordinator.registers.update(
            hot_water_top_temperature=60, hot_water_weighted_temperature=60
        )
        await self.coordinator.engine.tick(22, 12)
        self.assertEqual(boiler.preset_mode, "Excess Energy")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (58, 60))
        self.assertFalse(boost.is_on)
        self.assertEqual(boost.extra_state_attributes["native_boost_status"], "Paused at 60°C")
        self.coordinator.registers.update(
            hot_water_top_temperature=59, hot_water_weighted_temperature=59
        )
        await self.coordinator.engine.tick(22, 12)
        self.assertFalse(boost.is_on)
        self.coordinator.registers.update(
            hot_water_top_temperature=58, hot_water_weighted_temperature=58
        )
        await self.coordinator.engine.tick(22, 12)
        self.assertTrue(boost.is_on)
        self.assertEqual(boost.extra_state_attributes["native_boost_status"], "Active")
        await boiler.async_set_preset_mode("Normal")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))
        self.assertFalse(boost.is_on)
        self.assertFalse(boiler.extra_state_attributes["energy_excess_active"])
        self.assertEqual(boiler.preset_mode, "Normal")

    async def test_boiler_energy_edit_returns_normal_and_preserves_untouched_saved_endpoint(self):
        for request, expected in (
            ({"target_temp_low": 58, "target_temp_high": 59}, (45, 59)),
            ({"target_temp_low": 46, "target_temp_high": 60}, (46, 50)),
            ({"target_temp_low": 58, "target_temp_high": 58}, (45, 58)),
            ({"target_temp_high": 55}, (45, 55)),
            ({"target_temp_low": 40}, (40, 50)),
            ({"target_temp_low": 46, "target_temp_high": 56}, (46, 56)),
            ({"target_temp_low": 46, "target_temp_high": 56, "hvac_mode": "auto"}, (46, 56)),
        ):
            with self.subTest(request=request):
                coordinator = Coordinator()
                coordinator.registers["hot_water_stop"] = 50
                boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
                await boiler.async_set_preset_mode("Excess Energy")
                coordinator.commands.clear()
                await boiler.async_set_temperature(**request)
                self.assertEqual(coordinator.commands, [("hot_water_temperature_edit", request)])
                self.assertEqual(boiler.preset_mode, "Normal")
                self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
                self.assertEqual(
                    (boiler.target_temperature_low, boiler.target_temperature_high), expected
                )
                self.assertEqual(
                    (
                        boiler.extra_state_attributes["normal_start_temperature"],
                        boiler.extra_state_attributes["normal_stop_temperature"],
                    ),
                    expected,
                )
                self.assertFalse(coordinator.value("hot_water_boost"))
                self.assertFalse(boiler.extra_state_attributes["energy_excess_active"])
                self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])

    async def test_boiler_unchanged_preset_pair_does_not_cancel_or_restart_timer(self):
        for preset, pair in (("Excess Energy", (58, 60)), ("Low Mode", (35, 40))):
            with self.subTest(preset=preset):
                coordinator = Coordinator()
                coordinator.engine.now = Mock(return_value=1000)
                boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
                await boiler.async_set_preset_mode(preset)
                deadline = coordinator.engine.state["hot_water_excess_deadline"]
                writes = list(coordinator.writes)
                await boiler.async_set_temperature(
                    target_temp_low=pair[0], target_temp_high=pair[1]
                )
                self.assertEqual(boiler.preset_mode, preset)
                self.assertEqual(
                    (boiler.target_temperature_low, boiler.target_temperature_high), pair
                )
                self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], deadline)
                self.assertEqual(coordinator.writes, writes)

    async def test_colliding_boiler_edits_repair_normal_pair_in_one_atomic_command(self):
        for preset, request, expected in (
            ("Excess Energy", {"target_temp_low": 55, "target_temp_high": 60}, (55, 60)),
            ("Excess Energy", {"target_temp_low": 59, "target_temp_high": 60}, (55, 60)),
            ("Low Mode", {"target_temp_low": 35, "target_temp_high": 41}, (36, 41)),
            ("Normal", {"target_temp_low": 50, "target_temp_high": 50}, (50, 55)),
            ("Normal", {"target_temp_low": 45, "target_temp_high": 45}, (40, 45)),
        ):
            with self.subTest(preset=preset):
                coordinator = Coordinator()
                coordinator.registers["hot_water_stop"] = 50
                coordinator.engine.state["hot_water_target"] = 50
                coordinator.engine.now = Mock(return_value=1000)
                boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
                await boiler.async_set_preset_mode(preset)
                coordinator.commands.clear()
                await boiler.async_set_temperature(**request)
                self.assertEqual(coordinator.commands, [("hot_water_temperature_edit", request)])
                self.assertEqual(boiler.preset_mode, "Normal")
                self.assertEqual(
                    (boiler.target_temperature_low, boiler.target_temperature_high), expected
                )
                self.assertEqual(
                    (
                        boiler.extra_state_attributes["normal_start_temperature"],
                        boiler.extra_state_attributes["normal_stop_temperature"],
                    ),
                    expected,
                )
                self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
                self.assertIsNone(coordinator.engine.state["hot_water_low_deadline"])
                self.assertFalse(coordinator.value("hot_water_boost"))

    async def test_impossible_boiler_repair_preserves_low_preset_pair_and_timer(self):
        coordinator = Coordinator()
        coordinator.registers.update(hot_water_start=30, hot_water_stop=31)
        coordinator.engine.settings.update(
            max_start_temperature=30,
            max_hot_water_temperature=34,
            hot_water_evening_start=30,
            hot_water_evening_stop=31,
        )
        coordinator.engine.now = Mock(return_value=1000)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
        await boiler.async_set_preset_mode("Low Mode")
        state = deepcopy(coordinator.engine.state)
        settings = deepcopy(coordinator.engine.settings)
        writes = list(coordinator.writes)
        with self.assertRaisesRegex(ValueError, "5°C gap"):
            await boiler.async_set_temperature(target_temp_low=30, target_temp_high=30)
        self.assertEqual(boiler.preset_mode, "Low Mode")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (30, 31))
        self.assertIn("5°C gap", coordinator.engine.state["control_warning"])
        self.assertEqual(
            {
                key: value
                for key, value in coordinator.engine.state.items()
                if key != "control_warning"
            },
            {key: value for key, value in state.items() if key != "control_warning"},
        )
        self.assertEqual(coordinator.engine.settings, settings)
        self.assertEqual(coordinator.writes, writes)

    async def test_boiler_keeps_valid_narrow_pair_instead_of_applying_collision_repair(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.engine.settings.update(
            max_start_temperature=55, max_hot_water_temperature=60
        )
        request = {"target_temp_low": 54, "target_temp_high": 55}
        await boiler.async_set_temperature(**request)
        self.assertEqual(self.coordinator.commands, [("hot_water_temperature_edit", request)])
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (54, 55))

    async def test_oversized_boiler_start_settles_at_native_cap_and_returns_presets_to_normal(self):
        for preset in ("Normal", "Excess Energy", "Low Mode"):
            with self.subTest(preset=preset):
                coordinator = Coordinator()
                coordinator.engine.settings.update(
                    max_start_temperature=55, max_hot_water_temperature=60
                )
                coordinator.engine.now = Mock(return_value=1000)
                boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
                await boiler.async_set_preset_mode(preset)
                coordinator.commands.clear()
                request = {"target_temp_low": 57, "target_temp_high": 60}
                await boiler.async_set_temperature(**request)
                self.assertEqual(coordinator.commands, [("hot_water_temperature_edit", request)])
                self.assertEqual(
                    (boiler.target_temperature_low, boiler.target_temperature_high), (55, 60)
                )
                self.assertEqual(boiler.max_temp, 60)
                self.assertEqual(boiler.target_temperature_step, 1)
                self.assertEqual(boiler.preset_mode, "Normal")
                self.assertFalse(coordinator.value("hot_water_boost"))
                self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
                self.assertIsNone(coordinator.engine.state["hot_water_low_deadline"])
                self.assertEqual(boiler.extra_state_attributes["normal_start_temperature"], 55)
                self.assertEqual(boiler.extra_state_attributes["normal_stop_temperature"], 60)

    async def test_capped_boiler_edit_publishes_accepted_target_once_even_without_native_changes(
        self,
    ):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.registers.update(hot_water_start=55, hot_water_stop=60)
        self.coordinator.engine.state.update(hot_water_start=55, hot_water_target=60)
        self.coordinator.engine.settings.update(
            max_start_temperature=55, max_hot_water_temperature=60
        )
        self.assertFalse(boiler.force_update)
        await boiler.async_set_temperature(target_temp_low=57, target_temp_high=60)
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(boiler.state_updates, ["auto"])
        self.assertEqual(boiler.force_update_publications, [True])
        self.assertEqual(boiler.attribute_updates[0]["target_temp_low"], 55)
        self.assertEqual(boiler.attribute_updates[0]["target_temp_high"], 60)
        self.assertFalse(boiler.force_update)
        await self.coordinator.engine.tick(22, 12)
        self.assertEqual(boiler.force_update_publications, [True])

    async def test_failed_boiler_temperature_write_does_not_publish_successful_acceptance(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.engine.settings.update(
            max_start_temperature=55, max_hot_water_temperature=60
        )

        async def fail_stop(key, value):
            if key == "hot_water_stop" and value == 60:
                raise ValueError("Native stop write failed")
            await self.coordinator.write(key, value)

        self.coordinator.engine.write = fail_stop
        with self.assertRaisesRegex(ValueError, "Native stop write failed"):
            await boiler.async_set_temperature(target_temp_low=57, target_temp_high=60)
        self.assertEqual(boiler.state_updates, [])
        self.assertEqual(boiler.force_update_publications, [])
        self.assertFalse(boiler.force_update)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))

    async def test_temperature_acceptance_restores_force_update_when_state_publication_raises(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        boiler.async_write_ha_state = Mock(side_effect=RuntimeError("State publication failed"))
        with self.assertRaisesRegex(RuntimeError, "State publication failed"):
            await boiler.async_set_temperature(target_temp_low=46, target_temp_high=56)
        self.assertFalse(boiler.force_update)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (46, 56))

    async def test_boiler_off_exits_energy_and_turns_off_original_native_boost(self):
        self.coordinator.registers["hot_water_boost"] = True
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await boiler.async_set_preset_mode("Excess Energy")
        await boiler.async_turn_off()
        self.assertEqual(boiler.hvac_mode, HVACMode.OFF)
        self.assertIsNone(boiler.preset_mode)
        self.assertFalse(self.coordinator.value("hot_water_boost"))
        self.assertEqual(
            (boiler.target_temperature_low, boiler.target_temperature_high), (None, None)
        )
        self.assertEqual(
            (self.coordinator.value("hot_water_start"), self.coordinator.value("hot_water_stop")),
            (45, 55),
        )
        await boiler.async_turn_on()
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        self.assertEqual(self.coordinator.engine.state["hot_water_mode"], "auto")

    async def test_boiler_low_mode_uses_configured_native_pair_and_preserves_normal_settings(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        countdown = MODULES["sensor"].ThermiaExcessTimeRemaining(self.coordinator, "hot_water")
        await boiler.async_set_preset_mode("Low Mode")
        self.assertIn(("hot_water_mode", "evening"), self.coordinator.commands)
        self.assertEqual(boiler.preset_mode, "Low Mode")
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (35, 40))
        attrs = boiler.extra_state_attributes
        self.assertTrue(attrs["low_mode_active"])
        self.assertTrue(attrs["evening_active"])
        self.assertFalse(attrs["energy_excess_active"])
        self.assertEqual(attrs["evening_start_temperature"], 35)
        self.assertEqual(attrs["evening_stop_temperature"], 40)
        self.assertEqual(attrs["low_mode_start_temperature"], 35)
        self.assertEqual(attrs["low_mode_stop_temperature"], 40)
        self.assertEqual(attrs["normal_start_temperature"], 45)
        self.assertEqual(attrs["normal_stop_temperature"], 55)
        self.assertEqual(countdown.native_value, 0)
        self.assertFalse(countdown.extra_state_attributes["active"])
        self.assertIsNone(self.coordinator.engine.state["hot_water_excess_deadline"])
        self.assertEqual(heating.preset_modes, ["Normal", "Excess Energy", "Low Mode", "Vacation"])
        await heating.async_set_preset_mode("Low Mode")
        self.assertEqual(heating.preset_mode, "Low Mode")
        self.assertEqual(heating.target_temperature, 18)
        await boiler.async_set_preset_mode("Normal")
        self.assertEqual(boiler.preset_mode, "Normal")
        self.assertFalse(boiler.extra_state_attributes["evening_active"])
        self.assertFalse(boiler.extra_state_attributes["low_mode_active"])
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))

    async def test_low_mode_legacy_direct_alias_keeps_internal_settings_but_advertises_new_label(
        self,
    ):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.engine.settings.update(
            hot_water_evening_start=37, hot_water_evening_stop=44
        )
        await boiler.async_set_preset_mode("Evening")
        self.assertEqual(boiler.preset_mode, "Low Mode")
        self.assertNotIn("Evening", boiler.preset_modes)
        self.assertIn("Low Mode", boiler.preset_modes)
        self.assertEqual(self.coordinator.engine.state["hot_water_mode"], "evening")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (37, 44))
        attrs = boiler.extra_state_attributes
        self.assertEqual(attrs["low_mode_start_temperature"], attrs["evening_start_temperature"])
        self.assertEqual(attrs["low_mode_stop_temperature"], attrs["evening_stop_temperature"])
        await boiler.async_set_preset_mode("Normal")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))

    async def test_low_mode_configured_pair_does_not_start_or_reset_heating_countdown(self):
        self.coordinator.engine.settings.update(
            hot_water_evening_start=37, hot_water_evening_stop=44
        )
        self.coordinator.engine.now = Mock(return_value=1000.0)
        heating = MODULES["climate"].ThermiaClimate(self.coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await heating.async_set_preset_mode("Excess Energy")
        deadline = self.coordinator.engine.state["heating_excess_deadline"]
        self.coordinator.engine.now.return_value = 4600.0
        await boiler.async_set_preset_mode("Low Mode")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (37, 44))
        self.assertEqual(boiler.extra_state_attributes["low_mode_start_temperature"], 37)
        self.assertEqual(boiler.extra_state_attributes["low_mode_stop_temperature"], 44)
        self.assertEqual(self.coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertEqual(heating.preset_mode, "Excess Energy")
        self.assertIsNone(self.coordinator.engine.state["hot_water_excess_deadline"])

    async def test_low_mode_temperature_edit_applies_normal_pair_and_keeps_configured_low_profile(
        self,
    ):
        for request, expected in (
            ({"target_temp_low": 36, "target_temp_high": 40}, (36, 50)),
            ({"target_temp_low": 40, "target_temp_high": 40}, (40, 50)),
            ({"target_temp_low": 35, "target_temp_high": 55}, (45, 55)),
            ({"target_temp_low": 36, "target_temp_high": 41}, (36, 41)),
            ({"target_temp_low": 36}, (36, 50)),
        ):
            with self.subTest(request=request):
                coordinator = Coordinator()
                coordinator.registers["hot_water_stop"] = 50
                boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
                await boiler.async_set_preset_mode("Low Mode")
                coordinator.commands.clear()
                await boiler.async_set_temperature(**request)
                self.assertEqual(coordinator.commands, [("hot_water_temperature_edit", request)])
                self.assertEqual(boiler.preset_mode, "Normal")
                self.assertFalse(boiler.extra_state_attributes["low_mode_active"])
                self.assertEqual(
                    (boiler.target_temperature_low, boiler.target_temperature_high), expected
                )
                self.assertEqual(
                    boiler.hvac_mode,
                    HVACMode.AUTO,
                )
                self.assertEqual(
                    (
                        boiler.extra_state_attributes["low_mode_start_temperature"],
                        boiler.extra_state_attributes["low_mode_stop_temperature"],
                    ),
                    (35, 40),
                )
                self.assertFalse(coordinator.value("hot_water_boost"))

    async def test_off_hot_water_rejects_temperature_edit_and_preserves_direct_boost_flag(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await boiler.async_turn_off()
        self.coordinator.registers["hot_water_boost"] = True
        self.coordinator.commands.clear()
        self.coordinator.writes.clear()
        with self.assertRaisesRegex(HomeAssistantError, "Turn on hot water"):
            await boiler.async_set_temperature(target_temp_low=46, target_temp_high=56)
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(boiler.hvac_mode, HVACMode.OFF)
        self.assertIsNone(boiler.preset_mode)
        self.assertFalse(self.coordinator.value("hot_water_enabled"))
        self.assertTrue(self.coordinator.value("hot_water_boost"))
        self.assertEqual(
            (boiler.target_temperature_low, boiler.target_temperature_high), (None, None)
        )
        self.assertEqual(
            (self.coordinator.value("hot_water_start"), self.coordinator.value("hot_water_stop")),
            (45, 55),
        )

    async def test_low_mode_off_and_auto_exit_restore_normal_pair(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await boiler.async_set_preset_mode("Low Mode")
        await boiler.async_turn_off()
        self.assertEqual(boiler.hvac_mode, HVACMode.OFF)
        self.assertIsNone(boiler.preset_mode)
        self.assertFalse(boiler.extra_state_attributes["evening_active"])
        self.assertEqual(
            (boiler.target_temperature_low, boiler.target_temperature_high), (None, None)
        )
        self.assertEqual(
            (self.coordinator.value("hot_water_start"), self.coordinator.value("hot_water_stop")),
            (45, 55),
        )
        await boiler.async_turn_on()
        self.assertEqual(boiler.hvac_mode, HVACMode.AUTO)
        self.assertEqual(boiler.preset_mode, "Normal")
        await boiler.async_set_preset_mode("Low Mode")
        await boiler.async_set_hvac_mode(HVACMode.AUTO)
        self.assertEqual(boiler.preset_mode, "Normal")
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (45, 55))
        await boiler.async_set_temperature(target_temp_low=46, target_temp_high=56)
        self.assertEqual((boiler.target_temperature_low, boiler.target_temperature_high), (46, 56))

    async def test_boiler_energy_excess_refuses_unverified_or_unsupported_native_controls(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        self.coordinator.engine.settings["max_start_temperature"] = 29
        with self.assertRaises(ValueError):
            await boiler.async_set_preset_mode("Excess Energy")
        self.assertEqual(self.coordinator.writes, [])
        self.coordinator.engine.settings["max_start_temperature"] = 60
        self.coordinator.registers["hot_water_boost"] = 20000
        with self.assertRaises(ValueError):
            await boiler.async_set_preset_mode("Excess Energy")
        self.assertEqual(self.coordinator.writes, [])

    async def test_boiler_ranges_reject_out_of_bounds_and_single_temperature(self):
        boiler = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        for pair in ((29, 55), (45, 61)):
            with self.assertRaises(ValueError):
                await boiler.async_set_temperature(
                    target_temp_low=pair[0], target_temp_high=pair[1]
                )
        with self.assertRaisesRegex(HomeAssistantError, "both"):
            await boiler.async_set_temperature(temperature=55)
        self.assertEqual(self.coordinator.writes, [])

    async def test_control_logic_is_visible_on_device_and_available_offline(self):
        sensors = []
        await MODULES["sensor"].async_setup_entry(
            None, SimpleNamespace(runtime_data=self.coordinator), sensors.extend
        )
        guides = [
            entity
            for entity in sensors
            if isinstance(entity, MODULES["sensor"].ThermiaControlLogic)
        ]
        self.assertEqual(len(guides), 1)
        guide = guides[0]
        self.assertEqual(guide._attr_unique_id, "pump-id_control_logic")
        self.assertEqual(guide._attr_name, "C7.03 System — Control Logic")
        self.assertEqual(guide._attr_icon, "mdi:information-outline")
        self.assertIsNone(guide._attr_entity_category)
        self.assertTrue(guide._attr_entity_registry_enabled_default)
        self.assertTrue(guide._attr_entity_registry_visible_default)
        self.assertEqual(
            guide._attr_device_info["identifiers"],
            {(MODULES["const"].DOMAIN, self.coordinator.entry.unique_id)},
        )
        self.coordinator.last_update_success = False
        self.coordinator.engine.now = Mock(return_value=1000.0)
        original_state = deepcopy(self.coordinator.engine.state)
        original_settings = deepcopy(self.coordinator.engine.settings)
        for _ in range(2):
            self.assertTrue(guide.available)
            self.assertEqual(guide.native_value, "Open for explanation")
            self.assertIn("offline", guide.extra_state_attributes["Time Remaining"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.engine.settings, original_settings)
        self.coordinator.engine.now.assert_not_called()
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(self.coordinator.commands, [])

    def test_control_guide_explains_defaults_and_separates_requests_from_activity(self):
        guide = MODULES["sensor"].ThermiaControlLogic(self.coordinator).extra_state_attributes
        self.assertIn("23°C", guide["Heating Excess Energy"])
        self.assertIn("22.7°C", guide["Heating Excess Energy"])
        self.assertIn(
            "45°C (the pump's normal maximum heating supply temperature)",
            guide["Heating Excess Energy"],
        )
        self.assertIn("45°C; stop: 55°C", guide["Hot Water Normal"])
        self.assertIn("start to 58°C and stop to 60°C", guide["Hot Water Excess Energy"])
        self.assertIn("resumes only at 58°C or below", guide["Hot Water Excess Energy"])
        self.assertIn("start 35°C and stop 40°C", guide["Hot Water Low Mode"])
        self.assertIn("no automatic schedule", guide["Hot Water Low Mode"])
        self.assertIn("12 hours", guide["Timers And Reset"])
        self.assertIn("6 hours", guide["Timers And Reset"])
        self.assertIn("every 30 seconds", guide["Time Remaining"])
        self.assertIn("does not confirm", guide["Time Remaining"])
        self.assertIn("Maximum sensor age is off", guide["Inside And Outside Sensors"])
        self.assertIn("Heat enables native heating", guide["Heating Normal"])
        self.assertIn("regardless of the external room temperature", guide["Heating Normal"])
        self.assertIn("Auto (Heat/Cool) uses the inside reading", guide["Heating Normal"])
        self.assertIn("copies its selected temperature", guide["Heating Targets And Thermia"])
        self.assertIn(
            "no documented writable cooling room target", guide["Heating Targets And Thermia"]
        )
        self.assertIn("do not force the compressor", guide["Native Switches"])
        self.assertIn("own hygiene cycles remain separate", guide["Hot Water Low Mode"])
        self.assertIn("applies the new Normal target", guide["Heating Targets And Thermia"])
        self.assertIn("without ending the preset", guide["Heating Targets And Thermia"])
        self.assertIn("45/59°C", guide["Thermostat Temperature Changes"])
        self.assertIn("36/50°C", guide["Thermostat Temperature Changes"])
        self.assertIn("unchanged preset pair does nothing", guide["Thermostat Temperature Changes"])
        self.assertIn(
            "preset, settings and timer remain unchanged", guide["Thermostat Temperature Changes"]
        )
        self.assertNotIn("weighted", guide["Hot Water Normal"])
        self.assertIn("own hot-water mode on Normal", guide["Hot Water Normal"])
        self.assertIn(
            "displays the weighted tank temperature", guide["Hot Water Temperature Readings"]
        )
        self.assertIn("Thermia decides", guide["Hot Water Temperature Readings"])
        self.assertIn("both to pause", guide["Hot Water Temperature Readings"])
        self.assertTrue(
            all(isinstance(key, str) and isinstance(value, str) for key, value in guide.items())
        )

    def test_control_guide_uses_configured_values_and_saved_normal_pair_during_override(self):
        self.coordinator.engine.settings.update(
            max_heating_temperature=25,
            hysteresis=0.5,
            hot_water_hysteresis=8,
            heating_excess_hours=3.5,
            hot_water_excess_hours=1.25,
            hot_water_evening_start=32,
            hot_water_evening_stop=38,
            sensor_timeout=120,
        )
        self.coordinator.registers.update(
            max_supply_temperature=42, hot_water_start=52, hot_water_stop=60
        )
        self.coordinator.engine.state.update(
            heating_mode="heat_cool",
            heating_preset="pv_charge",
            hot_water_mode="evening",
            hot_water_start=49,
            hot_water_target=54,
        )
        guide = MODULES["sensor"].ThermiaControlLogic(self.coordinator).extra_state_attributes
        self.assertIn(
            "Heating: Excess Energy (Heating Only), Auto (Heat/Cool)", guide["Current Selections"]
        )
        self.assertIn("Hot water: Low Mode", guide["Current Selections"])
        self.assertIn("25°C", guide["Heating Excess Energy"])
        self.assertIn("24.5°C", guide["Heating Excess Energy"])
        self.assertIn(
            "42°C (the pump's normal maximum heating supply temperature)",
            guide["Heating Excess Energy"],
        )
        self.assertIn("49°C; stop: 54°C", guide["Hot Water Normal"])
        self.assertIn("start to 52°C and stop to 60°C", guide["Hot Water Excess Energy"])
        self.assertIn("restart gap 8°C", guide["Hot Water Excess Energy"])
        self.assertIn("start 32°C and stop 38°C", guide["Hot Water Low Mode"])
        self.assertIn("3.5 hours", guide["Timers And Reset"])
        self.assertIn("1.25 hours", guide["Timers And Reset"])
        self.assertIn("older than 2 minutes", guide["Inside And Outside Sensors"])

    def test_control_guide_explains_requested_gap_capped_by_default_start_limit(self):
        self.coordinator.engine.settings.update(
            max_start_temperature=55, max_hot_water_temperature=60
        )
        original_state = deepcopy(self.coordinator.engine.state)
        for gap in (1, 2):
            with self.subTest(gap=gap):
                self.coordinator.engine.settings["hot_water_hysteresis"] = gap
                guide = (
                    MODULES["sensor"].ThermiaControlLogic(self.coordinator).extra_state_attributes
                )
                self.assertIn("start to 55°C and stop to 60°C", guide["Hot Water Excess Energy"])
                self.assertIn(f"requested restart gap {gap}°C", guide["Hot Water Excess Energy"])
                self.assertIn("effective restart gap 5°C", guide["Hot Water Excess Energy"])
                self.assertIn("resumes only at 55°C or below", guide["Hot Water Excess Energy"])
                self.assertIn("configurable from 1 to 30°C", guide["Hot Water Excess Energy"])
                self.assertIn("5°C gap", guide["Thermostat Temperature Changes"])
                self.assertIn("49/54°C", guide["Thermostat Temperature Changes"])
                self.assertIn("36/41°C", guide["Thermostat Temperature Changes"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(self.coordinator.commands, [])

    def test_control_guide_handles_missing_invalid_settings_without_claiming_sensor_values(self):
        self.coordinator.engine.settings.update(
            max_heating_temperature=True,
            hysteresis=None,
            hot_water_hysteresis=0,
            heating_excess_hours=float("inf"),
            hot_water_excess_hours=None,
            hot_water_evening_start=45,
            hot_water_evening_stop=40,
            sensor_timeout=0,
        )
        self.coordinator.engine.state.update(hot_water_start=None, hot_water_target=True)
        self.coordinator.registers["max_supply_temperature"] = None
        guide = MODULES["sensor"].ThermiaControlLogic(self.coordinator).extra_state_attributes
        self.assertIn("23°C", guide["Heating Excess Energy"])
        self.assertIn(
            "not available; a valid native maximum heating supply temperature is required",
            guide["Heating Excess Energy"],
        )
        self.assertIn(
            "Excess Energy temperatures are unavailable", guide["Hot Water Excess Energy"]
        )
        self.assertIn("start 35°C and stop 40°C", guide["Hot Water Low Mode"])
        self.assertIn("start: not available; stop: not available", guide["Hot Water Normal"])
        self.assertIn("12 hours", guide["Timers And Reset"])
        self.assertIn("6 hours", guide["Timers And Reset"])
        text = " ".join(guide.values()).lower()
        self.assertNotIn("nan", text)
        self.assertNotIn("inf°", text)
        self.assertNotIn("modbus", text)

    async def test_all_telemetry_entities_handle_missing_optional_registers(self):
        entry = SimpleNamespace(runtime_data=self.coordinator)
        entities = []
        for platform in ("sensor", "binary_sensor"):
            await MODULES[platform].async_setup_entry(None, entry, entities.extend)
        self.assertGreater(len(entities), 200)
        self.assertEqual(len({entity._attr_unique_id for entity in entities}), len(entities))
        for entity in entities:
            # Missing accessories must not produce attribute exceptions while
            # Home Assistant builds the state of every enabled entity.
            if isinstance(entity, SensorEntity):
                _ = entity.native_value
            else:
                _ = entity.is_on
            self.assertIsInstance(entity.available, bool)
            if hasattr(entity, "extra_state_attributes"):
                _ = entity.extra_state_attributes
        self.assertEqual(self.coordinator.writes, [])


class PrimaryCoreAPIContractTests(unittest.TestCase):
    """Optional source-contract check against the exact downloaded HA tag."""

    @unittest.skipUnless(
        os.environ.get("THERMIA_HA_SOURCE"),
        "Set THERMIA_HA_SOURCE to a Core 2026.9.4 homeassistant source folder",
    )
    def test_exact_core_serialization_preserves_measured_precision_and_unit_conversion(self):
        source = Path(os.environ["THERMIA_HA_SOURCE"])
        climate_tree = ast.parse((source / "components/climate/__init__.py").read_text())
        climate_class = next(
            node
            for node in climate_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ClimateEntity"
        )
        state_function = deepcopy(
            next(
                node
                for node in climate_class.body
                if isinstance(node, ast.FunctionDef) and node.name == "state_attributes"
            )
        )
        state_function.decorator_list = []
        const_tree = ast.parse((source / "components/climate/const.py").read_text())
        state_enum = deepcopy(
            next(
                node
                for node in const_tree.body
                if isinstance(node, ast.ClassDef) and node.name == "ClimateEntityStateAttribute"
            )
        )
        state_enum.keywords = []

        def show_temp(hass, temperature, unit, precision):
            # Narrow stand-in for Core's verified temperature display helper.
            if temperature is None:
                return None
            return round(
                TemperatureConverter.convert(temperature, unit, hass.config.units.temperature_unit),
                1,
            )

        namespace = {
            "StrEnum": StrEnum,
            "ClimateEntityFeature": ClimateEntityFeature,
            "show_temp": show_temp,
        }
        module = ast.Module(body=[state_enum, state_function], type_ignores=[])
        exec(
            compile(ast.fix_missing_locations(module), "core_climate_attributes", "exec"), namespace
        )

        entity_tree = ast.parse((source / "helpers/entity.py").read_text())
        entity_class = next(
            node
            for node in entity_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Entity"
        )
        calculation = next(
            node
            for node in entity_class.body
            if isinstance(node, ast.FunctionDef) and node.name == "__async_calculate_state"
        )
        merge = deepcopy(
            next(
                node
                for node in calculation.body
                if isinstance(node, ast.If)
                and isinstance(node.test, ast.Name)
                and node.test.id == "available"
            )
        )
        serializer = ast.parse(
            "def serialize(self):\n    attr = {}\n    available = self.available\n    return attr\n"
        ).body[0]
        serializer.body.insert(2, merge)
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[serializer], type_ignores=[])),
                "core_entity_attribute_merge",
                "exec",
            ),
            namespace,
        )

        coordinator = Coordinator()
        heating = MODULES["climate"].ThermiaClimate(coordinator)
        boiler = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
        # Test the exact final Core property on our stand-ins; production uses
        # only the public extra_state_attributes hook, without overriding it.
        self.assertNotIn("state_attributes", type(heating).__dict__)
        self.assertNotIn("state_attributes", type(boiler).__dict__)
        with patch.object(
            ClimateEntity, "state_attributes", property(namespace["state_attributes"]), create=True
        ):
            for unit in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT):
                for entity in (heating, boiler):
                    entity.hass = SimpleNamespace(
                        config=SimpleNamespace(units=SimpleNamespace(temperature_unit=unit))
                    )
                for reading in (22.34, 22.345, 22.34567, 25.0):
                    with self.subTest(unit=unit, inside=reading):
                        coordinator.effective_inside = reading
                        expected = TemperatureConverter.convert(
                            reading, UnitOfTemperature.CELSIUS, unit
                        )
                        serialized = namespace["serialize"](heating)
                        self.assertEqual(serialized["current_temperature"], expected)
                        self.assertIsInstance(serialized["current_temperature"], float)
                        self.assertEqual(
                            heating.state_attributes["current_temperature"], round(expected, 1)
                        )
                        self.assertEqual(heating.target_temperature_step, 1)
                for weighted, top, expected in (
                    (50.26, 51.34, 50.26),
                    (None, 51.34567, 51.34567),
                    (None, None, None),
                ):
                    with self.subTest(unit=unit, weighted=weighted, top=top):
                        coordinator.registers.update(
                            hot_water_weighted_temperature=weighted, hot_water_top_temperature=top
                        )
                        serialized = namespace["serialize"](boiler)
                        converted = (
                            None
                            if expected is None
                            else TemperatureConverter.convert(
                                expected, UnitOfTemperature.CELSIUS, unit
                            )
                        )
                        self.assertEqual(serialized["current_temperature"], converted)
                        self.assertEqual(serialized["hot_water_top_temperature"], top)
                        self.assertEqual(boiler.target_temperature_step, 1)
            coordinator.last_update_success = False
            self.assertNotIn("current_temperature", namespace["serialize"](heating))
            self.assertNotIn("current_temperature", namespace["serialize"](boiler))
        self.assertEqual(coordinator.writes, [])
        self.assertEqual(coordinator.commands, [])

    @unittest.skipUnless(
        os.environ.get("THERMIA_HA_SOURCE"),
        "Set THERMIA_HA_SOURCE to a Core 2026.9.4 homeassistant source folder",
    )
    def test_exact_core_force_update_attribute_invalidates_its_cached_property(self):
        source = Path(os.environ["THERMIA_HA_SOURCE"])
        content = (source / "helpers/entity.py").read_text()
        tree = ast.parse(content)
        metaclasses = ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef)
                and node.name in {"CachedProperties", "ABCCachedProperties"}
            ],
            type_ignores=[],
        )
        namespace = {
            "ABCMeta": ABCMeta,
            "FunctionType": FunctionType,
            "attrgetter": attrgetter,
            "_SENTINEL": object(),
        }
        exec(compile(metaclasses, "core_entity_metaclasses", "exec"), namespace)

        class CachedForceEntity(
            metaclass=namespace["ABCCachedProperties"], cached_properties={"force_update"}
        ):
            @cached_property
            def force_update(self):
                return getattr(self, "_attr_force_update", False)

        entity = CachedForceEntity()
        self.assertFalse(entity.force_update)
        self.assertIn("force_update", entity.__dict__)
        entity._attr_force_update = True
        self.assertNotIn("force_update", entity.__dict__)
        self.assertTrue(entity.force_update)
        entity._attr_force_update = False
        self.assertNotIn("force_update", entity.__dict__)
        self.assertFalse(entity.force_update)
        self.assertIn(
            "self.force_update,", content.split("self.hass.states.async_set_internal(", 1)[1]
        )

    @unittest.skipUnless(
        os.environ.get("THERMIA_HA_SOURCE"),
        "Set THERMIA_HA_SOURCE to a Core 2026.9.4 homeassistant source folder",
    )
    def test_core_rounds_current_and_targets_with_precision_independently_of_edit_step(self):
        source = Path(os.environ["THERMIA_HA_SOURCE"])
        const = (source / "const.py").read_text()
        self.assertIn("PRECISION_TENTHS: Final = 0.1", const)
        content = (source / "components/climate/__init__.py").read_text()
        capability = content.split("def capability_attributes(self)", 1)[1].split(
            "def state_attributes(self)", 1
        )[0]
        state = content.split("def state_attributes(self)", 1)[1].split(
            "def current_temperature(self)", 1
        )[0]
        self.assertIn("precision = self.precision", state)
        self.assertIn("hass, self.current_temperature, temperature_unit, precision", state)
        self.assertIn(
            "self.target_temperature,\n                temperature_unit,\n                precision",
            state,
        )
        self.assertIn("hass, self.target_temperature_high, temperature_unit, precision", state)
        self.assertIn("hass, self.target_temperature_low, temperature_unit, precision", state)
        self.assertIn("target_temperature_step := self.target_temperature_step", capability)
        self.assertIn("ClimateEntityCapabilityAttribute.TARGET_TEMP_STEP", capability)
        self.assertNotIn("ClimateEntityCapabilityAttribute.PRECISION", capability)

    @unittest.skipUnless(
        os.environ.get("THERMIA_HA_SOURCE"),
        "Set THERMIA_HA_SOURCE to a Core 2026.9.4 homeassistant source folder",
    )
    def test_core_temperature_service_requires_both_range_fields_and_rejects_crossed_input(self):
        source = Path(os.environ["THERMIA_HA_SOURCE"])
        content = (source / "components/climate/__init__.py").read_text()
        schema = content.split("SET_TEMPERATURE_SCHEMA =", 1)[1].split("# mypy:", 1)[0]
        self.assertIn('vol.Inclusive(ATTR_TARGET_TEMP_LOW, "temperature")', schema)
        self.assertIn('vol.Inclusive(ATTR_TARGET_TEMP_HIGH, "temperature")', schema)
        service = content.split("async def async_service_temperature_set(", 1)[1]
        crossed_input = service.index("and target_low_temp > target_high_temp")
        entity_call = service.index("await entity.async_set_temperature(**kwargs)")
        self.assertLess(crossed_input, entity_call)

    @unittest.skipUnless(
        os.environ.get("THERMIA_HA_SOURCE"),
        "Set THERMIA_HA_SOURCE to a Core 2026.9.4 homeassistant source folder",
    )
    def test_core_publishes_read_only_humidity_and_reads_dynamic_climate_features(self):
        source = Path(os.environ["THERMIA_HA_SOURCE"])
        content = (source / "components/climate/__init__.py").read_text()
        capability = content.split("def capability_attributes(self)", 1)[1].split(
            "def state_attributes(self)", 1
        )[0]
        state = content.split("def state_attributes(self)", 1)[1].split(
            "def current_temperature(self)", 1
        )[0]
        self.assertIn("supported_features = self.supported_features", capability)
        self.assertIn("supported_features = self.supported_features", state)
        self.assertIn("if ClimateEntityFeature.PRESET_MODE in supported_features:", capability)
        read_only = state.index("if (current_humidity := self.current_humidity) is not None:")
        humidity_target = state.index(
            "if ClimateEntityFeature.TARGET_HUMIDITY in supported_features:"
        )
        self.assertLess(read_only, humidity_target)
        self.assertIn("CURRENT_HUMIDITY] = current_humidity", state[read_only:humidity_target])

    @unittest.skipUnless(
        os.environ.get("THERMIA_HA_SOURCE"),
        "Set THERMIA_HA_SOURCE to a Core 2026.9.4 homeassistant source folder",
    )
    def test_exact_core_public_symbols_and_feature_import_location(self):
        source = Path(os.environ["THERMIA_HA_SOURCE"])
        expected = {
            "const.py": ("UnitOfTime",),
            "components/water_heater/__init__.py": (
                "WaterHeaterEntity",
                "WaterHeaterEntityFeature",
            ),
            "components/climate/__init__.py": ("ClimateEntity",),
            "components/climate/const.py": ("HVACMode", "HVACAction", "ClimateEntityFeature"),
            "components/number/__init__.py": ("NumberEntity",),
            "components/number/const.py": ("NumberDeviceClass", "NumberMode"),
            "components/switch/__init__.py": ("SwitchEntity",),
            "components/button/__init__.py": ("ButtonEntity",),
            "components/binary_sensor/__init__.py": (
                "BinarySensorEntity",
                "BinarySensorDeviceClass",
            ),
            "helpers/update_coordinator.py": ("CoordinatorEntity",),
            "helpers/entity.py": ("Entity",),
        }
        for filename, symbols in expected.items():
            content = (source / filename).read_text()
            for symbol in symbols:
                self.assertRegex(content, rf"(?m)^class {symbol}(?:\W|$)", (filename, symbol))
        water_const = (source / "components/water_heater/const.py").read_text()
        self.assertNotIn("class WaterHeaterEntityFeature", water_const)
        connection = (source / "components/modbus/connection.py").read_text()
        self.assertRegex(connection, r"(?m)^def async_get_unit\(")
        self.assertRegex(connection, r"(?m)^async def async_get_temporary_unit\(")
        entity = (source / "helpers/entity.py").read_text()
        self.assertRegex(entity, r"(?m)^    def async_on_remove\(")
        self.assertRegex(entity, r"(?m)^    def _call_on_remove_callbacks\(")


if __name__ == "__main__":
    unittest.main()
