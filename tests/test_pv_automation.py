"""Execute the delivered YAML policy against simulated HA states and actions."""

import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml
from jinja2.nativetypes import NativeEnvironment

PACKAGE = yaml.safe_load(
    (Path(__file__).parents[1] / "examples/thermia_pv_automation_package.yaml").read_text()
)
VARIABLES = PACKAGE["automation"][0]["actions"][0]["variables"]
H, W, N = (VARIABLES[k] for k in ("heating_entity", "water_entity", "limit_entity"))


class Halt(Exception):
    pass


class Policy:
    def __init__(self):
        self.values = {
            H: "heat",
            W: "auto",
            N: "23",
            "sun.sun": "above_horizon",
            "sensor.net_grid_power": "2000",
            "sensor.net_battery_power": "500",
            "sensor.stp10_0_3se_40_146_battery_soc_total": "90",
            "sensor.thermia_pv_confirmed_alarm_mode": "Home",
            "input_select.thermia_pv_owner": "None",
            "input_boolean.thermia_pv_enabled": "on",
            "input_number.thermia_pv_saved_limit": "23",
            "input_number.thermia_pv_applied_limit": "23",
        }
        for key in ("limit_saved", "water_paused", "water_done", "heating_done"):
            self.values["input_boolean.thermia_pv_" + key] = "off"
        for key in ("water_ready", "heating_ready", "grid_stop", "battery_stop"):
            self.values["binary_sensor.thermia_pv_" + key] = (
                "on" if key.endswith("ready") else "off"
            )
        self.attrs = {
            H: {
                "preset_mode": "Normal",
                "normal_target_temperature": 20.0,
                "normal_target_temperature_low": 20.0,
                "target_temp_low": 20.0,
                "current_temperature": 19.0,
            },
            W: {
                "preset_mode": "Normal",
                "hot_water_top_temperature": 50.0,
                "hot_water_weighted_temperature": 45.0,
            },
            "sensor.net_grid_power": {"unit_of_measurement": "W"},
            "sensor.net_battery_power": {"unit_of_measurement": "W"},
        }
        self.commands = []
        self.env = NativeEnvironment()
        self.env.globals.update(
            states=lambda e: self.values.get(e, "unknown"),
            state_attr=lambda e, k: self.attrs.get(e, {}).get(k),
            is_state=lambda e, s: self.values.get(e) == s,
            has_value=lambda e: self.values.get(e, "unknown") not in ("unknown", "unavailable"),
            is_number=lambda v: self.numeric(v),
        )

    @staticmethod
    def numeric(v):
        try:
            return math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    def render(self, v, ctx):
        return (
            self.env.from_string(v).render(**ctx)
            if isinstance(v, str) and ("{{" in v or "{%" in v)
            else v
        )

    def execute(self, seq, ctx):
        for item in seq:
            if "variables" in item:
                for key, value in item["variables"].items():
                    ctx[key] = self.render(value, ctx)
            elif "condition" in item:
                if not self.render(item["value_template"], ctx):
                    raise Halt()
            elif "stop" in item:
                raise Halt()
            elif "choose" in item:
                for branch in item["choose"]:
                    if all(self.render(c["value_template"], ctx) for c in branch["conditions"]):
                        self.execute(branch["sequence"], ctx)
                        break
            else:
                name = item["action"]
                data = {k: self.render(v, ctx) for k, v in item.get("data", {}).items()}
                if name == "script.thermia_pv_stop":
                    try:
                        self.execute(PACKAGE["script"]["thermia_pv_stop"]["sequence"], data)
                    except Halt:
                        pass
                    continue
                entity = self.render(item["target"]["entity_id"], ctx)
                self.commands.append((name, entity, data))
                if name == "climate.set_preset_mode":
                    if self.values[entity] == "off":
                        raise AssertionError("Turn the thermostat on before selecting a preset")
                    self.attrs[entity]["preset_mode"] = data["preset_mode"]
                    if entity == W:
                        self.values[entity] = "auto"
                elif name == "climate.set_hvac_mode":
                    self.values[entity] = data["hvac_mode"]
                    self.attrs[entity]["preset_mode"] = (
                        None if data["hvac_mode"] == "off" else "Normal"
                    )
                elif name.startswith("input_boolean."):
                    self.values[entity] = "on" if name.endswith("turn_on") else "off"
                elif name == "input_select.select_option":
                    self.values[entity] = data["option"]
                else:
                    self.values[entity] = str(data["value"])

    def poll(self, trigger="poll"):
        try:
            self.execute(
                PACKAGE["automation"][0]["actions"], {"trigger": SimpleNamespace(id=trigger)}
            )
        except Halt:
            pass

    @property
    def owner(self):
        return self.values["input_select.thermia_pv_owner"]


class SurplusTests(unittest.TestCase):
    def test_home_hot_water_first(self):
        p = Policy()
        p.poll()
        self.assertEqual(p.owner, "Hot water")
        self.assertEqual(p.attrs[W]["preset_mode"], "Excess Energy")

    def test_holiday_heating_first_and_restore(self):
        p = Policy()
        p.values["sensor.thermia_pv_confirmed_alarm_mode"] = "Vacation"
        p.poll()
        self.assertEqual(p.owner, "Heating")
        self.assertEqual(float(p.values[N]), 25)
        self.assertEqual(p.values[W], "off")
        p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
        p.poll()
        self.assertEqual(p.owner, "None")
        self.assertEqual(float(p.values[N]), 23)
        self.assertEqual(p.attrs[W]["preset_mode"], "Normal")

    def test_automation_paused_water_is_enabled_before_restoring_its_preset(self):
        for sun, preset in (("above_horizon", "Normal"), ("below_horizon", "Low Mode")):
            with self.subTest(sun=sun):
                p = Policy()
                p.values["sensor.thermia_pv_confirmed_alarm_mode"] = "Vacation"
                p.poll()
                self.assertEqual(p.values[W], "off")
                self.assertIsNone(p.attrs[W]["preset_mode"])
                p.values["sun.sun"] = sun
                p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
                p.commands.clear()
                p.poll()
                active = ("climate.set_hvac_mode", W, {"hvac_mode": "auto"})
                restore = ("climate.set_preset_mode", W, {"preset_mode": preset})
                self.assertLess(p.commands.index(active), p.commands.index(restore))
                self.assertEqual(p.values[W], "auto")
                self.assertEqual(p.attrs[W]["preset_mode"], preset)
                self.assertEqual(p.owner, "None")

    def test_secondary_heating_plus_two(self):
        p = Policy()
        p.values["binary_sensor.thermia_pv_water_ready"] = "off"
        p.poll()
        self.assertEqual(p.owner, "Heating")
        self.assertEqual(float(p.values[N]), 22)

    def test_legacy_fractional_targets_and_ceiling_produce_whole_degree_requests(self):
        for mode in ("heat", "heat_cool"):
            with self.subTest(mode=mode):
                p = Policy()
                p.values[H] = mode
                p.values[N] = "23.5"
                p.values["binary_sensor.thermia_pv_water_ready"] = "off"
                p.attrs[H]["normal_target_temperature"] = 20.9
                p.attrs[H]["normal_target_temperature_low"] = 20.9
                p.attrs[H]["target_temp_low"] = 20.9
                p.poll()
                self.assertEqual(p.owner, "Heating")
                self.assertEqual(float(p.values[N]), 22)
                self.assertEqual(float(p.values["input_number.thermia_pv_saved_limit"]), 23)
                p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
                p.poll()
                self.assertEqual(p.owner, "None")
                self.assertEqual(float(p.values[N]), 23)
                requests = [
                    data["value"] for action, _, data in p.commands
                    if action in {"number.set_value", "input_number.set_value"}
                ]
                self.assertTrue(requests)
                self.assertTrue(all(float(value).is_integer() for value in requests))

    def test_previously_saved_fractional_ceiling_restores_as_safe_whole_degree(self):
        p = Policy()
        p.values[N] = "25"
        p.values["input_select.thermia_pv_owner"] = "Heating"
        p.values["input_boolean.thermia_pv_limit_saved"] = "on"
        p.values["input_number.thermia_pv_applied_limit"] = "25"
        p.values["input_number.thermia_pv_saved_limit"] = "23.5"
        p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
        p.attrs[H]["preset_mode"] = "Excess Energy"
        p.poll()
        self.assertEqual(float(p.values[N]), 23)
        self.assertIn(("number.set_value", N, {"value": 23}), p.commands)
        self.assertEqual(p.owner, "None")

    def test_alarm_settling_does_not_switch_priority(self):
        p = Policy()
        p.poll()
        count = len(p.commands)
        p.values["sensor.thermia_pv_confirmed_alarm_mode"] = "Settling"
        p.poll()
        self.assertEqual(p.owner, "Hot water")
        self.assertEqual(len(p.commands), count)

    def test_confirmed_holiday_preempts_hot_water(self):
        p = Policy()
        p.poll()
        p.values["sensor.thermia_pv_confirmed_alarm_mode"] = "Vacation"
        p.poll()
        self.assertEqual(p.owner, "Heating")

    def test_water_ceiling_hands_over_and_does_not_retrigger(self):
        p = Policy()
        p.poll()
        p.attrs[W]["hot_water_top_temperature"] = 60
        p.poll()
        self.assertEqual(p.owner, "Heating")
        p.poll()
        self.assertEqual(p.owner, "Heating")

    def test_timer_expiry_is_not_reselected(self):
        p = Policy()
        p.poll()
        p.attrs[W]["preset_mode"] = "Normal"
        p.poll()
        self.assertNotEqual(p.owner, "Hot water")
        self.assertEqual(p.values[W], "off")  # Heating takes over and pauses water.
        self.assertIsNone(p.attrs[W]["preset_mode"])

    def test_poll_does_not_restart_clock(self):
        p = Policy()
        p.poll()
        p.commands.clear()
        p.poll()
        self.assertEqual(p.commands, [])

    def test_night_stop_returns_low_mode(self):
        p = Policy()
        p.poll()
        p.values["sun.sun"] = "below_horizon"
        p.poll()
        self.assertEqual(p.attrs[W]["preset_mode"], "Low Mode")
        self.assertEqual(p.owner, "None")

    def test_sunset_selects_low_mode_and_preserves_manual_off(self):
        p = Policy()
        p.values["sun.sun"] = "below_horizon"
        p.poll("sunset")
        self.assertEqual([command for command in p.commands if command[0].startswith("climate.")], [
            ("climate.set_preset_mode", W, {"preset_mode": "Low Mode"}),
        ])
        p.commands.clear()
        p.values[W] = "off"
        p.poll("sunset")
        self.assertEqual([command for command in p.commands if command[0].startswith("climate.")], [])
        self.assertEqual(p.values[W], "off")

    def test_pre_sunset_respects_manual_excess(self):
        p = Policy()
        p.attrs[W]["preset_mode"] = "Excess Energy"
        p.poll("pre_sunset")
        self.assertEqual(p.commands, [])
        p.attrs[W]["preset_mode"] = "Low Mode"
        p.poll("pre_sunset")
        self.assertEqual(p.attrs[W]["preset_mode"], "Normal")

    def test_manual_off_water_is_not_enabled(self):
        p = Policy()
        p.values[W] = "off"
        p.poll()
        self.assertEqual(p.owner, "Heating")
        p.values["binary_sensor.thermia_pv_battery_stop"] = "on"
        p.poll()
        self.assertEqual(p.values[W], "off")

    def test_manual_heating_limit_edit_survives_restoration(self):
        p = Policy()
        p.values["binary_sensor.thermia_pv_water_ready"] = "off"
        p.poll()
        p.values[N] = "26"
        p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
        p.poll()
        self.assertEqual(p.values[N], "26")

    def test_sensor_failure_stops_owned_charge(self):
        p = Policy()
        p.poll()
        p.values["sensor.net_grid_power"] = "unavailable"
        p.poll()
        self.assertEqual(p.owner, "None")

    def test_power_boundaries_and_delay(self):
        p = Policy()
        sensors = {s["unique_id"]: s for s in PACKAGE["template"][0]["binary_sensor"]}
        for s in sensors.values():
            self.assertEqual(s["delay_on"], "00:05:00")
        p.values["sensor.stp10_0_3se_40_146_battery_soc_total"] = "80"
        self.assertFalse(p.render(sensors["thermia_pv_water_ready"]["state"], {}))
        p.values["sensor.stp10_0_3se_40_146_battery_soc_total"] = "80.1"
        for power, key, expected in [
            (1499, "water_ready", False),
            (1500, "water_ready", True),
            (749, "heating_ready", False),
            (750, "heating_ready", True),
            (-199, "grid_stop", False),
            (-200, "grid_stop", True),
        ]:
            p.values["sensor.net_grid_power"] = str(power)
            self.assertEqual(p.render(sensors["thermia_pv_" + key]["state"], {}), expected)
        p.values["sensor.net_battery_power"] = "-0.2"
        p.attrs["sensor.net_battery_power"]["unit_of_measurement"] = "kW"
        self.assertTrue(p.render(sensors["thermia_pv_battery_stop"]["state"], {}))

    def test_holiday_heating_completion_hands_to_hot_water(self):
        p = Policy()
        p.values["sensor.thermia_pv_confirmed_alarm_mode"] = "Vacation"
        p.poll()
        p.attrs[H]["current_temperature"] = 25
        p.poll()
        self.assertEqual(p.owner, "Hot water")
        self.assertEqual(p.attrs[W]["preset_mode"], "Excess Energy")
        self.assertEqual(float(p.values[N]), 23)

    def test_manual_excess_is_not_owned_or_cancelled(self):
        p = Policy()
        p.attrs[W]["preset_mode"] = "Excess Energy"
        p.poll()
        self.assertEqual(p.owner, "None")
        p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
        p.poll()
        self.assertEqual(p.attrs[W]["preset_mode"], "Excess Energy")

    def test_heating_charge_label_matches_mode_and_owned_charge_is_not_reselected(self):
        for mode, label in (
            ("heat", "Excess Energy"),
            ("heat_cool", "Excess Energy (Heating Only)"),
        ):
            with self.subTest(mode=mode):
                p = Policy()
                p.values[H] = mode
                p.values["binary_sensor.thermia_pv_water_ready"] = "off"
                p.poll()
                self.assertEqual(p.owner, "Heating")
                self.assertEqual(p.attrs[H]["preset_mode"], label)
                self.assertIn(("climate.set_preset_mode", H, {"preset_mode": label}), p.commands)
                p.commands.clear()
                p.poll()
                self.assertEqual([
                    c for c in p.commands
                    if c[0].startswith("climate.") or c[0] == "number.set_value"
                ], [])
                self.assertEqual(p.owner, "Heating")
                p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
                p.poll()
                self.assertEqual(p.owner, "None")
                self.assertEqual(p.attrs[H]["preset_mode"], "Normal")
                self.assertIn(("climate.set_preset_mode", H, {"preset_mode": "Normal"}), p.commands)
                self.assertEqual(float(p.values[N]), 23)

    def test_manual_heating_charge_both_labels_block_automation_and_are_not_cancelled(self):
        for mode, label in (
            ("heat", "Excess Energy"),
            ("heat_cool", "Excess Energy (Heating Only)"),
        ):
            with self.subTest(mode=mode):
                p = Policy()
                p.values[H] = mode
                p.attrs[H]["preset_mode"] = label
                p.poll()
                self.assertEqual(p.owner, "None")
                p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
                p.poll()
                self.assertEqual(p.attrs[H]["preset_mode"], label)
                self.assertEqual(p.attrs[W]["preset_mode"], "Normal")
                self.assertEqual([c for c in p.commands if c[0].startswith("climate.")], [])
                self.assertEqual(float(p.values[N]), 23)

    def test_manual_auto_heating_charge_preempts_owned_water_without_taking_ownership(self):
        p = Policy()
        p.values[H] = "heat_cool"
        p.poll()
        self.assertEqual(p.owner, "Hot water")
        p.attrs[H]["preset_mode"] = "Excess Energy (Heating Only)"
        p.commands.clear()
        p.poll()
        self.assertEqual(p.owner, "None")
        self.assertEqual(p.attrs[H]["preset_mode"], "Excess Energy (Heating Only)")
        self.assertEqual(p.attrs[W]["preset_mode"], "Normal")
        self.assertEqual([c for c in p.commands if c[0].startswith("climate.")], [
            ("climate.set_preset_mode", W, {"preset_mode": "Normal"}),
        ])

    def test_auto_heating_uses_saved_normal_lower_target_while_low_profile_is_displayed(self):
        p = Policy()
        p.values[H] = "heat_cool"
        p.attrs[H]["preset_mode"] = "Low Mode"
        p.attrs[H]["target_temp_low"] = 18
        p.values["binary_sensor.thermia_pv_water_ready"] = "off"
        p.poll()
        self.assertEqual(p.owner, "Heating")
        self.assertEqual(float(p.values[N]), 22)
        self.assertEqual(p.attrs[H]["normal_target_temperature_low"], 20)
        self.assertEqual(p.attrs[H]["preset_mode"], "Excess Energy (Heating Only)")
        p.commands.clear()
        p.poll()
        self.assertEqual([
            c for c in p.commands
            if c[0].startswith("climate.") or c[0] == "number.set_value"
        ], [])
        self.assertEqual(float(p.values[N]), 22)

    def test_manual_cool_and_off_do_not_start_heating_surplus_charge(self):
        for mode in ("cool", "off"):
            with self.subTest(mode=mode):
                p = Policy()
                p.values[H] = mode
                p.values["sensor.thermia_pv_confirmed_alarm_mode"] = "Vacation"
                p.values["binary_sensor.thermia_pv_water_ready"] = "off"
                p.poll()
                self.assertEqual(p.owner, "None")
                self.assertEqual(p.values[H], mode)
                self.assertEqual(p.values[N], "23")
                self.assertEqual([c for c in p.commands if c[0].startswith("climate.")], [])

    def test_sunset_stops_owned_auto_heating_and_restores_ceiling_before_water_low_mode(self):
        p = Policy()
        p.values[H] = "heat_cool"
        p.values["binary_sensor.thermia_pv_water_ready"] = "off"
        p.poll()
        p.values["sun.sun"] = "below_horizon"
        p.commands.clear()
        p.poll("sunset")
        self.assertEqual(p.owner, "None")
        self.assertEqual(p.attrs[H]["preset_mode"], "Normal")
        self.assertEqual(float(p.values[N]), 23)
        self.assertEqual(p.attrs[W]["preset_mode"], "Low Mode")
        heating_reset = next(i for i, c in enumerate(p.commands)
                             if c == ("climate.set_preset_mode", H, {"preset_mode": "Normal"}))
        ceiling_reset = next(i for i, c in enumerate(p.commands)
                             if c == ("number.set_value", N, {"value": 23}))
        water_low = next(i for i, c in enumerate(p.commands)
                         if c == ("climate.set_preset_mode", W, {"preset_mode": "Low Mode"}))
        self.assertLess(heating_reset, ceiling_reset)
        self.assertLess(ceiling_reset, water_low)

    def test_night_poll_does_not_reset_active_low_timer_but_recovers_expired_low(self):
        p = Policy()
        p.values["sun.sun"] = "below_horizon"
        p.attrs[W]["preset_mode"] = "Low Mode"
        p.poll()
        self.assertEqual([c for c in p.commands if c[0].startswith("climate.")], [])
        # The integration can return to Normal when a short Low timer expires.
        # The package's existing night recovery deliberately selects Low again.
        p.attrs[W]["preset_mode"] = "Normal"
        p.commands.clear()
        p.poll()
        self.assertEqual([c for c in p.commands if c[0].startswith("climate.")], [
            ("climate.set_preset_mode", W, {"preset_mode": "Low Mode"}),
        ])
        p.commands.clear()
        p.poll()
        self.assertEqual([c for c in p.commands if c[0].startswith("climate.")], [])

    def test_recovery_waits_for_pump_then_restores(self):
        p = Policy()
        p.values["binary_sensor.thermia_pv_water_ready"] = "off"
        p.poll()
        p.values[H] = "unavailable"
        p.values["binary_sensor.thermia_pv_grid_stop"] = "on"
        p.poll()
        self.assertEqual(p.owner, "Heating")
        self.assertEqual(p.values["input_boolean.thermia_pv_limit_saved"], "on")
        p.values[H] = "heat"
        p.attrs[H]["preset_mode"] = "Normal"
        p.poll("startup")
        self.assertEqual(p.owner, "None")
        self.assertEqual(float(p.values[N]), 23)

    def test_alarm_raw_states_and_five_minute_confirmation(self):
        template = PACKAGE["template"][0]["sensor"][0]["state"]
        env = NativeEnvironment()
        now = datetime.now(timezone.utc)
        env.globals["now"] = lambda: now
        for raw, label in [
            ("disarmed", "Home"),
            ("armed_home", "Home"),
            ("armed_away", "Home"),
            ("armed_vacation", "Vacation"),
        ]:
            a = SimpleNamespace(state=raw, last_changed=now - timedelta(seconds=299))
            states = SimpleNamespace(alarm_control_panel=SimpleNamespace(hhome=a))
            self.assertEqual(env.from_string(template).render(states=states).strip(), "Settling")
            a.last_changed = now - timedelta(seconds=300)
            self.assertEqual(env.from_string(template).render(states=states).strip(), label)
