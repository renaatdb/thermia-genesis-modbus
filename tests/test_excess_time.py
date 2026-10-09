"""Countdown diagnostics use the policy clock and never mutate saved state."""

import importlib.util
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SOURCE = (
    Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus" / "excess_time.py"
)
SPEC = importlib.util.spec_from_file_location("thermia_excess_time_test", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ExcessTimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = SimpleNamespace(
            now=Mock(return_value=1000.0),
            settings={"heating_excess_hours": 12, "hot_water_excess_hours": 6},
            state={
                "heating_preset": "pv_charge",
                "hot_water_mode": "energy_excess",
                "heating_excess_started_at": 1000.0,
                "heating_excess_deadline": 44200.0,
                "hot_water_excess_started_at": 1000.0,
                "hot_water_excess_deadline": 22600.0,
                "pending_restore": [],
                "charging_cancelled": [],
            },
        )
        self.coordinator = SimpleNamespace(engine=self.engine, last_update_success=True)

    def test_remaining_time_uses_injected_clock_and_safe_utc_timestamps(self):
        self.engine.now.return_value = 4600.0
        info = MODULE.excess_time_info(self.coordinator, "heating")
        self.assertEqual(info["remaining_hours"], 11)
        self.assertEqual(info["started_at"], "1970-01-01T00:16:40+00:00")
        self.assertEqual(info["deadline"], "1970-01-01T12:16:40+00:00")
        self.assertTrue(info["active"])
        self.assertFalse(info["reset_pending"])
        self.assertEqual(info["configured_duration_hours"], 12)

    def test_elapsed_active_run_clamps_to_zero_and_exposes_pending_reset_offline(self):
        self.engine.now.return_value = 22630.0
        self.coordinator.last_update_success = False
        original = deepcopy(self.engine.state)
        info = MODULE.excess_time_info(self.coordinator, "hot_water")
        self.assertEqual(info["remaining_hours"], 0)
        self.assertTrue(info["active"])
        self.assertTrue(info["reset_pending"])
        self.assertFalse(info["controller_available"])
        self.assertEqual(self.engine.state, original)

    def test_inactive_run_is_zero_even_if_stale_deadline_remains(self):
        self.engine.state["heating_preset"] = "normal"
        info = MODULE.excess_time_info(self.coordinator, "heating")
        self.assertEqual(info["remaining_hours"], 0)
        self.assertFalse(info["active"])
        self.assertFalse(info["reset_pending"])

    def test_invalid_and_out_of_calendar_timestamps_never_raise_or_emit_nonfinite_time(self):
        for invalid in (None, True, "44200", -1, float("nan"), float("inf"), 1e100, 10**1000):
            with self.subTest(value=str(invalid)[:30]):
                self.engine.state["heating_excess_started_at"] = invalid
                self.engine.state["heating_excess_deadline"] = invalid
                info = MODULE.excess_time_info(self.coordinator, "heating")
                self.assertIsNone(info["started_at"])
                self.assertIsNone(info["deadline"])
                self.assertEqual(info["remaining_hours"], 0)
                self.assertTrue(info["reset_pending"])

    def test_invalid_or_unavailable_clock_produces_zero_and_pending_reset(self):
        for invalid in (None, True, "1000", -1, float("nan"), float("inf"), 10**1000):
            with self.subTest(value=str(invalid)[:30]):
                self.engine.now.return_value = invalid
                info = MODULE.excess_time_info(self.coordinator, "heating")
                self.assertEqual(info["remaining_hours"], 0)
                self.assertTrue(info["reset_pending"])
        self.engine.now.side_effect = OSError("Clock unavailable")
        self.assertTrue(MODULE.excess_time_info(self.coordinator, "heating")["reset_pending"])

    def test_pending_restoration_remains_visible_after_logical_mode_resets(self):
        self.engine.state["hot_water_mode"] = "auto"
        for pending in (
            {"pending_restore": ["hot_water"]},
            {"charging_cancelled": ["hot_water"]},
            {"configuration_restore": {"expired_excess_groups": ["hot_water"]}},
        ):
            with self.subTest(pending=pending):
                self.engine.state.update(pending)
                info = MODULE.excess_time_info(self.coordinator, "hot_water")
                self.assertEqual(info["remaining_hours"], 0)
                self.assertFalse(info["active"])
                self.assertTrue(info["reset_pending"])
                for key in pending:
                    self.engine.state.pop(key)

    def test_low_mode_has_an_independent_twenty_four_hour_clock(self):
        self.engine.state.update(
            hot_water_mode="evening",
            hot_water_low_started_at=1000,
            hot_water_low_deadline=87400,
        )
        self.engine.now.return_value = 4600
        original_state = deepcopy(self.engine.state)
        original_settings = deepcopy(self.engine.settings)
        low = MODULE.low_time_info(self.coordinator)
        self.assertEqual(low["configured_duration_hours"], 24)
        self.assertEqual(low["remaining_hours"], 23)
        self.assertEqual(low["started_at"], "1970-01-01T00:16:40+00:00")
        self.assertEqual(low["deadline"], "1970-01-02T00:16:40+00:00")
        self.assertTrue(low["active"])
        self.assertFalse(low["reset_pending"])
        self.assertEqual(MODULE.excess_time_info(self.coordinator, "heating")["remaining_hours"], 11)
        self.assertEqual(MODULE.excess_time_info(self.coordinator, "hot_water")["remaining_hours"], 0)
        self.assertEqual(MODULE.low_time_attributes(self.coordinator), {
            "low_mode_duration_hours": 24,
            "low_mode_started_at": "1970-01-01T00:16:40+00:00",
            "low_mode_deadline": "1970-01-02T00:16:40+00:00",
            "low_mode_remaining_hours": 23,
            "low_mode_reset_pending": False,
        })
        self.assertEqual(self.engine.state, original_state)
        self.assertEqual(self.engine.settings, original_settings)

    def test_low_mode_deadline_expiry_is_visible_offline_without_resetting_policy(self):
        self.engine.settings["hot_water_low_hours"] = 2
        self.engine.state.update(
            hot_water_mode="evening", hot_water_low_started_at=1000,
            hot_water_low_deadline=8200,
        )
        self.engine.now.return_value = 8230
        self.coordinator.last_update_success = False
        original = deepcopy(self.engine.state)
        low = MODULE.low_time_info(self.coordinator)
        self.assertEqual(low["configured_duration_hours"], 2)
        self.assertEqual(low["remaining_hours"], 0)
        self.assertTrue(low["active"])
        self.assertTrue(low["reset_pending"])
        self.assertFalse(low["controller_available"])
        self.assertEqual(self.engine.state, original)

    def test_low_mode_restoration_and_configuration_expiry_remain_visible_when_inactive(self):
        self.engine.state["hot_water_mode"] = "auto"
        for pending in (
            {"pending_restore": ["hot_water"]},
            {"charging_cancelled": ["hot_water"]},
            {"configuration_restore": {"expired_low_mode": True}},
        ):
            with self.subTest(pending=pending):
                self.engine.state.update(pending)
                low = MODULE.low_time_info(self.coordinator)
                self.assertFalse(low["active"])
                self.assertEqual(low["remaining_hours"], 0)
                self.assertTrue(low["reset_pending"])
                for key in pending:
                    self.engine.state.pop(key)
        self.assertFalse(MODULE.low_time_info(self.coordinator)["reset_pending"])
        self.engine.state["configuration_restore"] = {"expired_excess_groups": ["hot_water"]}
        self.assertFalse(MODULE.low_time_info(self.coordinator)["reset_pending"])
        self.assertTrue(MODULE.excess_time_info(self.coordinator, "hot_water")["reset_pending"])

    def test_low_mode_restart_pending_and_missing_or_invalid_deadline_are_safe(self):
        self.engine.state.update(
            hot_water_mode="evening", hot_water_low_started_at=1000,
            hot_water_low_deadline=87400,
        )
        self.coordinator._recover_pending = True
        self.assertTrue(MODULE.low_time_info(self.coordinator)["reset_pending"])
        self.coordinator._recover_pending = False
        self.assertFalse(MODULE.low_time_info(self.coordinator)["reset_pending"])
        for invalid in (None, True, "87400", -1, float("nan"), float("inf"), 1e100):
            with self.subTest(value=invalid):
                self.engine.state["hot_water_low_deadline"] = invalid
                low = MODULE.low_time_info(self.coordinator)
                self.assertIsNone(low["deadline"])
                self.assertEqual(low["remaining_hours"], 0)
                self.assertTrue(low["reset_pending"])
                self.assertIsNone(MODULE.low_time_attributes(self.coordinator)["low_mode_remaining_hours"])

    def test_stale_low_mode_deadline_does_not_count_down_in_other_presets(self):
        self.engine.state.update(hot_water_low_started_at=1000, hot_water_low_deadline=87400)
        for mode in ("auto", "off", "energy_excess"):
            with self.subTest(mode=mode):
                self.engine.state["hot_water_mode"] = mode
                low = MODULE.low_time_info(self.coordinator)
                self.assertEqual(low["remaining_hours"], 0)
                self.assertFalse(low["active"])
                self.assertFalse(low["reset_pending"])


if __name__ == "__main__":
    unittest.main()
