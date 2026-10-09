"""Validate preference/fallback inputs using actual sensor report age."""

import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "thermia_temperature_test",
    Path(__file__).parents[1] / "custom_components/thermia_genesis_modbus/temperature.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TemperatureTests(unittest.TestCase):
    def test_selected_celsius_source_preserves_all_measured_decimals_through_freshness_pipeline(
        self,
    ):
        now = datetime.now(timezone.utc)
        state = SimpleNamespace(
            state="22.34567",
            attributes={"unit_of_measurement": "°C"},
            last_reported=now - timedelta(seconds=5),
        )
        for inside in (False, True):
            with self.subTest(inside=inside):
                sample = module.temperature_sample(state, now, 900, inside=inside)
                self.assertEqual(sample.value, 22.34567)
                self.assertEqual(sample.reason, "valid")
                self.assertEqual(sample.age_seconds, 5)
                self.assertEqual(module.fresh_temperature(state, now, 900, inside=inside), 22.34567)
                self.assertEqual(module.valid_temperature(22.34567, inside=inside), 22.34567)
                self.assertNotEqual(sample.value, round(sample.value, 3))
        state.last_reported = now - timedelta(seconds=901)
        self.assertIsNone(module.fresh_temperature(state, now, 900, inside=True))
        self.assertEqual(module.temperature_sample(state, now, 900, inside=True).reason, "stale")
        self.assertEqual(module.fresh_temperature(state, now, 0, inside=True), 22.34567)

    def test_selected_fahrenheit_and_kelvin_sources_preserve_full_converted_numeric_precision(self):
        now = datetime.now(timezone.utc)
        for value, unit, expected in (
            ("72.222222", "°F", (72.222222 - 32) * 5 / 9),
            ("295.495678", "K", 295.495678 - 273.15),
        ):
            for inside in (False, True):
                with self.subTest(unit=unit, inside=inside):
                    state = SimpleNamespace(
                        state=value,
                        attributes={"unit_of_measurement": unit},
                        last_reported=now,
                    )
                    sample = module.temperature_sample(state, now, 900, inside=inside)
                    self.assertEqual(sample.value, expected)
                    self.assertEqual(sample.unit, unit)
                    self.assertEqual(sample.reason, "valid")
                    self.assertEqual(
                        module.fresh_temperature(state, now, 900, inside=inside), expected
                    )
                    self.assertEqual(module.valid_temperature(value, unit, inside=inside), expected)
                    self.assertNotEqual(sample.value, round(expected, 3))

    def test_invalid_values_and_units_are_not_temperatures(self):
        for value in ("unknown", "unavailable", "nan", "inf", None, 200, True):
            self.assertIsNone(module.valid_temperature(value))
        self.assertIsNone(module.valid_temperature("20", "kWh"))
        self.assertAlmostEqual(module.valid_temperature("68", "°F", inside=True), 20)
        self.assertAlmostEqual(module.valid_temperature("293.15", "K", inside=True), 20)

    def test_normalized_temperature_units_and_explicit_unitless_input(self):
        for unit in (None, "", "C", " celsius ", " ºc "):
            self.assertEqual(module.valid_temperature("20", unit, inside=True), 20)
        self.assertEqual(module.valid_temperature("68", " °f ", inside=True), 20)
        self.assertEqual(module.valid_temperature("293.15", "kelvin", inside=True), 20)

    def test_constant_but_freshly_reported_sensor_remains_valid(self):
        now = datetime.now(timezone.utc)
        state = SimpleNamespace(
            state="21",
            attributes={"unit_of_measurement": "°C"},
            last_changed=now - timedelta(days=2),
            last_updated=now - timedelta(days=2),
            last_reported=now,
        )
        self.assertEqual(module.fresh_temperature(state, now, 900, inside=True), 21)
        state.last_reported = now - timedelta(minutes=16)
        self.assertIsNone(module.fresh_temperature(state, now, 900, inside=True))

    def test_valid_stable_sensor_is_not_expired_when_timeout_is_disabled(self):
        now = datetime.now(timezone.utc)
        state = SimpleNamespace(state="21", attributes={}, last_reported=now - timedelta(days=2))
        for timeout in (None, 0):
            sample = module.temperature_sample(state, now, timeout, inside=True)
            self.assertEqual(sample.value, 21)
            self.assertEqual(sample.reason, "valid")
            self.assertEqual(sample.unit, "°C")
            self.assertEqual(sample.age_seconds, 172800)
        self.assertEqual(module.temperature_sample(state, now, 900, inside=True).reason, "stale")

    def test_invalid_selected_states_have_specific_reasons(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(module.temperature_sample(None, now).reason, "not_found")
        for value, unit, reason in (
            ("unknown", "°C", "unknown"),
            ("unavailable", "°C", "unavailable"),
            ("nan", "°C", "invalid_value"),
            ("22", "kWh", "unsupported_unit"),
            ("200", "°C", "out_of_range"),
        ):
            state = SimpleNamespace(
                state=value, attributes={"unit_of_measurement": unit}, last_reported=now
            )
            self.assertEqual(module.temperature_sample(state, now).reason, reason)

    def test_report_timestamp_is_only_required_for_explicit_freshness_check(self):
        now = datetime.now(timezone.utc)
        state = SimpleNamespace(state="21", attributes={"unit_of_measurement": "°C"})
        self.assertEqual(module.temperature_sample(state, now, 0).value, 21)
        self.assertEqual(module.temperature_sample(state, now, 900).reason, "invalid_report_time")

    def test_source_status_explains_missing_external_and_native_room_sensor(self):
        self.assertEqual(
            module.temperature_source_status("not_selected", "unavailable", inside=True),
            "No inside sensor selected; Thermia unavailable",
        )
        self.assertEqual(
            module.temperature_source_status("not_found", "unavailable", inside=True),
            "Selected sensor not found",
        )
        self.assertEqual(
            module.temperature_source_status("unavailable", "room_sensor_alarm", inside=True),
            "Selected sensor unavailable",
        )

    def test_source_status_reports_native_fallback_and_external_rejection(self):
        self.assertEqual(
            module.temperature_source_status("stale", "valid", inside=True),
            "Thermia room sensor in use",
        )
        self.assertEqual(
            module.temperature_source_status("valid", "unavailable", inside=True),
            "External sensor in use",
        )
        self.assertEqual(
            module.temperature_source_status("stale", "unavailable", inside=True),
            "Selected sensor reading is stale",
        )
        self.assertEqual(
            module.temperature_source_status("unsupported_unit", "unavailable", inside=True),
            "Selected sensor unit is unsupported",
        )


if __name__ == "__main__":
    unittest.main()
