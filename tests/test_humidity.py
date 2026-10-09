"""Humidity validation and independent dew-point protection calculations."""

import importlib.util
import math
import sys
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

SOURCE = Path(__file__).parents[1] / "custom_components/thermia_genesis_modbus/humidity.py"
PACKAGE_NAME = "thermia_humidity_test_package"
PACKAGE = ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(SOURCE.parent)]
sys.modules[PACKAGE_NAME] = PACKAGE
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.humidity", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class HumidityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 12, tzinfo=UTC)

    def state(self, value="60", unit="%", **timestamps):
        return SimpleNamespace(
            state=value, attributes={"unit_of_measurement": unit}, **timestamps
        )

    def test_percentages_are_finite_above_zero_and_at_most_one_hundred(self):
        for value in (None, True, False, "unknown", "unavailable", "nan", "inf", -1, 0,
                      100.1, 10**1000):
            with self.subTest(value=str(value)[:30]):
                self.assertIsNone(MODULE.valid_humidity(value))
        self.assertEqual(MODULE.valid_humidity(" 60.25 "), 60.25)
        self.assertEqual(MODULE.valid_humidity(100), 100)
        self.assertEqual(MODULE.valid_humidity(0.1), 0.1)

    def test_ha_humidity_requires_percentage_unit_without_guessing_a_fraction(self):
        for unit in (None, "", "°C", "g/m³", "fraction", "percent"):
            with self.subTest(unit=unit):
                sample = MODULE.humidity_sample(self.state("0.6", unit), self.now)
                self.assertIsNone(sample.value)
                self.assertEqual(sample.reason, "unsupported_unit")
        self.assertEqual(MODULE.valid_humidity("60", " % "), 60)

    def test_selected_sensor_outages_have_specific_reasons_and_no_fallback(self):
        self.assertEqual(MODULE.humidity_sample(None, self.now).reason, "not_found")
        for value, reason in (
            (" unknown ", "unknown"),
            ("UNAVAILABLE", "unavailable"),
            ("bad", "invalid_value"),
            ("nan", "invalid_value"),
            (True, "invalid_value"),
            ("0", "out_of_range"),
            ("101", "out_of_range"),
        ):
            with self.subTest(value=value):
                sample = MODULE.humidity_sample(self.state(value), self.now)
                self.assertIsNone(sample.value)
                self.assertEqual(sample.reason, reason)

    def test_stable_state_remains_valid_when_report_timeout_is_disabled(self):
        state = self.state(last_reported=self.now - timedelta(days=2))
        original = deepcopy(state.__dict__)
        for timeout in (None, 0):
            sample = MODULE.humidity_sample(state, self.now, timeout)
            self.assertEqual(sample.value, 60)
            self.assertEqual(sample.reason, "valid")
            self.assertEqual(sample.unit, "%")
            self.assertEqual(sample.age_seconds, 172800)
        self.assertEqual(state.__dict__, original)

    def test_fresh_last_reported_keeps_unchanged_value_usable_with_timeout(self):
        state = self.state(
            last_changed=self.now - timedelta(days=2),
            last_updated=self.now - timedelta(days=2),
            last_reported=self.now,
        )
        self.assertEqual(MODULE.humidity_sample(state, self.now, 900).value, 60)
        state.last_reported = self.now - timedelta(seconds=900)
        self.assertEqual(MODULE.humidity_sample(state, self.now, 900).value, 60)
        state.last_reported -= timedelta(milliseconds=1)
        self.assertEqual(MODULE.humidity_sample(state, self.now, 900).reason, "stale")

    def test_last_updated_is_used_when_last_reported_is_absent(self):
        state = self.state(last_updated=self.now - timedelta(seconds=30))
        sample = MODULE.humidity_sample(state, self.now, 900)
        self.assertEqual(sample.value, 60)
        self.assertEqual(sample.age_seconds, 30)

    def test_bad_timestamps_are_only_rejected_when_timeout_is_enabled(self):
        for timestamps in ({}, {"last_reported": "bad"},
                           {"last_reported": self.now.replace(tzinfo=None)},
                           {"last_reported": self.now + timedelta(seconds=61)}):
            with self.subTest(timestamps=timestamps):
                state = self.state(**timestamps)
                self.assertEqual(MODULE.humidity_sample(state, self.now, 0).value, 60)
                self.assertEqual(
                    MODULE.humidity_sample(state, self.now, 900).reason,
                    "invalid_report_time",
                )

    def test_known_room_conditions_match_magnus_reference_values(self):
        for temperature, humidity, expected in ((25, 60, 16.6931), (20, 50, 9.2552),
                                                (30, 70, 23.9272)):
            with self.subTest(temperature=temperature, humidity=humidity):
                self.assertAlmostEqual(MODULE.dew_point(temperature, humidity), expected, places=3)

    def test_saturation_equals_air_temperature_without_upward_rounding_artifact(self):
        for temperature in (-20, 0, 20, 25, 60):
            with self.subTest(temperature=temperature):
                self.assertEqual(MODULE.dew_point(temperature, 100), temperature)
                info = MODULE.cooling_guard_info(temperature, 100)
                self.assertEqual(info["min_cooling_supply"], temperature + 2)

    def test_dew_point_rejects_invalid_or_implausible_inside_temperature(self):
        for temperature in (None, True, "nan", "inf", -20.1, 60.1, 10**1000):
            with self.subTest(temperature=str(temperature)[:30]):
                self.assertIsNone(MODULE.dew_point(temperature, 60))
        for humidity in (None, True, 0, 101):
            self.assertIsNone(MODULE.dew_point(25, humidity))

    def test_higher_humidity_raises_dew_point_and_safe_supply_minimum(self):
        previous = -math.inf
        for humidity in (20, 40, 60, 80, 100):
            point = MODULE.dew_point(25, humidity)
            self.assertGreater(point, previous)
            self.assertLessEqual(point, 25)
            info = MODULE.cooling_guard_info(25, humidity)
            self.assertGreaterEqual(info["min_cooling_supply"], point + 2)
            self.assertLess(info["min_cooling_supply"] - (point + 2), 1)
            previous = point

    def test_very_small_positive_humidity_still_has_a_finite_dew_point(self):
        point = MODULE.dew_point(25, 5e-324)
        self.assertTrue(math.isfinite(point))
        self.assertLess(point, 25)

    def test_ready_guard_rounds_supply_up_and_exposes_inputs(self):
        info = MODULE.cooling_guard_info(25, 60, 65, 2)
        self.assertTrue(info["configured"])
        self.assertTrue(info["allowed"])
        self.assertEqual(info["status"], "ready")
        self.assertAlmostEqual(info["dew_point"], 16.6931, places=3)
        self.assertEqual(info["min_cooling_supply"], 19)
        self.assertEqual(info["inside_temperature"], 25)
        self.assertEqual(info["relative_humidity"], 60)
        self.assertEqual(info["humidity_limit"], 65)
        self.assertEqual(info["dew_point_margin"], 2)

    def test_humidity_limit_pauses_at_equality_but_helper_is_stateless(self):
        for humidity in (65, 65.1, 100):
            info = MODULE.cooling_guard_info(25, humidity)
            self.assertFalse(info["allowed"])
            self.assertEqual(info["status"], "high_humidity")
        self.assertTrue(MODULE.cooling_guard_info(25, 64.9)["allowed"])
        self.assertTrue(MODULE.cooling_guard_info(25, 60)["allowed"])

    def test_configured_guard_pauses_on_missing_humidity_or_temperature(self):
        for temperature, humidity, status in (
            (25, None, "missing_humidity"),
            (25, 0, "missing_humidity"),
            (None, 60, "missing_inside_temperature"),
            (200, 60, "missing_inside_temperature"),
        ):
            with self.subTest(status=status, temperature=temperature, humidity=humidity):
                info = MODULE.cooling_guard_info(temperature, humidity)
                self.assertFalse(info["allowed"])
                self.assertEqual(info["status"], status)
                self.assertIsNone(info["dew_point"])
                self.assertIsNone(info["min_cooling_supply"])

    def test_no_selected_sensor_leaves_humidity_guard_inactive(self):
        info = MODULE.cooling_guard_info(None, None, configured=False)
        self.assertFalse(info["configured"])
        self.assertTrue(info["allowed"])
        self.assertEqual(info["status"], "no_sensor_selected")
        self.assertIsNone(info["dew_point"])

    def test_invalid_safety_settings_do_not_allow_configured_cooling(self):
        for limit, margin in ((29, 2), (91, 2), (65.5, 2), (True, 2), (65, 0),
                              (65, 6), (65, 1.5), (65, "nan"), (None, 2)):
            with self.subTest(limit=limit, margin=margin):
                info = MODULE.cooling_guard_info(25, 60, limit, margin)
                self.assertFalse(info["allowed"])
                self.assertEqual(info["status"], "invalid_settings")
                self.assertIsNone(info["min_cooling_supply"])


if __name__ == "__main__":
    unittest.main()
