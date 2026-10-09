"""Selection-history entities exercised with the real control policy."""

import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_platforms import MODULES, Coordinator


class ModeHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = Coordinator()
        self.coordinator.engine.now = Mock(return_value=1000)
        self.heating = MODULES["sensor"].ThermiaModePresetSensor(self.coordinator, "heating")
        self.water = MODULES["sensor"].ThermiaModePresetSensor(self.coordinator, "hot_water")

    async def test_registration_adds_two_visible_text_sensors_on_the_existing_device(self):
        entities = []
        await MODULES["sensor"].async_setup_entry(
            None, SimpleNamespace(runtime_data=self.coordinator), entities.extend
        )
        history = {
            entity._attr_unique_id: entity
            for entity in entities
            if isinstance(entity, MODULES["sensor"].ThermiaModePresetSensor)
        }
        self.assertEqual(
            {key: entity._attr_name for key, entity in history.items()},
            {
                "pump-id_heating_mode_and_preset": "C2.06 Heating — Mode And Preset",
                "pump-id_hot_water_mode_and_preset": "C3.08 Hot Water — Mode And Preset",
            },
        )
        self.assertEqual(len({entity._attr_unique_id for entity in entities}), len(entities))
        with patch.object(MODULES["sensor"], "async_track_time_interval") as interval:
            for entity in history.values():
                self.assertTrue(entity._attr_entity_registry_enabled_default)
                self.assertTrue(entity._attr_entity_registry_visible_default)
                self.assertIsNone(entity._attr_entity_category)
                self.assertIsNone(entity._attr_native_unit_of_measurement)
                self.assertIsNone(entity._attr_state_class)
                self.assertIsNone(entity._attr_device_class)
                self.assertEqual(
                    entity._attr_device_info["identifiers"],
                    {(MODULES["const"].DOMAIN, self.coordinator.entry.unique_id)},
                )
                await entity.async_added_to_hass()
            interval.assert_not_called()
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])

    async def test_heating_state_tracks_all_displayed_modes_and_presets(self):
        for mode, preset, expected in (
            ("heat", "normal", "Heat · Normal"),
            ("heat", "pv_charge", "Heat · Excess Energy"),
            ("heat", "low", "Heat · Low Mode"),
            ("heat", "vacation", "Heat · Vacation"),
            ("cool", "normal", "Cool · Normal"),
            ("cool", "vacation", "Cool · Vacation"),
            ("heat_cool", "normal", "Heat/Cool · Normal"),
            ("heat_cool", "pv_charge", "Heat/Cool · Excess Energy (Heating Only)"),
            ("heat_cool", "low", "Heat/Cool · Low Mode (Heating Only)"),
            ("heat_cool", "vacation", "Heat/Cool · Vacation"),
            ("off", None, "Off"),
        ):
            with self.subTest(mode=mode, preset=preset):
                coordinator = Coordinator()
                sensor = MODULES["sensor"].ThermiaModePresetSensor(coordinator, "heating")
                climate = MODULES["climate"].ThermiaClimate(coordinator)
                await coordinator.async_command("heating_mode", mode)
                if preset is not None:
                    await coordinator.async_command("heating_preset", preset)
                self.assertEqual(sensor.native_value, expected)
                attrs = sensor.extra_state_attributes
                self.assertEqual(attrs["hvac_mode"], climate.hvac_mode.value)
                self.assertEqual(attrs["preset"], climate.preset_mode)
                self.assertEqual(attrs["mode"], expected.split(" · ")[0])
                self.assertTrue(attrs["controller_available"])
                if mode == "off":
                    self.assertIsNone(attrs["preset"])

    async def test_water_state_tracks_normal_excess_low_and_off_without_claiming_activity(self):
        for mode, expected, preset in (
            ("auto", "Auto · Normal", "Normal"),
            ("energy_excess", "Auto · Excess Energy", "Excess Energy"),
            ("evening", "Auto · Low Mode", "Low Mode"),
            # A recoverable legacy mode follows the same public climate display.
            ("manual_on", "Auto · Normal", "Normal"),
            ("off", "Off", None),
        ):
            with self.subTest(mode=mode):
                coordinator = Coordinator()
                sensor = MODULES["sensor"].ThermiaModePresetSensor(coordinator, "hot_water")
                climate = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
                await coordinator.async_command("hot_water_mode", mode)
                for status, demands in (("Idle", []), ("Hot water", ["hot_water"])):
                    coordinator.status = status
                    coordinator.active_demands = demands
                    self.assertEqual(sensor.native_value, expected)
                attrs = sensor.extra_state_attributes
                self.assertEqual(attrs["preset"], preset)
                self.assertEqual(attrs["preset"], climate.preset_mode)
                self.assertEqual(attrs["hvac_mode"], climate.hvac_mode.value)

    async def test_temperature_edit_and_heating_expiry_return_history_to_normal(self):
        climate = MODULES["climate"].ThermiaClimate(self.coordinator)
        await self.coordinator.async_command("heating_mode", "heat_cool")
        await climate.async_set_preset_mode("Excess Energy (Heating Only)")
        self.assertEqual(self.heating.native_value, "Heat/Cool · Excess Energy (Heating Only)")
        await climate.async_set_temperature(temperature=21)
        self.assertEqual(self.heating.native_value, "Heat/Cool · Normal")
        self.assertIsNone(self.coordinator.engine.state["heating_excess_deadline"])
        await climate.async_set_preset_mode("Excess Energy (Heating Only)")
        deadline = self.coordinator.engine.state["heating_excess_deadline"]
        self.coordinator.engine.now.return_value = deadline
        await self.coordinator.engine.tick(22, 12)
        self.assertEqual(self.heating.native_value, "Heat/Cool · Normal")
        self.assertEqual(self.heating.extra_state_attributes["preset"], climate.preset_mode)

    async def test_temperature_edit_and_both_water_timer_expiries_return_history_to_normal(self):
        climate = MODULES["climate"].ThermiaHotWaterClimate(self.coordinator)
        await climate.async_set_preset_mode("Excess Energy")
        self.assertEqual(self.water.native_value, "Auto · Excess Energy")
        await climate.async_set_temperature(target_temp_low=46, target_temp_high=60)
        self.assertEqual(self.water.native_value, "Auto · Normal")
        self.assertIsNone(self.coordinator.engine.state["hot_water_excess_deadline"])
        for preset, timer in (
            ("Excess Energy", "hot_water_excess_deadline"),
            ("Low Mode", "hot_water_low_deadline"),
        ):
            await climate.async_set_preset_mode(preset)
            self.assertEqual(self.water.native_value, f"Auto · {preset}")
            self.coordinator.engine.now.return_value = self.coordinator.engine.state[timer]
            await self.coordinator.engine.tick(22, 12)
            self.assertEqual(self.water.native_value, "Auto · Normal")
            self.assertIsNone(self.coordinator.engine.state[timer])

    async def test_offline_reads_keep_durable_selections_without_writes_or_timer_updates(self):
        await self.coordinator.async_command("heating_mode", "heat_cool")
        await self.coordinator.async_command("heating_preset", "low")
        await self.coordinator.async_command("hot_water_mode", "evening")
        self.coordinator.last_update_success = False
        state = deepcopy(self.coordinator.engine.state)
        settings = deepcopy(self.coordinator.engine.settings)
        registers = deepcopy(self.coordinator.registers)
        writes = list(self.coordinator.writes)
        commands = list(self.coordinator.commands)
        self.coordinator.engine.now.reset_mock()
        for _ in range(3):
            self.assertTrue(self.heating.available)
            self.assertTrue(self.water.available)
            self.assertEqual(self.heating.native_value, "Heat/Cool · Low Mode (Heating Only)")
            self.assertEqual(self.water.native_value, "Auto · Low Mode")
            self.assertFalse(self.heating.extra_state_attributes["controller_available"])
            self.assertFalse(self.water.extra_state_attributes["controller_available"])
        self.coordinator.engine.now.assert_not_called()
        self.assertEqual(self.coordinator.engine.state, state)
        self.assertEqual(self.coordinator.engine.settings, settings)
        self.assertEqual(self.coordinator.registers, registers)
        self.assertEqual(self.coordinator.writes, writes)
        self.assertEqual(self.coordinator.commands, commands)

    def test_invalid_durable_selections_are_unknown_instead_of_normal(self):
        for mode, preset in (
            (None, "normal"),
            ("bad", "normal"),
            ([], "normal"),
            ("heat", None),
            ("heat", "bad"),
            ("heat", []),
            ("cool", "pv_charge"),
        ):
            with self.subTest(mode=mode, preset=preset):
                self.coordinator.engine.state.update(heating_mode=mode, heating_preset=preset)
                self.assertTrue(self.heating.available)
                self.assertIsNone(self.heating.native_value)
                self.assertIsNone(self.heating.extra_state_attributes["mode"])
                self.assertIsNone(self.heating.extra_state_attributes["preset"])
        for mode in (None, "bad", []):
            with self.subTest(mode=mode):
                self.coordinator.engine.state["hot_water_mode"] = mode
                self.assertTrue(self.water.available)
                self.assertIsNone(self.water.native_value)
                self.assertIsNone(self.water.extra_state_attributes["hvac_mode"])
                self.assertIsNone(self.water.extra_state_attributes["preset"])
        self.coordinator.engine.state.update(heating_mode="off", hot_water_mode="off")
        for entity in (self.heating, self.water):
            self.assertEqual(entity.native_value, "Off")
            self.assertIsNone(entity.extra_state_attributes["preset"])
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])
