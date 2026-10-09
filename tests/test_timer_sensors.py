"""Countdown sensors using the shared narrow HA platform API stand-ins.

These exercise registration, offline local callbacks and removal; they are not
a full Home Assistant runtime or hardware test.
"""

import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_platforms import MODULES, Coordinator, SensorDeviceClass, UnitOfTime

HUMIDITY = import_module(f"{MODULES['sensor'].__package__}.humidity")


class TimerSensorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.coordinator = Coordinator()
        self.coordinator.engine.now = Mock(return_value=1000.0)
        self.coordinator.engine.settings["hot_water_low_hours"] = 24
        self.coordinator.engine.state.update(
            heating_preset="pv_charge", heating_excess_started_at=1000,
            heating_excess_deadline=44200, hot_water_mode="evening",
            hot_water_low_started_at=1000, hot_water_low_deadline=87400,
        )

    async def test_registration_adds_low_timer_without_changing_excess_sensor_identities(self):
        entities = []
        await MODULES["sensor"].async_setup_entry(
            None, SimpleNamespace(runtime_data=self.coordinator), entities.extend
        )
        timers = {
            entity._attr_unique_id: entity
            for entity in entities
            if isinstance(entity, (
                MODULES["sensor"].ThermiaExcessTimeRemaining,
                MODULES["sensor"].ThermiaLowTimeRemaining,
            ))
        }
        self.assertEqual(set(timers), {
            "pump-id_heating_excess_time_remaining",
            "pump-id_hot_water_excess_time_remaining",
            "pump-id_hot_water_low_time_remaining",
        })
        low = timers["pump-id_hot_water_low_time_remaining"]
        self.assertEqual(low._attr_name, "C3.05 Hot Water — Low Mode Time Remaining")
        self.assertEqual(low.native_value, 24)
        self.assertEqual(low._attr_device_class, SensorDeviceClass.DURATION)
        self.assertEqual(low._attr_native_unit_of_measurement, UnitOfTime.HOURS)
        self.assertTrue(low._attr_entity_registry_enabled_default)
        self.assertTrue(low._attr_entity_registry_visible_default)
        for key in ("heating", "hot_water"):
            entity = timers[f"pump-id_{key}_excess_time_remaining"]
            self.assertIn("Excess Energy", entity._attr_name)
            self.assertNotIn("group", vars(entity))
            self.assertEqual(entity._timer_group, key)
            self.assertIsInstance(entity.native_value, (int, float))

    async def test_low_timer_updates_offline_and_unregisters_without_control_writes(self):
        entity = MODULES["sensor"].ThermiaLowTimeRemaining(self.coordinator)
        entity.hass = SimpleNamespace()
        cancel = Mock()
        with patch.object(MODULES["sensor"], "async_track_time_interval", return_value=cancel) as track:
            await entity.async_added_to_hass()
        track.assert_called_once_with(entity.hass, entity._async_update_countdown, timedelta(seconds=30))
        scheduled_callback = track.call_args.args[1]
        self.assertTrue(scheduled_callback._hass_callback)
        self.coordinator.last_update_success = False
        original_state = deepcopy(self.coordinator.engine.state)
        original_settings = deepcopy(self.coordinator.engine.settings)
        for now in (4600.0, 87400.0, 87430.0):
            self.coordinator.engine.now.return_value = now
            scheduled_callback(datetime.fromtimestamp(now, UTC))
            self.assertTrue(entity.available)
            self.assertFalse(entity.extra_state_attributes["controller_available"])
        self.assertEqual(entity.state_updates, [23, 0, 0])
        self.assertTrue(entity.extra_state_attributes["reset_pending"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.engine.settings, original_settings)
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])
        entity._call_on_remove_callbacks()
        cancel.assert_called_once_with()


class HumiditySensorTests(unittest.IsolatedAsyncioTestCase):
    """Exercise read-only humidity entities with the verified pure calculation."""

    def setUp(self):
        self.coordinator = Coordinator()
        self.coordinator.effective_inside = 25
        self.coordinator.effective_inside_humidity = 60
        self.coordinator.engine.settings["inside_humidity_sensor"] = "sensor.indoor_humidity"
        self.actual_engine_info = self.coordinator.engine.cooling_humidity_info
        self.source_status = "valid"
        self.source_value = "60"
        self.coordinator.humidity_source_details = self.source_details
        # The engine's durable controller is tested separately. This boundary
        # supplies its read-only guard calculation, without replacing the math.
        self.coordinator.engine.cooling_humidity_info = self.guard_info

    def source_details(self):
        return {
            "selected_sensor": self.coordinator.engine.settings["inside_humidity_sensor"] or None,
            "selected_sensor_state": self.source_value,
            "selected_sensor_unit": "%",
            "external_status": self.source_status,
            "freshness_limit_seconds": 0,
        }

    def guard_info(self, *, inside=..., humidity=...):
        return HUMIDITY.cooling_guard_info(
            self.coordinator.effective_inside if inside is ... else inside,
            self.coordinator.effective_inside_humidity if humidity is ... else humidity,
            configured=bool(self.coordinator.engine.settings["inside_humidity_sensor"]),
        ) | {"paused": False, "native_supply_target": 19, "restore_pending": False}

    async def test_registration_adds_three_visible_read_only_humidity_entities(self):
        entities = []
        await MODULES["sensor"].async_setup_entry(
            None, SimpleNamespace(runtime_data=self.coordinator), entities.extend
        )
        humidity = {
            entity._attr_unique_id: entity for entity in entities
            if isinstance(entity, MODULES["sensor"]._ThermiaHumidityDiagnostic)
        }
        self.assertEqual(set(humidity), {
            "pump-id_inside_relative_humidity", "pump-id_inside_dew_point",
            "pump-id_cooling_humidity_protection",
        })
        relative = humidity["pump-id_inside_relative_humidity"]
        self.assertEqual(relative._attr_device_class, SensorDeviceClass.HUMIDITY)
        self.assertEqual(relative._attr_native_unit_of_measurement, "%")
        dew_point = humidity["pump-id_inside_dew_point"]
        self.assertEqual(dew_point._attr_device_class, SensorDeviceClass.TEMPERATURE)
        self.assertEqual(dew_point._attr_native_unit_of_measurement, "°C")
        for entity in humidity.values():
            self.assertTrue(entity._attr_entity_registry_enabled_default)
            self.assertTrue(entity._attr_entity_registry_visible_default)
        self.assertEqual(self.coordinator.writes, [])
        self.assertEqual(self.coordinator.commands, [])

    def test_local_readings_remain_visible_offline_and_calculation_does_not_mutate_policy(self):
        self.coordinator.last_update_success = False
        original_state = deepcopy(self.coordinator.engine.state)
        original_settings = deepcopy(self.coordinator.engine.settings)
        relative = MODULES["sensor"].ThermiaInsideHumidity(self.coordinator)
        dew_point = MODULES["sensor"].ThermiaInsideDewPoint(self.coordinator)
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        self.assertTrue(relative.available)
        self.assertEqual(relative.native_value, 60)
        self.assertTrue(dew_point.available)
        self.assertAlmostEqual(dew_point.native_value, 16.6931, places=3)
        self.assertTrue(protection.available)
        self.assertEqual(protection.native_value, "Ready")
        for entity in (relative, dew_point, protection):
            attrs = entity.extra_state_attributes
            self.assertEqual(attrs["selected_sensor"], "sensor.indoor_humidity")
            self.assertEqual(attrs["selected_sensor_state"], "60")
            self.assertEqual(attrs["min_cooling_supply"], 19)
            self.assertFalse(attrs["controller_available"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.engine.settings, original_settings)
        self.assertEqual(self.coordinator.writes, [])

    def test_sensor_outage_has_no_native_humidity_fallback_and_keeps_status_available(self):
        self.coordinator.effective_inside_humidity = None
        self.source_status = "unavailable"
        self.source_value = "unavailable"
        relative = MODULES["sensor"].ThermiaInsideHumidity(self.coordinator)
        dew_point = MODULES["sensor"].ThermiaInsideDewPoint(self.coordinator)
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        self.assertFalse(relative.available)
        self.assertIsNone(relative.native_value)
        self.assertFalse(dew_point.available)
        self.assertIsNone(dew_point.native_value)
        self.assertTrue(protection.available)
        self.assertEqual(protection.native_value, "Paused: humidity unavailable")
        self.assertEqual(protection.extra_state_attributes["external_status"], "unavailable")
        self.assertFalse(protection.extra_state_attributes["allowed"])

    def test_no_selected_humidity_and_missing_temperature_have_distinct_status(self):
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        self.coordinator.engine.settings["inside_humidity_sensor"] = ""
        self.coordinator.effective_inside_humidity = None
        self.assertEqual(protection.native_value, "No humidity sensor selected")
        self.assertTrue(protection.extra_state_attributes["allowed"])
        self.coordinator.engine.settings["inside_humidity_sensor"] = "sensor.indoor_humidity"
        self.coordinator.effective_inside_humidity = 60
        self.coordinator.effective_inside = None
        self.assertEqual(protection.native_value, "Paused: inside temperature unavailable")
        self.assertFalse(protection.extra_state_attributes["allowed"])
        self.assertIsNone(MODULES["sensor"].ThermiaInsideDewPoint(self.coordinator).native_value)
        self.assertEqual(MODULES["sensor"].ThermiaInsideHumidity(self.coordinator).native_value, 60)

    def test_high_humidity_keeps_dew_point_visible_and_explains_pause(self):
        self.coordinator.effective_inside_humidity = 65
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        self.assertEqual(protection.native_value, "Paused: humidity limit reached")
        self.assertFalse(protection.extra_state_attributes["allowed"])
        self.assertIsNotNone(MODULES["sensor"].ThermiaInsideDewPoint(self.coordinator).native_value)
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])

    def test_disabled_normal_guard_still_displays_monitoring_readings(self):
        info = self.guard_info() | {
            "status": "disabled_in_normal", "allowed": True,
            "protection_enabled": False, "enabled_in_normal": False,
            "vacation_guard": False,
        }
        self.coordinator.engine.cooling_humidity_info = Mock(return_value=info)
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        self.assertEqual(protection.native_value, "Disabled in Normal")
        self.assertFalse(protection.extra_state_attributes["protection_enabled"])
        self.assertFalse(protection.extra_state_attributes["enabled_in_normal"])
        self.assertFalse(protection.extra_state_attributes["vacation_guard"])
        self.assertEqual(MODULES["sensor"].ThermiaInsideHumidity(self.coordinator).native_value, 60)
        self.assertAlmostEqual(
            MODULES["sensor"].ThermiaInsideDewPoint(self.coordinator).native_value,
            16.6931, places=3,
        )
        self.assertEqual(self.coordinator.writes, [])

    def test_hysteresis_pause_explains_below_limit_reading_and_resume_threshold(self):
        info = self.guard_info() | {
            "status": "high_humidity", "allowed": False, "paused": True,
            "relative_humidity": 64, "resume_humidity_limit": 63,
            "protection_enabled": True, "enabled_in_normal": False,
            "vacation_guard": True,
        }
        self.coordinator.engine.cooling_humidity_info = Mock(return_value=info)
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        self.assertEqual(protection.native_value, "Paused: waiting for humidity to fall")
        self.assertEqual(protection.extra_state_attributes["resume_humidity_limit"], 63)
        self.assertTrue(protection.extra_state_attributes["vacation_guard"])
        self.assertEqual(self.coordinator.commands, [])

    def test_real_engine_pending_permission_resume_remains_visible_without_register_journal(self):
        self.coordinator.engine.cooling_humidity_info = self.actual_engine_info
        self.coordinator.engine.state["cooling_humidity_resume_pending"] = True
        self.assertEqual(self.coordinator.engine.state["pending_restore"], [])
        self.assertEqual(self.coordinator.engine.state["overrides"], {})
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        original_state = deepcopy(self.coordinator.engine.state)
        self.assertEqual(protection.native_value, "Restoration pending")
        self.assertTrue(protection.extra_state_attributes["restore_pending"])
        self.assertFalse(protection.extra_state_attributes["protection_enabled"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.writes, [])
        self.coordinator.engine.state["cooling_humidity_resume_pending"] = False
        self.assertEqual(protection.native_value, "Disabled in Normal")

    async def test_local_diagnostic_refresh_displays_outage_offline_and_cleans_up(self):
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        protection.hass = SimpleNamespace()
        cancel = Mock()
        with patch.object(MODULES["sensor"], "async_track_time_interval", return_value=cancel) as track:
            await protection.async_added_to_hass()
        track.assert_called_once_with(
            protection.hass, protection._async_update_humidity, timedelta(seconds=30)
        )
        scheduled_callback = track.call_args.args[1]
        self.assertTrue(scheduled_callback._hass_callback)
        self.coordinator.last_update_success = False
        self.coordinator.effective_inside_humidity = None
        self.source_status = "stale"
        original_state = deepcopy(self.coordinator.engine.state)
        original_settings = deepcopy(self.coordinator.engine.settings)
        scheduled_callback(datetime(2026, 10, 5, tzinfo=UTC))
        self.assertEqual(protection.state_updates, ["Paused: humidity unavailable"])
        self.assertEqual(protection.attribute_updates[-1]["external_status"], "stale")
        self.assertFalse(protection.attribute_updates[-1]["controller_available"])
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.engine.settings, original_settings)
        self.assertEqual(self.coordinator.writes, [])
        protection._call_on_remove_callbacks()
        cancel.assert_called_once_with()

    async def test_real_engine_diagnostics_use_fresh_local_values_during_offline_poll_backoff(self):
        self.coordinator.engine.cooling_humidity_info = self.actual_engine_info
        self.coordinator.engine.settings["cooling_humidity_enabled"] = True
        self.coordinator.engine.update_measurements(25, 12, 60)
        self.assertEqual(self.actual_engine_info()["status"], "ready")
        protection = MODULES["sensor"].ThermiaCoolingHumidityProtection(self.coordinator)
        protection.hass = SimpleNamespace()
        cancel = Mock()
        with patch.object(MODULES["sensor"], "async_track_time_interval", return_value=cancel) as track:
            await protection.async_added_to_hass()
        scheduled_callback = track.call_args.args[1]
        self.coordinator.last_update_success = False
        original_state = deepcopy(self.coordinator.engine.state)
        original_settings = deepcopy(self.coordinator.engine.settings)
        for inside, humidity, expected in (
            (25, None, "Paused: humidity unavailable"),
            (None, 60, "Paused: inside temperature unavailable"),
        ):
            with self.subTest(inside=inside, humidity=humidity):
                self.coordinator.effective_inside = inside
                self.coordinator.effective_inside_humidity = humidity
                self.source_status = "stale" if humidity is None else "valid"
                scheduled_callback(datetime(2026, 10, 5, tzinfo=UTC))
                self.assertEqual(protection.state_updates[-1], expected)
                self.assertFalse(protection.extra_state_attributes["allowed"])
                self.assertIsNone(MODULES["sensor"].ThermiaInsideDewPoint(self.coordinator).native_value)
                # Poll-time cached readings retain their old values. Rendering
                # uses fresh measurements without mutating that policy cache.
                self.assertEqual(self.coordinator.engine._inside, 25)
                self.assertEqual(self.coordinator.engine._humidity, 60)
                self.assertEqual(self.actual_engine_info()["status"], "ready")
        self.assertEqual(self.coordinator.engine.state, original_state)
        self.assertEqual(self.coordinator.engine.settings, original_settings)
        self.assertEqual(self.coordinator.commands, [])
        self.assertEqual(self.coordinator.writes, [])
        protection._call_on_remove_callbacks()
        cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
