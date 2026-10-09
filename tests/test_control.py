"""Policy tests using an enforcing pump simulator, without Home Assistant."""

import asyncio
import importlib.util
import sys
import types
import unittest
from copy import deepcopy
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus" / "control.py"
PACKAGE_NAME = "thermia_policy_test_package"
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(SOURCE.parent)]
sys.modules[PACKAGE_NAME] = PACKAGE
SPEC = importlib.util.spec_from_file_location(f"{PACKAGE_NAME}.control", SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ControlEngine = MODULE.ControlEngine
NATIVE_SETTINGS = MODULE.NATIVE_SETTINGS


class Clock:
    def __init__(self, now=1000.0):
        self.value = now

    def __call__(self):
        return self.value

    def advance_hours(self, hours):
        self.value += hours * 3600


class Pump:
    """Reject invalid thresholds and supply requests; simulate dropped writes."""

    def __init__(self):
        self.values = {
            "heating_enabled": False,
            "hot_water_enabled": True,
            "passive_cooling_enabled": False,
            "comfort_wheel": 20.0,
            "hot_water_start": 45.0,
            "hot_water_stop": 50.0,
            "fixed_supply_enabled": False,
            "fixed_supply_target": 30.0,
            "max_supply_temperature": 40.0,
            "min_supply_temperature": 17.0,
            "heating_season_stop": 17.0,
            "passive_cooling_supply_target": 16.0,
            **{
                f"heat_curve_supply_{i + 1}": value
                for i, value in enumerate((24, 27, 29, 32, 36, 40, 45))
            },
            "smart_grid_request": 0,
            "immersion_heater": False,
            "anti_legionella_enabled": True,
            "hot_water_boost": False,
            "hot_water_top_temperature": 48.0,
            "hot_water_weighted_temperature": 47.0,
        }
        self.settings = {
            "max_hot_water_temperature": 70.0,
            "max_start_temperature": 65.0,
            "max_heating_temperature": 23.0,
            "charge_supply_temperature": 45.0,
        }
        self.writes = []
        self.saved = []
        self.failures = {}
        self.fail_persist = False
        self.enforce_exclusive_space_requests = False
        self.readback_failures = {}
        self.crash_after_write = None
        self.fail_persist_after_write = None
        self.enforce_native_journal = True
        self.persisted_before_write = []

    def read(self, key):
        return self.values.get(key)

    async def write(self, key, value):
        if self.failures.get(key, 0):
            self.failures[key] -= 1
            raise OSError(f"Simulated lost write: {key}")
        candidate = {**self.values, key: value}
        if candidate["hot_water_start"] > candidate["hot_water_stop"] or (
            candidate["hot_water_start"] == candidate["hot_water_stop"] != 60
        ):
            raise AssertionError("Native controller rejected crossed water thresholds")
        if key == "fixed_supply_target" and value > candidate["max_supply_temperature"]:
            raise AssertionError("Native flow-temperature cap exceeded")
        if (
            candidate["min_supply_temperature"] is not None
            and candidate["max_supply_temperature"] is not None
            and candidate["min_supply_temperature"] > candidate["max_supply_temperature"]
        ):
            raise AssertionError("Native controller rejected crossed supply limits")
        if (
            self.enforce_exclusive_space_requests
            and key == "heating_enabled"
            and value
            and candidate["passive_cooling_enabled"]
        ):
            raise AssertionError("Heat requested before disabling cooling")
        if (
            self.enforce_exclusive_space_requests
            and key == "passive_cooling_enabled"
            and value
            and candidate["heating_enabled"]
        ):
            raise AssertionError("Cool requested before disabling heating")
        # Temporary override values must be durable before touching hardware.
        if key == "fixed_supply_enabled" and value:
            assert self.saved[-1]["overrides"]["heating"]
        if key == "hot_water_stop" and value == self.settings.get("max_hot_water_temperature"):
            assert self.saved[-1]["overrides"]["hot_water"]
        if key in {"hot_water_start", "hot_water_stop"} and (
            candidate["hot_water_start"] == candidate["hot_water_stop"] == 60
        ):
            assert self.saved[-1]["overrides"]["hot_water"]
        if key in NATIVE_SETTINGS and self.enforce_native_journal:
            assert (
                self.saved[-1]["overrides"].get("native_settings")
                or (
                    key == "passive_cooling_supply_target"
                    and self.saved[-1]["overrides"].get("cooling_humidity")
                )
                or (key == "heating_season_stop" and self.saved[-1]["overrides"].get("heating"))
            )
        self.values[key] = value
        self.writes.append((key, value))
        self.persisted_before_write.append(deepcopy(self.saved[-1]) if self.saved else None)
        if self.fail_persist_after_write == key:
            self.fail_persist = True
        if self.crash_after_write == key:
            self.crash_after_write = None
            raise asyncio.CancelledError("Simulated shutdown after the physical write")
        if self.readback_failures.get(key, 0):
            self.readback_failures[key] -= 1
            raise OSError(f"Simulated lost readback: {key}")

    async def persist(self, state):
        if self.fail_persist:
            raise OSError("Simulated durable-store failure")
        self.saved.append(deepcopy(state))

    def engine(self, state=None, *, now=None, **options):
        return ControlEngine(
            self.read,
            self.write,
            self.persist,
            {**self.settings, **options},
            state,
            **({"now": now} if now is not None else {}),
        )


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_water_start_edits_clamp_to_configured_maximum_before_merging_normal_pair(self):
        for maximum, requested, expected in ((55, 57, 55), (55.34, 60, 55), (58, 59, 58)):
            for paired in (False, True):
                with self.subTest(maximum=maximum, requested=requested, paired=paired):
                    pump = Pump()
                    pump.values.update(hot_water_start=54, hot_water_stop=60)
                    engine = pump.engine(
                        max_start_temperature=maximum, max_hot_water_temperature=60
                    )
                    payload = {"target_temp_low": requested}
                    if paired:
                        payload["target_temp_high"] = 60
                    await engine.command("hot_water_temperature_edit", payload)
                    self.assertEqual(
                        (pump.values["hot_water_start"], pump.values["hot_water_stop"]),
                        (expected, 60),
                    )
                    self.assertEqual(engine.state["hot_water_start"], expected)
                    self.assertEqual(engine.state["hot_water_target"], 60)
                    self.assertFalse(any(key == "hot_water_stop" for key, _ in pump.writes))

    async def test_clamped_start_attempt_still_exits_water_presets_and_clears_their_clocks(self):
        for mode in ("evening", "energy_excess", "manual_on"):
            with self.subTest(mode=mode):
                pump = Pump()
                pump.values.update(hot_water_start=45, hot_water_stop=60)
                engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
                await engine.command("hot_water_mode", mode)
                displayed_stop = pump.values["hot_water_stop"]
                await engine.command(
                    "hot_water_temperature_edit",
                    {"target_temp_low": 57, "target_temp_high": displayed_stop},
                )
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (55, 60)
                )
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertIsNone(engine.state["hot_water_excess_deadline"])
                self.assertIsNone(engine.state["hot_water_low_deadline"])
                self.assertFalse(engine.state["overrides"])

    async def test_clamped_start_that_equals_normal_display_publishes_native_value_without_writes(
        self,
    ):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                pump = Pump()
                pump.values.update(
                    hot_water_start=55,
                    hot_water_stop=60,
                    hot_water_enabled=enabled,
                    hot_water_boost=True,
                )
                engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
                await engine.command(
                    "hot_water_temperature_edit", {"target_temp_low": 57, "target_temp_high": 60}
                )
                self.assertEqual(engine.state["hot_water_start"], 55)
                self.assertEqual(engine.state["hot_water_target"], 60)
                self.assertEqual(engine.state["hot_water_mode"], "auto" if enabled else "off")
                self.assertEqual(pump.values["hot_water_enabled"], enabled)
                self.assertTrue(pump.values["hot_water_boost"])
                self.assertFalse(pump.writes)

    async def test_legacy_start_above_new_cap_is_preserved_on_poll_but_corrected_by_explicit_start_edit(
        self,
    ):
        pump = Pump()
        pump.values.update(hot_water_start=57, hot_water_stop=60)
        engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
        await engine.tick(20, 10)
        self.assertEqual(pump.values["hot_water_start"], 57)
        self.assertEqual(engine.state["hot_water_start"], 57)
        self.assertFalse(pump.writes)
        await engine.command(
            "hot_water_temperature_edit", {"target_temp_low": 57, "target_temp_high": 60}
        )
        self.assertEqual(pump.values["hot_water_start"], 55)
        self.assertEqual(engine.state["hot_water_start"], 55)

    async def test_water_start_clamping_does_not_accept_invalid_inputs_or_clamp_stop(self):
        for payload in (
            {"target_temp_low": True},
            {"target_temp_low": float("nan")},
            {"target_temp_low": 29},
            {"target_temp_low": 61},
            {"target_temp_low": 55.5},
            {"target_temp_low": 57, "target_temp_high": 61},
            {"target_temp_low": 57, "target_temp_high": 59},
        ):
            with self.subTest(payload=payload):
                pump = Pump()
                pump.values.update(hot_water_start=45, hot_water_stop=58)
                engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=58)
                previous = deepcopy(engine.state)
                with self.assertRaises(ValueError):
                    await engine.command("hot_water_temperature_edit", payload)
                self.assertFalse(pump.writes)
                for key in ("hot_water_start", "hot_water_target", "hot_water_mode", "overrides"):
                    self.assertEqual(engine.state[key], previous[key])

    async def test_clamped_start_write_readback_commit_and_crash_failures_restore_exact_normal_original(
        self,
    ):
        for failure in ("readback", "commit", "crash"):
            with self.subTest(failure=failure):
                pump = Pump()
                pump.values.update(hot_water_start=54.34, hot_water_stop=60)
                engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
                await engine.command("hot_water_mode", "energy_excess")
                real_write = pump.write
                real_save = pump.persist
                failed = False

                async def write(key, value):
                    nonlocal failed
                    if not failed and key == "hot_water_start" and value == 55:
                        failed = True
                        if failure == "readback":
                            pump.readback_failures[key] = 1
                        elif failure == "crash":
                            pump.crash_after_write = key
                    await real_write(key, value)

                async def save(state):
                    if (
                        failure == "commit"
                        and pump.values["hot_water_start"] == 55
                        and "water_temperature_edit" not in state["overrides"]
                        and state["hot_water_mode"] == "auto"
                    ):
                        raise OSError("Clamped START commit not confirmed")
                    await real_save(state)

                engine.write = write
                engine.persist = save
                with self.assertRaises(asyncio.CancelledError if failure == "crash" else OSError):
                    await engine.command(
                        "hot_water_temperature_edit",
                        {"target_temp_low": 57, "target_temp_high": 60},
                    )
                if failure == "crash":
                    engine = pump.engine(
                        pump.saved[-1], max_start_temperature=55, max_hot_water_temperature=60
                    )
                    self.assertTrue(await engine.recover())
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (54.34, 60)
                )
                self.assertEqual(engine.state["hot_water_start"], 54.34)
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertIsNone(engine.state["hot_water_excess_deadline"])
                self.assertFalse(engine.state["overrides"])

    async def test_hot_water_temperature_collision_repairs_five_degree_pair_and_exits_preset(self):
        for preset in ("auto", "evening", "energy_excess"):
            for payload, expected in (
                ({"target_temp_high": 54}, (49, 54)),
                ({"temperature": 54}, (49, 54)),
                ({"target_temp_low": 56}, (55, 60)),
                ({"target_temp_low": 60}, (55, 60)),
                ({"target_temp_low": 52, "target_temp_high": 51}, (46, 51)),
                ({"target_temp_low": 32, "target_temp_high": 30}, (30, 35)),
            ):
                with self.subTest(preset=preset, payload=payload):
                    pump = Pump()
                    pump.values.update(hot_water_start=54, hot_water_stop=55)
                    engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
                    if preset != "auto":
                        await engine.command("hot_water_mode", preset)
                    offset = len(pump.writes)
                    await engine.command("hot_water_temperature_edit", payload)
                    self.assertEqual(
                        (pump.values["hot_water_start"], pump.values["hot_water_stop"]), expected
                    )
                    self.assertEqual(
                        (engine.state["hot_water_start"], engine.state["hot_water_target"]),
                        expected,
                    )
                    self.assertEqual(engine.state["hot_water_mode"], "auto")
                    self.assertFalse(pump.values["hot_water_boost"])
                    self.assertIsNone(engine.state["hot_water_excess_deadline"])
                    self.assertIsNone(engine.state["hot_water_low_deadline"])
                    self.assertFalse(engine.state["overrides"])
                    saved = pump.persisted_before_write[offset]
                    self.assertEqual(saved["hot_water_mode"], "auto")
                    self.assertEqual(
                        (
                            saved["overrides"]["water_temperature_edit"]["hot_water_start"],
                            saved["overrides"]["water_temperature_edit"]["hot_water_stop"],
                        ),
                        (54, 55),
                    )

    async def test_hot_water_valid_short_pair_and_stock_equal_handles_preserve_untouched_normal_end(
        self,
    ):
        pump = Pump()
        pump.values.update(hot_water_start=54, hot_water_stop=55)
        engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
        await engine.command("hot_water_mode", "energy_excess")
        # Stock UI clamps its first STOP move to displayed START55. Saved
        # Normal54/55 is already valid, so it must not be widened to5°C.
        await engine.command(
            "hot_water_temperature_edit", {"target_temp_low": 55, "target_temp_high": 55}
        )
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (54, 55))
        await engine.command(
            "hot_water_temperature_edit", {"target_temp_low": 54, "target_temp_high": 54}
        )
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (49, 54))

    async def test_repaired_collision_preserves_remaining_sg_owner_without_transient_water_on(self):
        pump = Pump()
        pump.values.update(
            hot_water_start=54,
            hot_water_stop=55,
            hot_water_top_temperature=59,
            hot_water_weighted_temperature=58,
        )
        engine = pump.engine(
            smart_grid_mode="sg_ready", max_start_temperature=55, max_hot_water_temperature=60
        )
        await engine.tick(20, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        real_write = pump.write

        async def write(key, value):
            proposed = pump.values | {key: value}
            if proposed["smart_grid_request"] == 3:
                self.assertFalse(proposed["hot_water_enabled"], (key, value))
            await real_write(key, value)

        engine.write = write
        await engine.command("hot_water_temperature_edit", {"temperature": 54})
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (49, 54))
        self.assertEqual(engine.state["sg_owners"], ["heating"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertTrue(engine.state["overrides"]["water_guard"]["hot_water_enabled"])
        await engine.command("heating_preset", "normal")
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertTrue(pump.values["hot_water_enabled"])

    async def test_hot_water_collision_fits_controller_boundaries_and_rejects_impossible_gap(self):
        for caps, original, payload, expected in (
            ((50, 52), (45, 50), {"target_temp_low": 50}, (47, 52)),
            ((35, 60), (35, 40), {"temperature": 35}, (30, 35)),
            ((50.34, 54.34), (45, 50), {"target_temp_low": 59}, (49, 54)),
        ):
            with self.subTest(caps=caps, payload=payload):
                pump = Pump()
                pump.values.update(hot_water_start=original[0], hot_water_stop=original[1])
                engine = pump.engine(
                    max_start_temperature=caps[0], max_hot_water_temperature=caps[1]
                )
                await engine.command("hot_water_temperature_edit", payload)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), expected
                )
        pump = Pump()
        pump.values.update(hot_water_start=30, hot_water_stop=33)
        engine = pump.engine(max_start_temperature=30, max_hot_water_temperature=34)
        with self.assertRaisesRegex(ValueError, "5°C gap"):
            await engine.command("hot_water_temperature_edit", {"temperature": 30})
        self.assertFalse(pump.writes)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (30, 33))

    async def test_repaired_water_pair_readback_commit_and_crash_failures_restore_original_normal(
        self,
    ):
        for failure in ("readback", "commit", "crash"):
            with self.subTest(failure=failure):
                pump = Pump()
                pump.values.update(hot_water_start=54.34, hot_water_stop=55.34)
                engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
                await engine.command("hot_water_mode", "energy_excess")
                real_write = pump.write
                real_save = pump.persist
                failed = False

                async def write(key, value):
                    if key == "hot_water_stop" and value == 54:
                        if failure == "readback":
                            pump.readback_failures[key] = 1
                        elif failure == "crash":
                            pump.crash_after_write = key
                    await real_write(key, value)

                async def save(state):
                    nonlocal failed
                    if (
                        failure == "commit"
                        and not failed
                        and pump.values["hot_water_stop"] == 54
                        and "water_temperature_edit" not in state["overrides"]
                    ):
                        failed = True
                        raise OSError("Repaired water pair commit failed")
                    await real_save(state)

                engine.write = write
                engine.persist = save
                with self.assertRaises(asyncio.CancelledError if failure == "crash" else OSError):
                    await engine.command("hot_water_temperature_edit", {"temperature": 54})
                if failure == "crash":
                    engine = pump.engine(
                        pump.saved[-1], max_start_temperature=55, max_hot_water_temperature=60
                    )
                    self.assertTrue(await engine.recover())
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (54.34, 55.34)
                )
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertIsNone(engine.state["hot_water_excess_deadline"])
                self.assertFalse(engine.state["overrides"])

    async def test_capped_excess_gap_uses_actual_start_for_pause_resume_and_live_edits(self):
        for requested in (1, 2, 10):
            with self.subTest(requested=requested):
                pump = Pump()
                clock = Clock()
                engine = pump.engine(
                    now=clock,
                    max_start_temperature=55,
                    max_hot_water_temperature=60,
                    hot_water_hysteresis=requested,
                )
                await engine.command("hot_water_mode", "energy_excess")
                start = min(60 - requested, 55)
                info = engine.hot_water_excess_info()
                self.assertTrue(info["available"])
                self.assertEqual(info["configured_restart_gap"], requested)
                self.assertEqual(info["requested_start_temperature"], 60 - requested)
                self.assertEqual(info["effective_start_temperature"], start)
                self.assertEqual(info["effective_restart_gap"], 60 - start)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (start, 60)
                )
                original = deepcopy(engine.state["overrides"]["hot_water"])
                deadline = engine.state["hot_water_excess_deadline"]
                pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=50)
                await engine.tick(20, 10)
                pump.values["hot_water_top_temperature"] = start + 0.01
                await engine.tick(20, 10)
                self.assertFalse(pump.values["hot_water_boost"])
                pump.values["hot_water_top_temperature"] = start
                await engine.tick(20, 10)
                self.assertTrue(pump.values["hot_water_boost"])
                clock.advance_hours(1)
                await engine.command("setting", ("hot_water_hysteresis", 1))
                await engine.tick(20, 10)
                self.assertEqual(pump.values["hot_water_start"], 55)
                self.assertEqual(engine.state["overrides"]["hot_water"], original)
                self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
                await engine.command("hot_water_mode", "auto")
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )

    async def test_excess_pair_info_is_read_only_and_reports_invalid_limits_without_available_claim(
        self,
    ):
        for options in (
            {"max_start_temperature": 29},
            {"max_hot_water_temperature": 59},
            {"max_start_temperature": None},
            {"hot_water_hysteresis": 0.5},
        ):
            with self.subTest(options=options):
                pump = Pump()
                engine = pump.engine(**options)
                original = deepcopy(engine.state)
                info = engine.hot_water_excess_info()
                self.assertFalse(info["available"])
                self.assertIsNone(info["effective_start_temperature"])
                self.assertTrue(info["reason"])
                self.assertEqual(engine.state, original)
                self.assertFalse(pump.writes)

    async def test_heating_excess_uses_exact_native_supply_maximum_and_ignores_retired_target(self):
        for maximum, retired in ((38, 35), (40, 60), (38.34, 35)):
            with self.subTest(maximum=maximum, retired=retired):
                pump = Pump()
                pump.values.update(max_supply_temperature=maximum, fixed_supply_target=30.34)
                engine = pump.engine(charge_supply_temperature=retired)
                await engine.tick(20, 10)
                await engine.command("heating_preset", "pv_charge")
                self.assertEqual(pump.values["fixed_supply_target"], maximum)
                self.assertEqual(pump.values["max_supply_temperature"], maximum)
                self.assertNotIn("charge_supply_temperature", engine.settings)
                original = deepcopy(engine.state["overrides"]["heating"])
                await engine.tick(23, 10)
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertEqual(pump.values["fixed_supply_target"], 30.34)
                self.assertEqual(engine.state["overrides"]["heating"], original)
                await engine.tick(22, 10)
                self.assertTrue(pump.values["heating_enabled"])
                self.assertEqual(pump.values["fixed_supply_target"], maximum)
                await engine.command("heating_preset", "normal")
                self.assertEqual(pump.values["fixed_supply_target"], 30.34)
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertFalse(engine.state["overrides"])

    async def test_heating_excess_rejects_unavailable_or_unsupported_maximum_before_overrides(self):
        for maximum in (None, False, 4.99, 65.01, 20000, float("nan"), float("inf"), "40"):
            for previous in ("normal", "low"):
                with self.subTest(maximum=maximum, previous=previous):
                    pump = Pump()
                    engine = pump.engine()
                    await engine.tick(20, 10)
                    await engine.command("heating_mode", "heat")
                    if previous == "low":
                        await engine.command("heating_preset", "low")
                    original = deepcopy(engine.state["overrides"])
                    pump.values["max_supply_temperature"] = maximum
                    pump.writes.clear()
                    with self.assertRaisesRegex(ValueError, "Supply Line Maximum"):
                        await engine.command("heating_preset", "pv_charge")
                    self.assertEqual(pump.writes, [])
                    self.assertEqual(engine.state["overrides"], original)
                    self.assertEqual(engine.state["heating_preset"], previous)
                    self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_heating_native_maximum_supply_lost_readback_and_crash_restore_exact_original(
        self,
    ):
        for failure in ("readback", "crash"):
            with self.subTest(failure=failure):
                pump = Pump()
                pump.values.update(max_supply_temperature=38.34, fixed_supply_target=30.34)
                engine = pump.engine(charge_supply_temperature=60)
                await engine.tick(20, 10)
                if failure == "readback":
                    pump.readback_failures["fixed_supply_target"] = 1
                    expected = OSError
                else:
                    pump.crash_after_write = "fixed_supply_target"
                    expected = asyncio.CancelledError
                with self.assertRaises(expected):
                    await engine.command("heating_preset", "pv_charge")
                if failure == "crash":
                    saved = deepcopy(pump.saved[-1])
                    saved["settings"]["charge_supply_temperature"] = 35
                    engine = pump.engine(saved)
                    self.assertNotIn("charge_supply_temperature", engine.settings)
                    self.assertNotIn("charge_supply_temperature", engine.state["settings"])
                    self.assertTrue(await engine.recover())
                self.assertEqual(pump.values["fixed_supply_target"], 30.34)
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertFalse(engine.state["overrides"])

    async def test_retired_charging_supply_preference_is_ignored_on_load_and_rejected_on_edit(self):
        pump = Pump()
        state = {"settings": {"charge_supply_temperature": None, "heating_excess_hours": 1.5}}
        engine = pump.engine(state, charge_supply_temperature=35)
        self.assertNotIn("charge_supply_temperature", engine.settings)
        self.assertNotIn("charge_supply_temperature", engine.state["settings"])
        self.assertEqual(engine.settings["heating_excess_hours"], 1.5)
        with self.assertRaisesRegex(ValueError, "Unsupported setting"):
            await engine.command("setting", ("charge_supply_temperature", 60))
        self.assertFalse(pump.writes)

    async def test_default_two_degree_water_gap_requests_58_60_with_start_cap_58(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=59)
        engine = pump.engine(max_start_temperature=58)
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.settings["hot_water_hysteresis"], 2)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        self.assertTrue(pump.values["hot_water_boost"])

    async def test_new_duration_edits_are_whole_hours_and_leave_legacy_clocks_unchanged_on_error(
        self,
    ):
        for duration, group, action, preset in (
            ("heating_excess_hours", "heating_excess", "heating_preset", "pv_charge"),
            ("hot_water_excess_hours", "hot_water_excess", "hot_water_mode", "energy_excess"),
            ("hot_water_low_hours", "hot_water_low", "hot_water_mode", "evening"),
        ):
            with self.subTest(duration=duration):
                pump = Pump()
                clock = Clock()
                engine = pump.engine(now=clock, **{duration: 1.5})
                await engine.tick(20, 10)
                await engine.command(action, preset)
                original = deepcopy(engine.state)
                clock.advance_hours(0.25)
                for value in (0.1, 0.5, 1.5, 12.5, 169, True, None, float("nan")):
                    with self.subTest(value=value):
                        pump.writes.clear()
                        with self.assertRaises(ValueError):
                            await engine.command("setting", (duration, value))
                        self.assertEqual(engine.settings[duration], 1.5)
                        self.assertEqual(
                            engine.state[f"{group}_started_at"], original[f"{group}_started_at"]
                        )
                        self.assertEqual(
                            engine.state[f"{group}_deadline"], original[f"{group}_deadline"]
                        )
                        self.assertEqual(engine.state["overrides"], original["overrides"])
                        self.assertFalse(pump.writes)
                await engine.tick(20, 10)
                self.assertEqual(engine.state[f"{group}_deadline"], 6400)
                await engine.command("setting", (duration, 2))
                self.assertEqual(engine.state[f"{group}_started_at"], 1000)
                self.assertEqual(engine.state[f"{group}_deadline"], 8200)

    async def test_explicit_excess_off_native_restore_failure_cannot_reenable_space_on_poll_or_restart(
        self,
    ):
        for key in ("comfort_wheel", "heating_season_stop"):
            for retry in ("poll", "restart"):
                with self.subTest(key=key, retry=retry):
                    pump = Pump()
                    pump.values.update(
                        comfort_wheel=22.34,
                        heating_season_stop=17.23,
                        heating_enabled=True,
                        passive_cooling_enabled=True,
                    )
                    engine = pump.engine(max_heating_temperature=26)
                    await engine.tick(23.2, 22.3)
                    await engine.command("heating_preset", "pv_charge")
                    pump.failures[key] = 1
                    pump.writes.clear()
                    with self.assertRaises(OSError):
                        await engine.command("heating_mode", "off")
                    saved = pump.saved[-1]
                    self.assertFalse(saved["overrides"]["heating"]["heating_enabled"])
                    self.assertFalse(saved["overrides"]["heating"]["passive_cooling_enabled"])
                    self.assertEqual(saved["heating_mode"], "off")
                    self.assertEqual(saved["heating_preset"], "normal")
                    self.assertIsNone(saved["heating_excess_deadline"])
                    self.assertIsNone(saved["heating_excess_previous_mode"])
                    self.assertFalse(pump.values["heating_enabled"])
                    self.assertFalse(pump.values["passive_cooling_enabled"])
                    if retry == "poll":
                        await engine.tick(23.2, 22.3)
                    else:
                        engine = pump.engine(saved, max_heating_temperature=26)
                        self.assertTrue(await engine.recover())
                        await engine.tick(23.2, 22.3)
                    self.assertEqual(pump.values["comfort_wheel"], 22.34)
                    self.assertAlmostEqual(pump.values["heating_season_stop"], 17.23)
                    self.assertFalse(pump.values["heating_enabled"])
                    self.assertFalse(pump.values["passive_cooling_enabled"])
                    self.assertEqual(engine.state["heating_mode"], "off")
                    self.assertNotIn(("heating_enabled", True), pump.writes)
                    self.assertNotIn(("passive_cooling_enabled", True), pump.writes)
                    self.assertFalse(engine.state["overrides"])

    async def test_explicit_excess_off_with_remaining_water_sg_owner_keeps_off_guard_after_restore_failure(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=22, heating_enabled=True)
        engine = pump.engine(max_heating_temperature=26, smart_grid_mode="sg_ready")
        await engine.tick(23.2, 22.3)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        pump.failures["comfort_wheel"] = 1
        pump.writes.clear()
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "off")
        await engine.tick(23.2, 22.3)
        self.assertEqual(engine.state["sg_owners"], ["hot_water"])
        self.assertFalse(pump.values["heating_enabled"])
        await engine.command("hot_water_mode", "auto")
        await engine.tick(23.2, 22.3)
        self.assertEqual(engine.state["heating_mode"], "off")
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertNotIn(("heating_enabled", True), pump.writes)

    async def test_excess_heat_stop_offset_is_from_original_never_stacks_and_live_change_keeps_timer(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=22, heating_season_stop=17.23)
        clock = Clock()
        engine = pump.engine(now=clock, max_heating_temperature=26)
        await engine.tick(23.2, 22.3)
        await engine.command("heating_preset", "pv_charge")
        self.assertAlmostEqual(pump.values["heating_season_stop"], 19.23)
        original = deepcopy(engine.state["overrides"]["heating"])
        deadline = engine.state["heating_excess_deadline"]
        for _ in range(3):
            await engine.tick(23.2, 22.3)
        self.assertAlmostEqual(pump.values["heating_season_stop"], 19.23)
        await engine.command("setting", ("heating_excess_heat_stop_offset", 5))
        await engine.tick(23.2, 22.3)
        self.assertAlmostEqual(pump.values["heating_season_stop"], 22.23)
        self.assertEqual(engine.state["overrides"]["heating"], original)
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)
        await engine.tick(26, 22.3)
        self.assertAlmostEqual(pump.values["heating_season_stop"], 17.23)
        await engine.command("setting", ("heating_excess_heat_stop_offset", 4))
        await engine.tick(25.9, 22.3)
        self.assertAlmostEqual(pump.values["heating_season_stop"], 17.23)
        self.assertFalse(pump.values["heating_enabled"])
        await engine.tick(25.7, 22.3)
        self.assertAlmostEqual(pump.values["heating_season_stop"], 21.23)
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)

    async def test_excess_heat_stop_offset_invalid_or_above_cap_rejects_before_native_mutation(
        self,
    ):
        for value in (-1, 26, 2.5, True, float("nan")):
            with self.subTest(value=value):
                pump = Pump()
                engine = pump.engine()
                with self.assertRaises(ValueError):
                    await engine.command("setting", ("heating_excess_heat_stop_offset", value))
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.settings["heating_excess_heat_stop_offset"], 2)
        pump = Pump()
        pump.values["heating_season_stop"] = 39
        engine = pump.engine(max_heating_temperature=26)
        await engine.tick(23.2, 22.3)
        with self.assertRaisesRegex(ValueError, "40"):
            await engine.command("heating_preset", "pv_charge")
        self.assertEqual(pump.writes, [])
        self.assertFalse(engine.state["overrides"])
        pump.values["heating_season_stop"] = 17
        await engine.command("heating_preset", "pv_charge")
        before = deepcopy(engine.state)
        pump.writes.clear()
        with self.assertRaisesRegex(ValueError, "40"):
            await engine.command("setting", ("heating_excess_heat_stop_offset", 24))
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.settings["heating_excess_heat_stop_offset"], 2)
        self.assertEqual(engine.state["heating_excess_deadline"], before["heating_excess_deadline"])
        self.assertEqual(engine.state["overrides"], before["overrides"])

    async def test_charge_sensor_loss_restores_dial_and_season_with_original_normal_permission(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=22.34, heating_season_stop=17.23, heating_enabled=True)
        engine = pump.engine(max_heating_temperature=26)
        await engine.tick(23.2, 22.3)
        await engine.command("heating_preset", "pv_charge")
        await engine.tick(None, 22.3)
        self.assertEqual(pump.values["comfort_wheel"], 22.34)
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertTrue(pump.values["heating_enabled"])
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_legacy_charge_journal_recovers_without_inventing_dial_or_season_originals(self):
        pump = Pump()
        pump.values.update(
            fixed_supply_enabled=True,
            fixed_supply_target=40,
            heating_enabled=True,
            comfort_wheel=22.34,
            heating_season_stop=17.23,
        )
        state = {
            "heating_preset": "pv_charge",
            "heating_mode": "heat",
            "heating_target": 22.34,
            "managed_heating": True,
            "overrides": {
                "heating": {
                    "heating_enabled": False,
                    "passive_cooling_enabled": False,
                    "fixed_supply_enabled": False,
                    "fixed_supply_target": 30,
                }
            },
            "charge_active": {"heating": True, "hot_water": False},
        }
        engine = pump.engine(state, max_heating_temperature=26)
        self.assertTrue(await engine.recover())
        self.assertEqual(pump.values["comfort_wheel"], 22.34)
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertFalse(
            any(key in {"comfort_wheel", "heating_season_stop"} for key, _ in pump.writes)
        )
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(pump.values["heating_enabled"])
        self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_charge_crash_after_native_dial_or_season_write_restores_durable_originals(self):
        for key in ("comfort_wheel", "heating_season_stop"):
            with self.subTest(key=key):
                pump = Pump()
                pump.values.update(comfort_wheel=22.34, heating_season_stop=17.23)
                engine = pump.engine(max_heating_temperature=26)
                await engine.tick(23.2, 22.3)
                pump.crash_after_write = key
                with self.assertRaises(asyncio.CancelledError):
                    await engine.command("heating_preset", "pv_charge")
                self.assertEqual(pump.saved[-1]["overrides"]["heating"]["comfort_wheel"], 22.34)
                self.assertEqual(
                    pump.saved[-1]["overrides"]["heating"]["heating_season_stop"], 17.23
                )
                restarted = pump.engine(pump.saved[-1], max_heating_temperature=26)
                self.assertTrue(await restarted.recover())
                self.assertEqual(pump.values["comfort_wheel"], 22.34)
                self.assertEqual(pump.values["heating_season_stop"], 17.23)
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertEqual(restarted.state["heating_preset"], "normal")

    async def test_excess_copies_live_ceiling_to_native_dial_and_temporarily_overrides_season_stop(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=22.34, heating_season_stop=17.23, heating_enabled=True)
        engine = pump.engine(max_heating_temperature=26)
        await engine.tick(23.2, 22.3)
        await engine.command("heating_preset", "pv_charge")
        self.assertEqual(pump.values["comfort_wheel"], 26)
        self.assertAlmostEqual(pump.values["heating_season_stop"], 19.23)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.values["fixed_supply_target"], 40)
        self.assertTrue(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["max_supply_temperature"], 40)
        snapshot = engine.state["overrides"]["heating"]
        self.assertEqual(snapshot["comfort_wheel"], 22.34)
        self.assertEqual(snapshot["heating_season_stop"], 17.23)
        for key in ("comfort_wheel", "heating_season_stop"):
            index = next(i for i, write in enumerate(pump.writes) if write[0] == key)
            self.assertEqual(
                pump.persisted_before_write[index]["overrides"]["heating"][key],
                22.34 if key == "comfort_wheel" else 17.23,
            )
        await engine.command("heating_preset", "normal")
        self.assertEqual(pump.values["comfort_wheel"], 22.34)
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(engine.state["overrides"])

    async def test_excess_pause_restores_season_and_flow_but_retains_displayed_dial_then_resumes(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=22, heating_season_stop=17.23)
        clock = Clock()
        engine = pump.engine(now=clock, max_heating_temperature=26)
        await engine.tick(23.2, 22.3)
        await engine.command("heating_preset", "pv_charge")
        deadline = engine.state["heating_excess_deadline"]
        await engine.tick(26, 22.3)
        self.assertEqual(pump.values["comfort_wheel"], 26)
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["fixed_supply_target"], 30)
        pump.values["comfort_wheel"] = 22
        await engine.tick(25.9, 22.3)
        self.assertEqual(pump.values["comfort_wheel"], 26)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        await engine.tick(25.7, 22.3)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertAlmostEqual(pump.values["heating_season_stop"], 19.23)
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)
        await engine.command("setting", ("max_heating_temperature", 27))
        await engine.tick(25.7, 22.3)
        self.assertEqual(pump.values["comfort_wheel"], 27)
        self.assertEqual(engine.state["overrides"]["heating"]["comfort_wheel"], 22)
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)

    async def test_initial_above_excess_ceiling_copies_dial_with_space_off_and_season_unmodified(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=22, heating_enabled=True, heating_season_stop=17.23)
        engine = pump.engine(max_heating_temperature=26)
        await engine.tick(27, 22.3)
        await engine.command("heating_preset", "pv_charge")
        self.assertEqual(pump.values["comfort_wheel"], 26)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertNotIn(("heating_season_stop", 19.23), pump.writes)
        await engine.command("heating_mode", "off")
        self.assertEqual(pump.values["comfort_wheel"], 22)
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_excess_dial_and_season_write_failures_roll_back_exact_originals_before_retry(
        self,
    ):
        for key in ("comfort_wheel", "heating_season_stop"):
            for lost_readback in (False, True):
                with self.subTest(key=key, lost_readback=lost_readback):
                    pump = Pump()
                    pump.values.update(comfort_wheel=22.34, heating_season_stop=17.23)
                    engine = pump.engine(max_heating_temperature=26)
                    await engine.tick(23.2, 22.3)
                    failures = pump.readback_failures if lost_readback else pump.failures
                    failures[key] = 1
                    with self.assertRaises(OSError):
                        await engine.command("heating_preset", "pv_charge")
                    self.assertEqual(pump.values["comfort_wheel"], 22.34)
                    self.assertEqual(pump.values["heating_season_stop"], 17.23)
                    self.assertFalse(pump.values["fixed_supply_enabled"])
                    self.assertEqual(engine.state["heating_preset"], "normal")
                    self.assertFalse(engine.state["overrides"])
                    await engine.command("heating_preset", "pv_charge")
                    self.assertEqual(pump.values["comfort_wheel"], 26)
                    self.assertAlmostEqual(pump.values["heating_season_stop"], 19.23)

    async def test_excess_rejects_unreadable_native_settings_or_below_dial_range_before_writes(
        self,
    ):
        for key, value in (
            ("comfort_wheel", None),
            ("comfort_wheel", 0),
            ("heating_season_stop", None),
            ("heating_season_stop", float("nan")),
            ("heating_season_stop", 20000),
            ("max_heating_temperature", 9),
        ):
            with self.subTest(key=key, value=value):
                pump = Pump()
                if key != "max_heating_temperature":
                    pump.values[key] = value
                engine = pump.engine(
                    max_heating_temperature=value if key == "max_heating_temperature" else 26
                )
                await engine.tick(23.2, 22.3)
                with self.assertRaises(ValueError):
                    await engine.command("heating_preset", "pv_charge")
                self.assertEqual(pump.writes, [])
                self.assertFalse(engine.state["overrides"])
                self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_excess_timeout_restart_and_off_restore_native_dial_and_season_without_recharging(
        self,
    ):
        for exit_route in ("timeout", "restart", "off"):
            with self.subTest(exit_route=exit_route):
                pump = Pump()
                pump.values.update(
                    comfort_wheel=22.34, heating_season_stop=17.23, heating_enabled=True
                )
                clock = Clock()
                engine = pump.engine(
                    now=clock, max_heating_temperature=26, heating_excess_hours=0.1
                )
                await engine.tick(23.2, 22.3)
                await engine.command("heating_preset", "pv_charge")
                pump.writes.clear()
                if exit_route == "timeout":
                    clock.advance_hours(0.1)
                    await engine.tick(23.2, 22.3)
                elif exit_route == "restart":
                    engine = pump.engine(pump.saved[-1], now=clock, max_heating_temperature=26)
                    self.assertTrue(await engine.recover())
                else:
                    await engine.command("heating_mode", "off")
                self.assertEqual(pump.values["comfort_wheel"], 22.34)
                self.assertEqual(pump.values["heating_season_stop"], 17.23)
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertNotIn(("comfort_wheel", 26), pump.writes)
                self.assertNotIn(("heating_season_stop", 19.23), pump.writes)
                self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_failed_excess_native_restore_keeps_both_off_and_durable_originals_until_retry(
        self,
    ):
        for key in ("comfort_wheel", "heating_season_stop"):
            with self.subTest(key=key):
                pump = Pump()
                pump.values.update(comfort_wheel=22.34, heating_season_stop=17.23)
                engine = pump.engine(max_heating_temperature=26)
                await engine.tick(23.2, 22.3)
                await engine.command("heating_preset", "pv_charge")
                pump.failures[key] = 1
                with self.assertRaises(OSError):
                    await engine.command("heating_preset", "normal")
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertEqual(
                    engine.state["overrides"]["heating"][key],
                    22.34 if key == "comfort_wheel" else 17.23,
                )
                self.assertIn("heating", engine.state["pending_restore"])
                restarted = pump.engine(pump.saved[-1], max_heating_temperature=26)
                self.assertTrue(await restarted.recover())
                self.assertEqual(pump.values["comfort_wheel"], 22.34)
                self.assertEqual(pump.values["heating_season_stop"], 17.23)
                self.assertEqual(restarted.state["heating_preset"], "normal")

    async def test_native_targets_follow_all_heating_modes_and_supported_presets(self):
        for mode, presets in (
            ("heat", ("normal", "low", "vacation", "pv_charge")),
            ("heat_cool", ("normal", "low", "vacation", "pv_charge")),
            ("cool", ("normal", "vacation")),
            ("off", ("normal",)),
        ):
            for preset in presets:
                with self.subTest(mode=mode, preset=preset):
                    pump = Pump()
                    pump.values.update(comfort_wheel=22, heating_season_stop=17.23)
                    engine = pump.engine(max_heating_temperature=26)
                    await engine.tick(23.2, 22.3)
                    await engine.command("heating_target", 22)
                    await engine.command("heating_range", (20, 24))
                    await engine.command("heating_mode", mode)
                    pump.writes.clear()
                    await engine.command("heating_preset", preset)
                    await engine.tick(23.2, 22.3)
                    expected = (
                        26
                        if preset == "pv_charge"
                        else 17
                        if preset == "vacation" and mode in {"heat", "heat_cool"}
                        else 20
                        if mode == "heat" and preset == "low"
                        else 18
                        if mode == "heat_cool" and preset == "low"
                        else 20
                        if mode == "heat_cool"
                        else 22
                    )
                    self.assertEqual(pump.values["comfort_wheel"], expected)
                    self.assertEqual(
                        pump.values["heating_season_stop"],
                        19.23 if preset == "pv_charge" else 17.23,
                    )
                    if mode in {"cool", "off"}:
                        self.assertFalse(any(key == "comfort_wheel" for key, _ in pump.writes))
                        self.assertFalse(pump.values["heating_enabled"])

    async def test_auto_excess_temperature_noop_and_changed_endpoint_match_displayed_charge_ceiling(
        self,
    ):
        pump = Pump()
        engine = pump.engine(max_heating_temperature=26)
        await engine.tick(23.2, 22.3)
        await engine.command("heating_mode", "heat_cool")
        await engine.command("heating_preset", "pv_charge")
        deadline = engine.state["heating_excess_deadline"]
        await engine.command(
            "heating_temperature_edit", {"target_temp_low": 26, "target_temp_high": 26}
        )
        self.assertEqual(engine.state["heating_preset"], "pv_charge")
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)
        await engine.command(
            "heating_temperature_edit", {"target_temp_low": 23, "target_temp_high": 26}
        )
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual((engine.state["heating_low"], engine.state["heating_high"]), (23, 24))
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(pump.values["heating_season_stop"], 17)

    async def test_boiler_native_pair_and_boost_matrix_remains_independent_of_heating_charge(self):
        for water_mode, pair, boosted in (
            ("auto", (45, 50), False),
            ("evening", (35, 40), False),
            ("energy_excess", (58, 60), True),
            ("off", (45, 50), False),
        ):
            with self.subTest(water_mode=water_mode):
                pump = Pump()
                engine = pump.engine(max_heating_temperature=26)
                await engine.tick(23.2, 22.3)
                await engine.command("heating_preset", "pv_charge")
                await engine.command("hot_water_mode", water_mode)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), pair
                )
                self.assertEqual(pump.values["hot_water_boost"], boosted)
                self.assertEqual(pump.values["hot_water_enabled"], water_mode != "off")
                self.assertEqual(pump.values["comfort_wheel"], 26)
                self.assertAlmostEqual(pump.values["heating_season_stop"], 19)
                await engine.command("hot_water_mode", "auto")
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertTrue(pump.values["heating_enabled"])

    async def test_profile_off_restores_normal_targets_and_reactivates_in_normal(self):
        for mode, preset in (
            ("heat", "low"),
            ("heat", "vacation"),
            ("heat_cool", "low"),
            ("heat_cool", "vacation"),
            ("cool", "vacation"),
        ):
            with self.subTest(mode=mode, preset=preset):
                pump = Pump()
                engine = pump.engine(inside_humidity_sensor="sensor.rh")
                await engine.tick(27, 10, 60)
                await engine.command("heating_target", 23)
                await engine.command("heating_range", (22, 26))
                await engine.command("heating_mode", mode)
                normal_targets = tuple(
                    engine.state[key] for key in ("heating_target", "heating_low", "heating_high")
                )
                await engine.command("heating_preset", preset)
                pump.writes.clear()
                await engine.command("heating_mode", "off")
                self.assertEqual(engine.state["heating_mode"], "off")
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(
                    tuple(
                        engine.state[key]
                        for key in ("heating_target", "heating_low", "heating_high")
                    ),
                    normal_targets,
                )
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertNotIn(("heating_enabled", True), pump.writes)
                self.assertNotIn(("passive_cooling_enabled", True), pump.writes)
                self.assertFalse(engine.state["overrides"])
                self.assertIsNone(engine.state["heating_excess_deadline"])
                self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
                await engine.command("heating_enabled", True)
                self.assertEqual(engine.state["heating_mode"], mode)
                self.assertEqual(engine.state["heating_preset"], "normal")
                if mode in {"heat", "heat_cool"}:
                    self.assertEqual(pump.values["comfort_wheel"], 23 if mode == "heat" else 22)

    async def test_failed_profile_off_restore_keeps_both_off_through_poll_restart_or_shutdown(self):
        for preset in ("low", "vacation"):
            for retry in ("poll", "restart", "shutdown"):
                with self.subTest(preset=preset, retry=retry):
                    pump = Pump()
                    pump.values.update(
                        comfort_wheel=23.34, heating_enabled=True, passive_cooling_enabled=True
                    )
                    engine = pump.engine()
                    await engine.tick(27, 10)
                    await engine.command("heating_preset", preset)
                    pump.failures["comfort_wheel"] = 1
                    pump.writes.clear()
                    with self.assertRaises(OSError):
                        await engine.command("heating_mode", "off")
                    snapshot = engine.state["overrides"]["heating_profile"]
                    self.assertFalse(snapshot["heating_enabled"])
                    self.assertFalse(snapshot["passive_cooling_enabled"])
                    self.assertEqual(snapshot["policy"]["heating_mode"], "off")
                    self.assertEqual(snapshot["policy"]["heating_preset"], "normal")
                    self.assertIn("heating_profile", engine.state["pending_restore"])
                    self.assertFalse(pump.values["heating_enabled"])
                    self.assertFalse(pump.values["passive_cooling_enabled"])
                    for saved in pump.persisted_before_write[-len(pump.writes) :]:
                        self.assertFalse(saved["overrides"]["heating_profile"]["heating_enabled"])
                        self.assertFalse(
                            saved["overrides"]["heating_profile"]["passive_cooling_enabled"]
                        )
                    if retry == "poll":
                        await engine.tick(27, 10)
                    elif retry == "restart":
                        engine = pump.engine(pump.saved[-1])
                        self.assertTrue(await engine.recover())
                    else:
                        self.assertTrue(await engine.shutdown())
                    self.assertEqual(engine.state["heating_mode"], "off")
                    self.assertEqual(engine.state["heating_preset"], "normal")
                    self.assertEqual(pump.values["comfort_wheel"], 23.34)
                    self.assertFalse(pump.values["heating_enabled"])
                    self.assertFalse(pump.values["passive_cooling_enabled"])
                    self.assertNotIn(("heating_enabled", True), pump.writes)
                    self.assertNotIn(("passive_cooling_enabled", True), pump.writes)
                    self.assertFalse(engine.state["overrides"])

    async def test_profile_off_disarms_humidity_resume_before_failed_supply_restore(self):
        pump = Pump()
        pump.values["passive_cooling_enabled"] = True
        engine = pump.engine(inside_humidity_sensor="sensor.rh")
        await engine.tick(25, 10, 60)
        await engine.command("heating_preset", "vacation")
        self.assertGreater(pump.values["passive_cooling_supply_target"], 16)
        pump.failures["passive_cooling_supply_target"] = 1
        pump.writes.clear()
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "off")
        self.assertFalse(engine.state["cooling_humidity_resume_pending"])
        self.assertFalse(engine.state["overrides"]["cooling_humidity"]["requested"])
        restarted = pump.engine(pump.saved[-1], inside_humidity_sensor="sensor.rh")
        self.assertTrue(await restarted.recover())
        await restarted.tick(25, 10, 60)
        self.assertEqual(restarted.state["heating_mode"], "off")
        self.assertEqual(restarted.state["heating_preset"], "normal")
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertNotIn(("passive_cooling_enabled", True), pump.writes)

    async def test_profile_off_with_remaining_boiler_sg_owner_cannot_restore_heat_permission(self):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(19, 10)
        await engine.command("heating_mode", "heat")
        await engine.command("heating_preset", "vacation")
        await engine.command("hot_water_mode", "energy_excess")
        deadline = engine.state["hot_water_excess_deadline"]
        pump.writes.clear()
        await engine.command("heating_mode", "off")
        self.assertEqual(engine.state["sg_owners"], ["hot_water"])
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertFalse(pump.values["heating_enabled"])
        await engine.command("hot_water_mode", "auto")
        await engine.tick(19, 10)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.state["heating_mode"], "off")
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertNotIn(("heating_enabled", True), pump.writes)

    async def test_unmanaged_guard_does_not_reverse_front_panel_off_with_or_without_raised_floor(
        self,
    ):
        for original_floor in (16, 24):
            with self.subTest(original_floor=original_floor):
                pump = Pump()
                pump.values.update(
                    passive_cooling_enabled=True, passive_cooling_supply_target=original_floor
                )
                engine = pump.engine(
                    inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True
                )
                await engine.tick(25, 10, 60)
                self.assertTrue(pump.values["passive_cooling_enabled"])
                pump.values["passive_cooling_enabled"] = False
                pump.writes.clear()
                await engine.tick(25, 10, 60)
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertNotIn(("passive_cooling_enabled", True), pump.writes)
                self.assertEqual(pump.values["passive_cooling_supply_target"], original_floor)
                self.assertFalse(engine.state["cooling_humidity_requested"])

    async def test_guard_release_on_failure_keeps_durable_resume_intent_until_verified_retry(self):
        for lost_readback in (False, True):
            with self.subTest(lost_readback=lost_readback):
                pump = Pump()
                pump.values["passive_cooling_enabled"] = True
                engine = pump.engine(inside_humidity_sensor="sensor.rh")
                await engine.tick(25, 10, 60)
                await engine.command("heating_preset", "vacation")
                restarted = pump.engine(pump.saved[-1], inside_humidity_sensor="sensor.rh")
                restarted.update_measurements(25, 10, None)
                original_write = pump.write
                failed = False

                async def fail_final_on(key, value):
                    nonlocal failed
                    if (
                        key == "passive_cooling_enabled"
                        and value
                        and pump.values["passive_cooling_supply_target"] == 16
                        and not failed
                    ):
                        failed = True
                        if lost_readback:
                            await original_write(key, value)
                        raise OSError("Original cooling On was not confirmed")
                    await original_write(key, value)

                restarted.write = fail_final_on
                with self.assertRaisesRegex(OSError, "not confirmed"):
                    await restarted.recover()
                self.assertTrue(restarted.state["cooling_humidity_resume_pending"])
                self.assertTrue(pump.saved[-1]["cooling_humidity_resume_pending"])
                self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
                self.assertTrue(await restarted.recover())
                self.assertTrue(pump.values["passive_cooling_enabled"])
                self.assertFalse(restarted.state["cooling_humidity_resume_pending"])
                self.assertFalse(pump.saved[-1]["cooling_humidity_resume_pending"])

    async def test_disabled_humidity_does_not_reassert_native_permission_after_front_panel_off(
        self,
    ):
        pump = Pump()
        pump.values["passive_cooling_enabled"] = True
        engine = pump.engine()
        await engine.tick(25, 10)
        self.assertFalse(engine.state["cooling_humidity_requested"])
        pump.values["passive_cooling_enabled"] = False
        pump.writes.clear()
        await engine.tick(25, 10)
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["heating_mode"], "off")

    async def test_vacation_cooling_restart_restores_native_cool_after_floor_when_normal_guard_disabled(
        self,
    ):
        pump = Pump()
        pump.values["passive_cooling_enabled"] = True
        engine = pump.engine(inside_humidity_sensor="sensor.rh")
        await engine.tick(25, 10, 60)
        await engine.command("heating_preset", "vacation")
        self.assertGreater(pump.values["passive_cooling_supply_target"], 16)
        restarted = pump.engine(pump.saved[-1], inside_humidity_sensor="sensor.rh")
        restarted.update_measurements(25, 10, None)
        pump.writes.clear()
        self.assertTrue(await restarted.recover())
        self.assertEqual(restarted.state["heating_preset"], "normal")
        self.assertEqual(restarted.state["heating_mode"], "cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
        last_on = max(
            i for i, write in enumerate(pump.writes) if write == ("passive_cooling_enabled", True)
        )
        last_floor = max(
            i
            for i, write in enumerate(pump.writes)
            if write == ("passive_cooling_supply_target", 16)
        )
        self.assertGreater(last_on, last_floor)
        self.assertFalse(restarted.state["cooling_humidity_requested"])

    async def test_profile_prewrite_and_final_commit_store_failures_restore_native_normal_values(
        self,
    ):
        for stage in ("prewrite", "commit"):
            with self.subTest(stage=stage):
                pump = Pump()
                pump.values.update(heating_enabled=True, comfort_wheel=23.34)
                failed = False

                async def persist(state):
                    nonlocal failed
                    hit = (
                        stage == "prewrite"
                        and "heating_profile" in state["overrides"]
                        and state["heating_preset"] == "normal"
                    ) or (
                        stage == "commit"
                        and state["heating_preset"] == "vacation"
                        and "heating_target_edit" not in state["overrides"]
                    )
                    if hit and not failed:
                        failed = True
                        raise OSError("Simulated profile save failure")
                    await pump.persist(state)

                engine = ControlEngine(pump.read, pump.write, persist, pump.settings)
                with self.assertRaises(OSError):
                    await engine.command("heating_preset", "vacation")
                self.assertTrue(failed)
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(engine.state["heating_target"], 23.34)
                self.assertEqual(pump.values["comfort_wheel"], 23.34)
                self.assertFalse(engine.state["overrides"])

    async def test_vacation_missing_room_pauses_without_leaving_preset_or_blocking_boiler_ceiling(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh")
        await engine.tick(25, 10, 60)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_preset", "vacation")
        await engine.command("hot_water_mode", "energy_excess")
        pump.values.update(hot_water_top_temperature=62, hot_water_weighted_temperature=61)
        await engine.tick(None, 10, 60)
        self.assertEqual(engine.state["heating_preset"], "vacation")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")

    async def test_humidity_fresh_read_only_override_uses_explicit_missing_value_without_mutation(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        engine.update_measurements(25, 10, 60)
        before = deepcopy(engine.state)
        info = engine.cooling_humidity_info(inside=25, humidity=None)
        self.assertEqual(info["status"], "missing_humidity")
        self.assertIsNone(info["dew_point"])
        self.assertEqual(engine.state, before)
        self.assertEqual(engine._humidity, 60)
        self.assertEqual(pump.writes, [])

    async def test_heating_profiles_preserve_mode_normal_targets_and_apply_native_heat(self):
        for mode, preset, expected in (
            ("heat", "low", 20),
            ("heat", "vacation", 17),
            ("heat_cool", "low", 18),
            ("heat_cool", "vacation", 17),
            ("off", "low", 22),
            ("off", "vacation", 22),
        ):
            with self.subTest(mode=mode, preset=preset):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(19, 10)
                await engine.command("heating_target", 22)
                await engine.command("heating_mode", mode)
                originals = tuple(
                    engine.state[key] for key in ("heating_target", "heating_low", "heating_high")
                )
                await engine.command("heating_preset", preset)
                self.assertEqual(engine.state["heating_mode"], mode)
                self.assertEqual(engine.effective_heating_target(), expected)
                self.assertEqual(
                    tuple(
                        engine.state[key]
                        for key in ("heating_target", "heating_low", "heating_high")
                    ),
                    originals,
                )
                if mode != "off":
                    self.assertEqual(pump.values["comfort_wheel"], expected)
                else:
                    self.assertFalse(pump.values["heating_enabled"])
                    self.assertFalse(pump.values["passive_cooling_enabled"])
                await engine.command("heating_preset", preset)
                self.assertEqual(engine.effective_heating_target(), expected)
                await engine.command("heating_preset", "normal")
                self.assertEqual(engine.state["heating_mode"], mode)
                self.assertEqual(
                    tuple(
                        engine.state[key]
                        for key in ("heating_target", "heating_low", "heating_high")
                    ),
                    originals,
                )
                self.assertNotIn("heating_profile", engine.state["overrides"])

    async def test_heating_low_floor_and_vacation_cooling_offset_keep_cooling_semantics(self):
        pump = Pump()
        engine = pump.engine(heating_low_offset=25, heating_vacation_cooling_offset=2)
        await engine.tick(25, 10)
        await engine.command("heating_mode", "heat")
        await engine.command("heating_preset", "low")
        self.assertEqual(pump.values["comfort_wheel"], 10)
        await engine.command("heating_mode", "cool")
        self.assertEqual(engine.state["heating_preset"], "normal")
        await engine.command("heating_target", 24)
        await engine.command("heating_preset", "vacation")
        self.assertEqual(engine.effective_heating_target(), 22)
        self.assertEqual(engine.state["heating_mode"], "cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertFalse(pump.values["heating_enabled"])
        await engine.tick(21.6, 10)
        self.assertFalse(pump.values["passive_cooling_enabled"])
        await engine.tick(22.4, 10)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        for preset in ("low", "pv_charge"):
            pump.writes.clear()
            with self.assertRaisesRegex(ValueError, "Cool"):
                await engine.command("heating_preset", preset)
            self.assertEqual(pump.writes, [])

    async def test_invalid_vacation_auto_pair_rejected_before_write_but_cool_scalar_edit_works(
        self,
    ):
        pump = Pump()
        engine = pump.engine(heating_vacation_cooling_offset=10)
        await engine.tick(20, 10)
        await engine.command("heating_mode", "heat_cool")
        pump.writes.clear()
        with self.assertRaisesRegex(ValueError, "effective cooling"):
            await engine.command("heating_preset", "vacation")
        self.assertEqual(pump.writes, [])
        await engine.command("heating_mode", "cool")
        await engine.command("heating_target", 24)
        await engine.command("heating_preset", "vacation")
        self.assertEqual(engine.effective_heating_target(), 14)
        await engine.command("heating_temperature_edit", {"temperature": 25})
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["heating_mode"], "cool")
        self.assertEqual(engine.state["heating_target"], 25)

    async def test_all_profile_temperature_edits_return_normal_and_restore_untouched_endpoint(self):
        for mode, preset, payload in (
            ("heat", "low", {"temperature": 23}),
            ("heat", "vacation", {"temperature": 23}),
            ("off", "low", {"temperature": 23}),
            ("off", "vacation", {"temperature": 23}),
            ("cool", "vacation", {"temperature": 25}),
            ("heat_cool", "low", {"target_temp_low": 18, "target_temp_high": 25}),
            ("heat_cool", "vacation", {"target_temp_low": 17, "target_temp_high": 25}),
        ):
            with self.subTest(mode=mode, preset=preset):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(22, 10)
                await engine.command("heating_mode", mode)
                await engine.command("heating_preset", preset)
                await engine.command("heating_temperature_edit", payload)
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(engine.state["heating_mode"], mode)
                if mode == "heat_cool":
                    self.assertEqual(
                        (engine.state["heating_low"], engine.state["heating_high"]), (20, 25)
                    )
                self.assertNotIn("heating_profile", engine.state["overrides"])

    async def test_profile_failed_comfort_readback_rolls_back_native_and_normal_policy(self):
        pump = Pump()
        pump.values["comfort_wheel"] = 23.34
        engine = pump.engine()
        await engine.tick(20, 10)
        await engine.command("heating_mode", "heat")
        original = deepcopy(engine.state)
        original_dial = pump.values["comfort_wheel"]
        pump.readback_failures["comfort_wheel"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_preset", "vacation")
        self.assertEqual(pump.values["comfort_wheel"], original_dial)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["heating_target"], original["heating_target"])
        self.assertNotIn("heating_profile", engine.state["overrides"])

    async def test_profile_crash_recovery_restores_exact_original_without_resuming_profile(self):
        pump = Pump()
        pump.values["comfort_wheel"] = 23.34
        engine = pump.engine()
        await engine.tick(20, 10)
        await engine.command("heating_mode", "heat")
        pump.crash_after_write = "comfort_wheel"
        with self.assertRaises(asyncio.CancelledError):
            await engine.command("heating_preset", "vacation")
        restarted = pump.engine(pump.saved[-1])
        self.assertTrue(await restarted.recover())
        self.assertEqual(pump.values["comfort_wheel"], 23.34)
        self.assertEqual(restarted.state["heating_preset"], "normal")
        self.assertFalse(restarted.state["overrides"])

    async def test_excess_to_low_releases_only_its_sg_owner_and_preserves_boiler_timer(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, smart_grid_mode="sg_ready")
        await engine.tick(19, 10)
        await engine.command("heating_mode", "heat")
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        deadline = engine.state["hot_water_excess_deadline"]
        await engine.command("heating_preset", "low")
        self.assertEqual(engine.state["heating_preset"], "low")
        self.assertIsNone(engine.state["heating_excess_deadline"])
        self.assertEqual(engine.state["sg_owners"], ["hot_water"])
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)

    async def test_humidity_profile_limits_switch_with_preset_and_latched_resume_uses_selected_limit(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(25, 10, 70)
        await engine.command("heating_mode", "cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        normal = engine.cooling_humidity_info()
        self.assertEqual(normal["active_humidity_profile"], "normal")
        self.assertEqual(normal["humidity_limit"], 80)
        self.assertEqual(normal["active_humidity_limit"], 80)
        self.assertEqual(normal["normal_humidity_limit"], 80)
        self.assertEqual(normal["vacation_humidity_limit"], 65)
        self.assertEqual(normal["normal_resume_humidity_limit"], 78)
        self.assertEqual(normal["vacation_resume_humidity_limit"], 63)
        await engine.command("heating_preset", "vacation")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertTrue(engine.state["cooling_humidity_high"])
        vacation = engine.cooling_humidity_info()
        self.assertEqual(vacation["active_humidity_profile"], "vacation")
        self.assertEqual(vacation["humidity_limit"], 65)
        self.assertEqual(vacation["resume_humidity_limit"], 63)
        # A latched pause uses the newly selected limit, retaining its gap.
        await engine.tick(25, 10, 79)
        await engine.command("heating_preset", "normal")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["resume_humidity_limit"], 78)
        await engine.tick(25, 10, 78)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertFalse(engine.state["cooling_humidity_high"])
        self.assertGreater(pump.values["passive_cooling_supply_target"], 16)
        self.assertEqual(
            engine.state["overrides"]["cooling_humidity"]["passive_cooling_supply_target"], 16
        )

    async def test_humidity_vacation_pause_resumes_immediately_when_returning_to_normal_below_gap(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(25, 10, 70)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_preset", "vacation")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        await engine.command("heating_preset", "normal")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertFalse(engine.state["cooling_humidity_high"])
        self.assertEqual(engine.cooling_humidity_info()["humidity_limit"], 80)

    async def test_live_humidity_limit_changes_use_only_active_profile_and_do_not_rebase_originals(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(25, 10, 70)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_preset", "vacation")
        profile_original = deepcopy(engine.state["overrides"]["heating_profile"])
        native_original = engine.state["overrides"]["cooling_humidity"][
            "passive_cooling_supply_target"
        ]
        await engine.command("setting", ("cooling_humidity_limit", 90))
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["humidity_limit"], 65)
        self.assertEqual(engine.cooling_humidity_info()["normal_humidity_limit"], 90)
        await engine.command("setting", ("cooling_vacation_humidity_limit", 75))
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["humidity_limit"], 75)
        self.assertEqual(engine.cooling_humidity_info()["resume_humidity_limit"], 73)
        self.assertEqual(engine.state["heating_preset"], "vacation")
        self.assertEqual(engine.state["overrides"]["heating_profile"], profile_original)
        self.assertEqual(
            engine.state["overrides"]["cooling_humidity"]["passive_cooling_supply_target"],
            native_original,
        )
        self.assertEqual(pump.saved[-1]["settings"]["cooling_vacation_humidity_limit"], 75)
        await engine.command("heating_preset", "normal")
        await engine.command("setting", ("cooling_vacation_humidity_limit", 60))
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["humidity_limit"], 90)
        await engine.command("setting", ("cooling_humidity_limit", 70))
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["humidity_limit"], 70)
        await engine.command("setting", ("cooling_humidity_limit", 80))
        self.assertTrue(pump.values["passive_cooling_enabled"])

    async def test_changed_vacation_limit_keeps_two_point_resume_gap_after_high_pause(self):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(25, 10, 79)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_preset", "vacation")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        await engine.command("setting", ("cooling_vacation_humidity_limit", 80))
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["resume_humidity_limit"], 78)
        await engine.tick(25, 10, 78)
        self.assertTrue(pump.values["passive_cooling_enabled"])

    async def test_humidity_limits_reject_invalid_or_fractional_user_edits_before_writes(self):
        for key in ("cooling_humidity_limit", "cooling_vacation_humidity_limit"):
            for value in (29, 91, 65.5, True, None, float("nan")):
                with self.subTest(key=key, value=value):
                    pump = Pump()
                    engine = pump.engine()
                    original = deepcopy(engine.settings)
                    with self.assertRaises(ValueError):
                        await engine.command("setting", (key, value))
                    self.assertEqual(engine.settings, original)
                    self.assertFalse(pump.writes)
                    self.assertNotIn(key, engine.state["settings"])

    async def test_failed_live_vacation_limit_write_restores_preference_and_remains_safely_paused(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(25, 10, 70)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_preset", "vacation")
        original = deepcopy(engine.state["overrides"]["heating_profile"])
        pump.readback_failures["passive_cooling_enabled"] = 1
        with self.assertRaises(OSError):
            await engine.command("setting", ("cooling_vacation_humidity_limit", 75))
        self.assertEqual(engine.settings["cooling_vacation_humidity_limit"], 65)
        self.assertNotIn("cooling_vacation_humidity_limit", pump.saved[-1]["settings"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.state["heating_preset"], "vacation")
        self.assertEqual(engine.state["overrides"]["heating_profile"], original)
        await engine.tick(25, 10, 70)
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["humidity_limit"], 65)

    async def test_humidity_normal_default_disabled_but_vacation_selected_sensor_always_guards(
        self,
    ):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh")
        await engine.tick(25, 10, 70)
        await engine.command("heating_mode", "cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["status"], "disabled_in_normal")
        await engine.command("heating_preset", "vacation")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["status"], "high_humidity")
        self.assertTrue(engine.cooling_humidity_info()["protection_enabled"])
        await engine.command("heating_preset", "normal")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)

    async def test_humidity_pause_and_two_percent_resume_gap_raise_supply_durably(self):
        pump = Pump()
        engine = pump.engine(
            inside_humidity_sensor="sensor.rh",
            cooling_humidity_enabled=True,
            cooling_humidity_limit=65,
        )
        await engine.tick(28, 10, 65)
        await engine.command("heating_mode", "cool")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        await engine.tick(28, 10, 64)
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.cooling_humidity_info()["resume_humidity_limit"], 63)
        await engine.tick(28, 10, 63)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertGreater(pump.values["passive_cooling_supply_target"], 16)
        self.assertEqual(
            engine.state["overrides"]["cooling_humidity"]["passive_cooling_supply_target"], 16
        )
        for index, (key, value) in enumerate(pump.writes):
            if key == "passive_cooling_supply_target":
                self.assertEqual(pump.writes[index - 1], ("passive_cooling_enabled", False))
                self.assertIn("cooling_humidity", pump.persisted_before_write[index]["overrides"])

    async def test_humidity_toggle_off_restores_exact_supply_before_normal_cooling_on(self):
        pump = Pump()
        pump.values["passive_cooling_supply_target"] = 16.34
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(28, 10, 60)
        await engine.command("heating_mode", "cool")
        self.assertGreater(pump.values["passive_cooling_supply_target"], 16.34)
        pump.writes.clear()
        await engine.command("setting", ("cooling_humidity_enabled", False))
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16.34)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        target_index = pump.writes.index(("passive_cooling_supply_target", 16.34))
        self.assertEqual(pump.writes[target_index - 1], ("passive_cooling_enabled", False))
        self.assertGreater(pump.writes.index(("passive_cooling_enabled", True)), target_index)
        self.assertNotIn("cooling_humidity", engine.state["overrides"])

    async def test_humidity_missing_sensor_or_unachievable_dewpoint_pauses_raw_permission_without_heating_change(
        self,
    ):
        for inside, humidity, supply in (
            (25, None, 16),
            (None, 60, 16),
            (40, 64, 16),
            (25, 60, None),
        ):
            with self.subTest(inside=inside, humidity=humidity, supply=supply):
                pump = Pump()
                pump.values.update(heating_enabled=True, passive_cooling_supply_target=supply)
                engine = pump.engine(
                    inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True
                )
                engine.update_measurements(inside, 10, humidity)
                await engine.command("native_cooling_enabled", True)
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertTrue(pump.values["heating_enabled"])
                self.assertTrue(engine.cooling_humidity_info()["paused"])

    async def test_humidity_no_selected_sensor_leaves_vacation_cooling_native_floor(self):
        pump = Pump()
        engine = pump.engine(cooling_humidity_enabled=True)
        await engine.tick(25, 10, None)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_preset", "vacation")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
        self.assertEqual(engine.cooling_humidity_info()["status"], "no_sensor_selected")

    async def test_humidity_restart_restores_original_floor_with_cooling_off_when_rh_invalid(self):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(28, 10, 60)
        await engine.command("heating_mode", "cool")
        restarted = pump.engine(
            pump.saved[-1], inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True
        )
        restarted.update_measurements(28, 10, None)
        pump.writes.clear()
        self.assertTrue(await restarted.recover())
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertNotIn(("passive_cooling_enabled", True), pump.writes)

    async def test_humidity_lost_supply_readback_keeps_recoverable_original_and_cooling_off(self):
        pump = Pump()
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(28, 10, 60)
        pump.readback_failures["passive_cooling_supply_target"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "cool")
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(
            engine.state["overrides"]["cooling_humidity"]["passive_cooling_supply_target"], 16
        )
        self.assertTrue(await engine.recover())
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)

    async def test_humidity_temperature_floor_never_lowers_original_and_native_edit_rebases_deliberately(
        self,
    ):
        pump = Pump()
        pump.values["passive_cooling_supply_target"] = 24.34
        engine = pump.engine(inside_humidity_sensor="sensor.rh", cooling_humidity_enabled=True)
        await engine.tick(28, 10, 60)
        await engine.command("heating_mode", "cool")
        self.assertEqual(pump.values["passive_cooling_supply_target"], 24.34)
        await engine.command("native_setting", ("passive_cooling_supply_target", 16))
        self.assertGreater(pump.values["passive_cooling_supply_target"], 16)
        self.assertEqual(
            engine.state["overrides"]["cooling_humidity"]["passive_cooling_supply_target"], 16
        )
        await engine.command("setting", ("cooling_humidity_enabled", False))
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)

    async def test_water_low_timer_is_durable_before_profile_and_expires_at_default_24_hours(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.command("hot_water_mode", "evening")
        self.assertEqual(engine.state["hot_water_low_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_low_deadline"], 1000 + 24 * 3600)
        for saved in pump.persisted_before_write:
            self.assertEqual(saved["hot_water_low_deadline"], 1000 + 24 * 3600)
        clock.value = 1000 + 24 * 3600 - 1
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "evening")
        clock.value += 1
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertIsNone(engine.state["hot_water_low_started_at"])
        self.assertIsNone(engine.state["hot_water_low_deadline"])

    async def test_water_low_reselection_restarts_timer_but_profile_edits_and_polls_do_not(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.command("hot_water_mode", "evening")
        clock.advance_hours(2)
        await engine.tick(20, 10)
        await engine.command("setting", ("hot_water_evening_start", 36))
        await engine.command("setting", ("hot_water_evening_stop", 42))
        self.assertEqual(engine.state["hot_water_low_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_low_deadline"], 1000 + 24 * 3600)
        await engine.command("hot_water_mode", "evening")
        self.assertEqual(engine.state["hot_water_low_started_at"], clock.value)
        self.assertEqual(engine.state["hot_water_low_deadline"], clock.value + 24 * 3600)
        self.assertEqual(
            (engine.state["hot_water_start"], engine.state["hot_water_target"]), (45, 50)
        )

    async def test_water_low_duration_change_uses_original_start_and_expires_without_touching_heating(
        self,
    ):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "evening")
        heating_deadline = engine.state["heating_excess_deadline"]
        clock.advance_hours(2)
        await engine.command("setting", ("hot_water_low_hours", 3))
        self.assertEqual(engine.state["hot_water_low_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_low_deadline"], 1000 + 3 * 3600)
        await engine.command("setting", ("hot_water_low_hours", 1))
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual(engine.state["heating_preset"], "pv_charge")
        self.assertEqual(engine.state["heating_excess_deadline"], heating_deadline)
        self.assertTrue(pump.values["fixed_supply_enabled"])

    async def test_water_low_leaving_preset_or_editing_card_clears_clock(self):
        for action, value in (
            ("hot_water_mode", "auto"),
            ("hot_water_mode", "off"),
            ("hot_water_mode", "energy_excess"),
            ("hot_water_temperature_edit", {"target_temp_low": 37, "target_temp_high": 40}),
        ):
            with self.subTest(action=action, value=value):
                pump = Pump()
                engine = pump.engine(now=Clock())
                await engine.command("hot_water_mode", "evening")
                await engine.command(action, value)
                self.assertIsNone(engine.state["hot_water_low_started_at"])
                self.assertIsNone(engine.state["hot_water_low_deadline"])

    async def test_water_low_expiry_finishes_despite_independent_heating_target_error(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, hot_water_low_hours=0.1)
        await engine.tick(18, 10)
        await engine.command("heating_mode", "heat_cool")
        await engine.command("hot_water_mode", "evening")
        pump.values["comfort_wheel"] = 0
        clock.advance_hours(0.1)
        with self.assertRaises(ValueError):
            await engine.tick(18, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIsNone(engine.state["hot_water_low_deadline"])

    async def test_water_low_failed_expiry_keeps_expired_deadline_and_restores_on_retry(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, hot_water_low_hours=0.1)
        await engine.command("hot_water_mode", "evening")
        pump.failures["hot_water_start"] = 20
        clock.advance_hours(0.1)
        with self.assertRaises(OSError):
            await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_low_deadline"], clock.value)
        self.assertIn("hot_water", engine.state["pending_restore"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        pump.failures.clear()
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIsNone(engine.state["hot_water_low_deadline"])

    async def test_water_low_restart_and_shutdown_cancel_clock_and_restore_exact_native_originals(
        self,
    ):
        for restart in (False, True):
            with self.subTest(restart=restart):
                pump = Pump()
                pump.values.update(
                    hot_water_enabled=False,
                    hot_water_start=45.25,
                    hot_water_stop=55.5,
                    hot_water_boost=True,
                )
                engine = pump.engine(now=Clock())
                await engine.command("hot_water_mode", "evening")
                if restart:
                    engine = pump.engine(deepcopy(pump.saved[-1]), now=Clock())
                self.assertTrue(await engine.recover())
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45.25, 55.5)
                )
                self.assertFalse(pump.values["hot_water_enabled"])
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertIsNone(engine.state["hot_water_low_deadline"])
                self.assertEqual(engine.state["hot_water_mode"], "off")

    async def test_failed_water_low_timer_save_leaves_existing_clock_and_native_profile_unchanged(
        self,
    ):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.command("hot_water_mode", "evening")
        original_clock = (
            engine.state["hot_water_low_started_at"],
            engine.state["hot_water_low_deadline"],
        )
        writes = len(pump.writes)
        clock.advance_hours(1)
        pump.fail_persist = True
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "evening")
        self.assertEqual(
            (engine.state["hot_water_low_started_at"], engine.state["hot_water_low_deadline"]),
            original_clock,
        )
        self.assertEqual(len(pump.writes), writes)
        self.assertEqual(engine.state["hot_water_mode"], "evening")

    async def test_heating_card_edit_cancels_excess_and_applies_displayed_heat_mode(self):
        for previous_mode in ("off", "heat"):
            with self.subTest(previous_mode=previous_mode):
                pump = Pump()
                clock = Clock()
                engine = pump.engine(now=clock, smart_grid_mode="sg_ready")
                await engine.tick(21, 10)
                await engine.command("heating_mode", previous_mode)
                await engine.command("heating_preset", "pv_charge")
                await engine.command("heating_temperature_edit", {"temperature": 22})
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(engine.state["heating_mode"], "heat")
                self.assertEqual(engine.state["heating_target"], 22)
                self.assertEqual(pump.values["comfort_wheel"], 22)
                self.assertTrue(pump.values["heating_enabled"])
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertEqual(pump.values["fixed_supply_target"], 30)
                self.assertEqual(pump.values["smart_grid_request"], 0)
                self.assertIsNone(engine.state["heating_excess_deadline"])
                self.assertEqual(engine.state["overrides"], {})

    async def test_heating_card_range_or_scalar_edit_cancels_auto_excess(self):
        for payload in (
            {"target_temp_low": 22, "target_temp_high": 25},
            {"temperature": 22},
        ):
            with self.subTest(payload=payload):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(18, 10)
                await engine.command("heating_mode", "heat_cool")
                await engine.command("heating_preset", "pv_charge")
                await engine.command("heating_temperature_edit", payload)
                self.assertEqual(engine.state["heating_mode"], "heat_cool")
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(engine.state["heating_low"], 22)
                self.assertEqual(
                    engine.state["heating_high"], 25 if "target_temp_high" in payload else 24
                )
                self.assertEqual(pump.values["comfort_wheel"], 22)
                self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_heating_card_normal_cool_and_off_edits_do_not_write_native_dial(self):
        for mode in ("cool", "off"):
            with self.subTest(mode=mode):
                pump = Pump()
                pump.values["comfort_wheel"] = None
                engine = pump.engine()
                await engine.command("heating_mode", mode)
                pump.writes.clear()
                await engine.command("heating_temperature_edit", {"temperature": 22})
                self.assertEqual(engine.state["heating_mode"], mode)
                self.assertEqual(engine.state["heating_target"], 22)
                self.assertFalse(any(key == "comfort_wheel" for key, _ in pump.writes))

    async def test_heating_card_bundled_cool_restores_excess_dial_without_copying_cooling_target(
        self,
    ):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        pump.values["comfort_wheel"] = None
        pump.writes.clear()
        await engine.command("heating_temperature_edit", {"temperature": 25, "hvac_mode": "cool"})
        self.assertEqual(engine.state["heating_mode"], "cool")
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.values["comfort_wheel"], 20)
        self.assertNotIn(("comfort_wheel", 25), pump.writes)

    async def test_heating_card_unchanged_displayed_temperature_preserves_excess_deadline(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        original = deepcopy(engine.state)
        writes = len(pump.writes)
        clock.advance_hours(1)
        await engine.command("heating_temperature_edit", {"temperature": 23})
        self.assertEqual(engine.state, original)
        self.assertEqual(len(pump.writes), writes)
        await engine.command("heating_temperature_edit", {"temperature": 23, "hvac_mode": "cool"})
        self.assertEqual(engine.state["heating_mode"], "cool")
        self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_heating_card_explicit_mode_ends_excess_even_with_unchanged_temperature(self):
        for mode in ("heat", "heat_cool"):
            with self.subTest(mode=mode):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(21, 10)
                await engine.command("heating_mode", mode)
                await engine.command("heating_preset", "pv_charge")
                payload = (
                    {"temperature": 23}
                    if mode == "heat"
                    else {"target_temp_low": 20, "target_temp_high": 24}
                )
                await engine.command("heating_temperature_edit", payload | {"hvac_mode": mode})
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(engine.state["heating_mode"], mode)
                self.assertEqual(pump.values["comfort_wheel"], 23 if mode == "heat" else 20)
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_heating_card_invalid_edit_does_not_cancel_excess_or_write(self):
        for payload in (
            {"temperature": 22.5},
            {"temperature": 36},
            {"target_temp_low": 26, "target_temp_high": 24},
            {"temperature": 22, "hvac_mode": "unsupported"},
        ):
            with self.subTest(payload=payload):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                deadline = engine.state["heating_excess_deadline"]
                writes = len(pump.writes)
                with self.assertRaises(ValueError):
                    await engine.command("heating_temperature_edit", payload)
                self.assertEqual(len(pump.writes), writes)
                self.assertEqual(engine.state["heating_preset"], "pv_charge")
                self.assertEqual(engine.state["heating_excess_deadline"], deadline)

    async def test_boiler_card_edit_reconstructs_untouched_normal_side_from_both_handles(self):
        for preset, payload, expected in (
            ("energy_excess", {"target_temp_low": 57, "target_temp_high": 58}, (45, 58)),
            ("energy_excess", {"target_temp_low": 47, "target_temp_high": 60}, (47, 50)),
            ("energy_excess", {"target_temp_low": 57, "target_temp_high": 57}, (45, 57)),
            ("evening", {"target_temp_low": 35, "target_temp_high": 42}, (37, 42)),
            ("evening", {"target_temp_low": 37, "target_temp_high": 40}, (37, 50)),
        ):
            with self.subTest(preset=preset, payload=payload):
                pump = Pump()
                engine = pump.engine(hot_water_hysteresis=3)
                await engine.command("hot_water_mode", preset)
                writes = len(pump.writes)
                if expected[0] >= expected[1]:
                    with self.assertRaises(ValueError):
                        await engine.command("hot_water_temperature_edit", payload)
                    self.assertEqual(engine.state["hot_water_mode"], preset)
                    self.assertEqual(len(pump.writes), writes)
                    continue
                await engine.command("hot_water_temperature_edit", payload)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), expected
                )
                self.assertEqual(
                    (engine.state["hot_water_start"], engine.state["hot_water_target"]), expected
                )
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertTrue(pump.values["hot_water_enabled"])
                self.assertIsNone(engine.state["hot_water_excess_deadline"])
                self.assertEqual(engine.state["overrides"], {})

    async def test_boiler_card_edit_can_lower_low_mode_stop_below_saved_start_if_both_changed(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "evening")
        await engine.command(
            "hot_water_temperature_edit", {"target_temp_low": 32, "target_temp_high": 38}
        )
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (32, 38))
        self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_boiler_card_one_sided_edit_and_front_panel_normal_originals_are_preserved(self):
        pump = Pump()
        engine = pump.engine()
        pump.values.update(hot_water_start=48.5, hot_water_stop=55)
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("hot_water_temperature_edit", {"target_temp_high": 58})
        self.assertEqual(
            (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (48.5, 58)
        )
        pump.values["hot_water_start"] = 49.5
        await engine.command("hot_water_temperature_edit", {"temperature": 59})
        self.assertEqual(
            (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (49.5, 59)
        )

    async def test_boiler_card_unchanged_one_or_two_sides_do_not_cancel_presets(self):
        for preset in ("energy_excess", "evening"):
            for one_sided in (False, True):
                with self.subTest(preset=preset, one_sided=one_sided):
                    pump = Pump()
                    engine = pump.engine()
                    await engine.command("hot_water_mode", preset)
                    payload = {"target_temp_high": pump.values["hot_water_stop"]}
                    if not one_sided:
                        payload["target_temp_low"] = pump.values["hot_water_start"]
                    old = deepcopy(engine.state)
                    writes = len(pump.writes)
                    await engine.command("hot_water_temperature_edit", payload)
                    self.assertEqual(engine.state, old)
                    self.assertEqual(len(pump.writes), writes)

    async def test_boiler_card_explicit_auto_restores_normal_even_with_unchanged_handles(self):
        for preset in ("energy_excess", "evening"):
            with self.subTest(preset=preset):
                pump = Pump()
                engine = pump.engine()
                await engine.command("hot_water_mode", preset)
                await engine.command(
                    "hot_water_temperature_edit",
                    {
                        "target_temp_low": pump.values["hot_water_start"],
                        "target_temp_high": pump.values["hot_water_stop"],
                        "hvac_mode": "auto",
                    },
                )
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertTrue(pump.values["hot_water_enabled"])
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertIsNone(engine.state["hot_water_excess_deadline"])

    async def test_boiler_card_normal_off_edit_and_direct_boost_flag_are_preserved(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                pump = Pump()
                pump.values.update(hot_water_enabled=enabled, hot_water_boost=True)
                engine = pump.engine()
                await engine.command(
                    "hot_water_temperature_edit", {"target_temp_low": 46, "target_temp_high": 55}
                )
                self.assertEqual(pump.values["hot_water_enabled"], enabled)
                self.assertTrue(pump.values["hot_water_boost"])
                self.assertFalse(any(key == "hot_water_boost" for key, _ in pump.writes))
                self.assertEqual(engine.state["hot_water_mode"], "auto" if enabled else "off")

    async def test_boiler_card_bundled_off_cancels_excess_even_for_identical_handles(self):
        pump = Pump()
        engine = pump.engine(hot_water_hysteresis=3)
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command(
            "hot_water_temperature_edit",
            {"target_temp_low": 57, "target_temp_high": 60, "hvac_mode": "off"},
        )
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_boiler_card_invalid_reconstructed_range_has_no_write_or_preset_cancellation(
        self,
    ):
        for payload in (
            {"target_temp_low": 61, "target_temp_high": 60},
            {"target_temp_low": 29, "target_temp_high": 44},
            {"target_temp_low": 46.5, "target_temp_high": 58},
            {"temperature": 61},
        ):
            with self.subTest(payload=payload):
                pump = Pump()
                engine = pump.engine()
                await engine.command("hot_water_mode", "energy_excess")
                deadline = engine.state["hot_water_excess_deadline"]
                writes = len(pump.writes)
                with self.assertRaises(ValueError):
                    await engine.command("hot_water_temperature_edit", payload)
                self.assertEqual(len(pump.writes), writes)
                self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
                self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)

    async def test_boiler_card_clamps_requested_normal_start_before_exiting_preset(self):
        pump = Pump()
        pump.values.update(hot_water_start=55, hot_water_stop=60)
        engine = pump.engine(max_start_temperature=58, max_hot_water_temperature=60)
        await engine.command("hot_water_mode", "energy_excess")
        pump.writes.clear()
        await engine.command(
            "hot_water_temperature_edit", {"target_temp_low": 59, "target_temp_high": 60}
        )
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_card_edit_new_policy_is_durable_before_each_first_physical_write(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        offset = len(pump.writes)
        await engine.command("heating_temperature_edit", {"temperature": 22})
        saved = pump.persisted_before_write[offset]
        self.assertEqual(saved["heating_preset"], "normal")
        self.assertIsNone(saved["heating_excess_deadline"])
        self.assertIn("heating_target_edit", saved["overrides"])
        offset = len(pump.writes)
        await engine.command("hot_water_temperature_edit", {"target_temp_high": 58})
        saved = pump.persisted_before_write[offset]
        self.assertEqual(saved["hot_water_mode"], "auto")
        self.assertIsNone(saved["hot_water_excess_deadline"])
        self.assertEqual(saved["overrides"]["water_temperature_edit"]["hot_water_start"], 45)

    async def test_boiler_card_lost_readback_restores_old_normal_pair_without_resuming_excess(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        original_write = pump.write

        async def write(key, value):
            if key == "hot_water_stop" and value == 58:
                pump.readback_failures[key] = 1
            await original_write(key, value)

        engine.write = write
        with self.assertRaises(OSError):
            await engine.command("hot_water_temperature_edit", {"target_temp_high": 58})
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(
            (engine.state["hot_water_start"], engine.state["hot_water_target"]), (45, 50)
        )
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        await engine.tick(21, 10)
        self.assertEqual(pump.values["hot_water_stop"], 50)

    async def test_boiler_card_late_commit_failure_restores_pair_and_saved_policy(self):
        for cancellation in (False, True):
            with self.subTest(cancellation=cancellation):
                pump = Pump()
                failed = False

                async def persist(state):
                    nonlocal failed
                    if (
                        not failed
                        and state["hot_water_target"] == 58
                        and "water_temperature_edit" not in state["overrides"]
                    ):
                        failed = True
                        if cancellation:
                            raise asyncio.CancelledError("Interrupted temperature commit")
                        raise OSError("Failed temperature commit")
                    await pump.persist(state)

                engine = ControlEngine(pump.read, pump.write, persist, pump.settings)
                await engine.command("hot_water_mode", "energy_excess")
                with self.assertRaises(asyncio.CancelledError if cancellation else OSError):
                    await engine.command("hot_water_temperature_edit", {"target_temp_high": 58})
                self.assertTrue(failed)
                if cancellation:
                    self.assertIn("water_temperature_edit", engine.state["overrides"])
                    self.assertTrue(await engine.shutdown())
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertEqual(pump.saved[-1]["hot_water_target"], 50)
                self.assertFalse(pump.values["hot_water_boost"])

    async def test_heating_card_failed_new_dial_write_returns_original_normal_and_removes_override(
        self,
    ):
        pump = Pump()
        pump.values.update(heating_enabled=True, comfort_wheel=21)
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        pump.readback_failures["comfort_wheel"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_temperature_edit", {"temperature": 22})
        self.assertEqual(pump.values["comfort_wheel"], 21)
        self.assertEqual(engine.state["heating_target"], 21)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_card_edit_commit_failure_before_preset_restore_still_cancels_charge(self):
        for group, action, payload in (
            ("heating", "heating_temperature_edit", {"temperature": 22}),
            ("hot_water", "hot_water_temperature_edit", {"target_temp_high": 58}),
        ):
            with self.subTest(group=group):
                pump = Pump()
                engine = pump.engine(smart_grid_mode="sg_ready")
                await engine.tick(21, 10)
                await engine.command(
                    "heating_preset", "pv_charge"
                ) if group == "heating" else await engine.command("hot_water_mode", "energy_excess")
                failed = False

                async def persist(state):
                    nonlocal failed
                    journal = (
                        "heating_target_edit" if group == "heating" else "water_temperature_edit"
                    )
                    target = (
                        state["heating_target"] if group == "heating" else state["hot_water_target"]
                    )
                    if (
                        not failed
                        and journal in state["overrides"]
                        and target == (22 if group == "heating" else 58)
                    ):
                        failed = True
                        raise OSError("Failed cancellation-intent save")
                    await pump.persist(state)

                engine.persist = persist
                with self.assertRaises(OSError):
                    await engine.command(action, payload)
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertEqual(pump.values["smart_grid_request"], 0)
                self.assertIsNone(engine.state[f"{group}_excess_deadline"])

    async def test_card_edit_crash_recovery_returns_original_normal_and_never_recharges(self):
        for group, action, payload, crash_key in (
            ("heating", "heating_temperature_edit", {"temperature": 22}, "comfort_wheel"),
            ("hot_water", "hot_water_temperature_edit", {"target_temp_high": 58}, "hot_water_stop"),
        ):
            with self.subTest(group=group):
                pump = Pump()
                engine = pump.engine(smart_grid_mode="sg_ready")
                await engine.tick(21, 10)
                if group == "heating":
                    await engine.command("heating_preset", "pv_charge")
                else:
                    await engine.command("hot_water_mode", "energy_excess")
                original_write = pump.write

                async def write(key, value):
                    if key == crash_key and value == (22 if group == "heating" else 58):
                        pump.crash_after_write = key
                    await original_write(key, value)

                engine.write = write
                with self.assertRaises(asyncio.CancelledError):
                    await engine.command(action, payload)
                restarted = pump.engine(deepcopy(pump.saved[-1]), smart_grid_mode="sg_ready")
                self.assertTrue(await restarted.recover())
                self.assertEqual(pump.values["comfort_wheel"], 20)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertEqual(pump.values["smart_grid_request"], 0)
                writes = len(pump.writes)
                await restarted.tick(21, 10)
                self.assertEqual(len(pump.writes), writes)

    async def test_boiler_card_exit_with_other_sg_owner_does_not_enable_above_new_stop(self):
        for preset in ("energy_excess", "evening"):
            with self.subTest(preset=preset):
                pump = Pump()
                pump.values.update(hot_water_top_temperature=55, hot_water_weighted_temperature=54)
                engine = pump.engine(smart_grid_mode="sg_ready")
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                await engine.command("hot_water_mode", preset)
                pump.writes.clear()
                await engine.command("hot_water_temperature_edit", {"temperature": 52})
                self.assertEqual(engine.state["sg_owners"], ["heating"])
                self.assertEqual(pump.values["smart_grid_request"], 3)
                self.assertFalse(pump.values["hot_water_enabled"])
                self.assertNotIn(("hot_water_enabled", True), pump.writes)
                self.assertTrue(engine.state["overrides"]["water_guard"]["hot_water_enabled"])
                await engine.command("heating_preset", "normal")
                self.assertTrue(pump.values["hot_water_enabled"])
                self.assertEqual(pump.values["hot_water_stop"], 52)

    async def test_heating_card_exit_retains_other_water_sg_owner_and_deadline(self):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        deadline = engine.state["hot_water_excess_deadline"]
        await engine.command("heating_temperature_edit", {"temperature": 22})
        self.assertEqual(engine.state["sg_owners"], ["hot_water"])
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["comfort_wheel"], 22)

    async def test_low_level_normal_target_and_charge_number_edits_keep_excess_active(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        deadlines = (
            engine.state["heating_excess_deadline"],
            engine.state["hot_water_excess_deadline"],
        )
        await engine.command("setting", ("max_heating_temperature", 24))
        await engine.command("heating_target", 22)
        await engine.command("hot_water_target", 58)
        self.assertEqual(engine.state["heating_preset"], "pv_charge")
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual(
            (engine.state["heating_excess_deadline"], engine.state["hot_water_excess_deadline"]),
            deadlines,
        )

    async def test_failed_normal_card_edit_restores_front_panel_permission_and_prior_ha_policy(
        self,
    ):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "auto")
        pump.values.update(hot_water_enabled=False, hot_water_boost=True)
        previous = deepcopy(engine.state)
        pump.readback_failures["hot_water_start"] = 1
        with self.assertRaises(OSError):
            await engine.command(
                "hot_water_temperature_edit", {"target_temp_low": 44, "target_temp_high": 50}
            )
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        for key in ("hot_water_start", "hot_water_target", "hot_water_mode", "managed_hot_water"):
            self.assertEqual(engine.state[key], previous[key])

    async def test_boiler_card_shared_sg_checks_every_intermediate_physical_permission(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=58)
        engine = pump.engine(smart_grid_mode="sg_ready", hot_water_hysteresis=3)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        original_write = pump.write

        async def write(key, value):
            candidate = {**pump.values, key: value}
            if candidate["smart_grid_request"] == 3:
                self.assertFalse(candidate["hot_water_enabled"], (key, value))
            await original_write(key, value)

        engine.write = write
        await engine.command(
            "hot_water_temperature_edit", {"target_temp_low": 57, "target_temp_high": 58}
        )
        self.assertEqual(engine.state["sg_owners"], ["heating"])
        self.assertTrue(engine.state["overrides"]["water_guard"]["hot_water_enabled"])
        await engine.command("heating_preset", "normal")
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertTrue(pump.values["hot_water_enabled"])

    async def test_crashed_card_edit_keeps_water_off_until_physical_sg_restoration_completes(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=58)
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        original_write = pump.write

        async def write(key, value):
            if key == "hot_water_stop" and value == 58:
                pump.crash_after_write = key
            await original_write(key, value)

        engine.write = write
        with self.assertRaises(asyncio.CancelledError):
            await engine.command("hot_water_temperature_edit", {"target_temp_high": 58})
        pump.failures["smart_grid_request"] = 20
        restored = pump.engine(deepcopy(pump.saved[-1]), smart_grid_mode="sg_ready")

        async def safe_write(key, value):
            candidate = {**pump.values, key: value}
            if candidate["smart_grid_request"] == 3:
                self.assertFalse(candidate["hot_water_enabled"], (key, value))
            await original_write(key, value)

        restored.write = safe_write
        self.assertFalse(await restored.recover())
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertIn("water_temperature_edit", restored.state["pending_restore"])
        self.assertIn("water_guard", restored.state["pending_restore"])
        pump.failures.clear()
        self.assertTrue(await restored.recover())
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(restored.state["hot_water_mode"], "auto")

    async def test_pending_card_preset_exit_restarts_in_normal_after_threshold_retry(self):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.command("hot_water_mode", "energy_excess")
        pump.failures["hot_water_start"] = 20
        with self.assertRaises(OSError):
            await engine.command("hot_water_temperature_edit", {"target_temp_high": 58})
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertIn("water_temperature_edit", engine.state["pending_restore"])
        pump.failures.clear()
        restored = pump.engine(deepcopy(pump.saved[-1]), smart_grid_mode="sg_ready")
        self.assertTrue(await restored.recover())
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(restored.state["hot_water_mode"], "auto")
        self.assertIsNone(restored.state["hot_water_excess_deadline"])
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_heating_card_late_commit_failure_cancels_excess_and_restores_old_normal_target(
        self,
    ):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        failed = False

        async def persist(state):
            nonlocal failed
            if (
                not failed
                and state["heating_target"] == 22
                and "heating_target_edit" not in state["overrides"]
            ):
                failed = True
                raise OSError("Failed final card edit commit")
            await pump.persist(state)

        engine.persist = persist
        with self.assertRaises(OSError):
            await engine.command("heating_temperature_edit", {"temperature": 22})
        self.assertTrue(failed)
        self.assertEqual(pump.values["comfort_wheel"], 20)
        self.assertEqual(engine.state["heating_target"], 20)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_excess_deadlines_are_durable_before_first_override_writes(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.state["heating_excess_started_at"], 1000)
        self.assertEqual(engine.state["heating_excess_deadline"], 1000 + 12 * 3600)
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_excess_deadline"], 1000 + 6 * 3600)
        for (key, value), saved in zip(pump.writes, pump.persisted_before_write):
            if key == "fixed_supply_enabled" and value:
                self.assertEqual(saved["heating_excess_started_at"], 1000)
                self.assertEqual(saved["heating_excess_deadline"], 1000 + 12 * 3600)
            if key == "hot_water_stop" and value == 60:
                self.assertEqual(saved["hot_water_excess_started_at"], 1000)
                self.assertEqual(saved["hot_water_excess_deadline"], 1000 + 6 * 3600)

    async def test_default_excess_durations_expire_boiler_and_heating_independently(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        clock.advance_hours(6)
        await engine.tick(21, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        self.assertEqual(engine.state["heating_preset"], "pv_charge")
        self.assertTrue(pump.values["fixed_supply_enabled"])
        self.assertEqual(engine.state["heating_excess_deadline"], 1000 + 12 * 3600)
        clock.advance_hours(6)
        await engine.tick(21, 10)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["heating_mode"], "off")
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(pump.values["heating_enabled"])
        self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_heating_excess_expiry_restores_prior_normal_mode_and_targets(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, heating_excess_hours=0.1)
        await engine.tick(22, 10)
        await engine.command("heating_target", 22)
        await engine.command("heating_range", (21, 25))
        await engine.command("heating_mode", "heat_cool")
        await engine.command("heating_preset", "pv_charge")
        clock.advance_hours(0.1)
        await engine.tick(22, 10)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["heating_mode"], "heat_cool")
        self.assertEqual(engine.state["heating_target"], 22)
        self.assertEqual((engine.state["heating_low"], engine.state["heating_high"]), (21, 25))
        self.assertTrue(engine.state["managed_heating"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])

    async def test_heating_explicit_reselection_restarts_only_its_clock_and_preserves_originals(
        self,
    ):
        pump = Pump()
        pump.values.update(passive_cooling_enabled=True, heating_enabled=True)
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        original = deepcopy(engine.state["overrides"]["heating"])
        clock.advance_hours(1)
        await engine.command("heating_preset", "pv_charge")
        self.assertEqual(engine.state["heating_excess_started_at"], 4600)
        self.assertEqual(engine.state["heating_excess_deadline"], 4600 + 12 * 3600)
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)
        self.assertEqual(engine.state["overrides"]["heating"], original)
        self.assertEqual(engine.state["heating_excess_previous_mode"], "heat_cool")
        clock.advance_hours(12)
        await engine.tick(21, 10)
        self.assertEqual(engine.state["heating_mode"], "heat_cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])

    async def test_water_explicit_reselection_restarts_only_its_clock_and_preserves_originals(self):
        pump = Pump()
        pump.values["hot_water_enabled"] = False
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        original = deepcopy(engine.state["overrides"]["hot_water"])
        clock.advance_hours(1)
        pump.writes.clear()
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["hot_water_excess_started_at"], 4600)
        self.assertEqual(engine.state["hot_water_excess_deadline"], 4600 + 6 * 3600)
        self.assertEqual(engine.state["heating_excess_started_at"], 1000)
        self.assertEqual(engine.state["overrides"]["hot_water"], original)
        self.assertEqual(engine.state["boost_previous_mode"], "off")

    async def test_normal_then_excess_starts_new_timer_for_each_function(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("heating_preset", "normal")
        await engine.command("hot_water_mode", "auto")
        self.assertIsNone(engine.state["heating_excess_started_at"])
        self.assertIsNone(engine.state["hot_water_excess_started_at"])
        clock.advance_hours(1)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.state["heating_excess_started_at"], 4600)
        self.assertEqual(engine.state["hot_water_excess_started_at"], 4600)

    async def test_polls_ceiling_pause_and_resume_never_restart_excess_clocks(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        heating_original = deepcopy(engine.state["overrides"]["heating"])
        clock.advance_hours(1)
        pump.values["hot_water_top_temperature"] = 60
        await engine.tick(23, 10)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["fixed_supply_target"], 30)
        self.assertEqual(engine.state["overrides"]["heating"], heating_original)
        clock.advance_hours(1)
        pump.values["hot_water_top_temperature"] = 57
        await engine.tick(22.5, 10)
        self.assertTrue(pump.values["fixed_supply_enabled"])
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["overrides"]["heating"], heating_original)
        self.assertEqual(engine.state["heating_excess_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)

    async def test_paused_heating_expiry_restores_original_native_permission(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        clock = Clock()
        engine = pump.engine(now=clock, heating_excess_hours=0.1)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.tick(23, 10)
        self.assertFalse(pump.values["heating_enabled"])
        clock.advance_hours(0.1)
        await engine.tick(23, 10)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertEqual(engine.state["heating_mode"], "heat")
        self.assertFalse(engine.state["managed_heating"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_duration_changes_use_original_start_and_expire_shortened_water_duration(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        clock.advance_hours(2)
        await engine.command("setting", ("heating_excess_hours", 4))
        self.assertEqual(engine.state["heating_excess_started_at"], 1000)
        self.assertEqual(engine.state["heating_excess_deadline"], 1000 + 4 * 3600)
        await engine.command("setting", ("hot_water_excess_hours", 1))
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        self.assertEqual(engine.state["heating_preset"], "pv_charge")

    async def test_options_duration_changes_are_observed_by_next_poll_without_resetting_start(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        clock.advance_hours(1)
        engine.settings["heating_excess_hours"] = 2
        await engine.tick(21, 10)
        self.assertEqual(engine.state["heating_excess_started_at"], 1000)
        self.assertEqual(engine.state["heating_excess_deadline"], 1000 + 2 * 3600)
        engine.settings["heating_excess_hours"] = 0.1
        await engine.tick(21, 10)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")

    async def test_invalid_excess_durations_reject_entry_without_hardware_writes(self):
        for group, action, selected in (
            ("heating", "heating_preset", "pv_charge"),
            ("hot_water", "hot_water_mode", "energy_excess"),
        ):
            for hours in (0, -1, 169, True, float("nan"), float("inf")):
                with self.subTest(group=group, hours=hours):
                    pump = Pump()
                    engine = pump.engine(now=Clock(), **{f"{group}_excess_hours": hours})
                    engine.update_measurements(21, 10)
                    with self.assertRaises(ValueError):
                        await engine.command(action, selected)
                    self.assertEqual(pump.writes, [])
                    self.assertIsNone(engine.state[f"{group}_excess_started_at"])

    async def test_failed_heating_expiry_keeps_expired_journal_and_never_recharges(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, heating_excess_hours=0.1)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        deadline = engine.state["heating_excess_deadline"]
        clock.advance_hours(0.1)
        pump.failures["fixed_supply_target"] = 10
        pump.writes.clear()
        for _ in range(2):
            with self.assertRaises(OSError):
                await engine.tick(21, 10)
            self.assertEqual(engine.state["heating_excess_deadline"], deadline)
            self.assertTrue(engine.state["overrides"]["heating"])
        self.assertNotIn(("fixed_supply_enabled", True), pump.writes)
        with self.assertRaises(RuntimeError):
            await engine.command("heating_preset", "pv_charge")
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)
        pump.failures.clear()
        await engine.tick(21, 10)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_failed_water_expiry_keeps_expired_journal_and_never_reasserts_boost(self):
        pump = Pump()
        pump.values["hot_water_boost"] = True
        clock = Clock()
        engine = pump.engine(now=clock, hot_water_excess_hours=0.1)
        await engine.command("hot_water_mode", "energy_excess")
        deadline = engine.state["hot_water_excess_deadline"]
        clock.advance_hours(0.1)
        pump.failures["hot_water_start"] = 10
        pump.writes.clear()
        for _ in range(2):
            with self.assertRaises(OSError):
                await engine.tick(21, 10)
            self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
            self.assertFalse(engine.state["overrides"]["hot_water"]["hot_water_boost"])
        self.assertNotIn(("hot_water_boost", True), pump.writes)
        pump.failures.clear()
        await engine.tick(21, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertIsNone(engine.state["hot_water_excess_deadline"])

    async def test_water_expiry_still_runs_while_an_independent_heating_restore_is_pending(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, heating_excess_hours=0.1, hot_water_excess_hours=0.2)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        pump.failures["fixed_supply_target"] = 10
        clock.advance_hours(0.1)
        with self.assertRaises(OSError):
            await engine.tick(21, 10)
        self.assertTrue(engine.state["pending_restore"])
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        clock.advance_hours(0.1)
        with self.assertRaises(OSError):
            await engine.tick(21, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        self.assertTrue(engine.state["overrides"]["heating"])

    async def test_restart_restores_stale_excess_and_clears_both_durable_clocks(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        restarted = pump.engine(deepcopy(pump.saved[-1]), now=clock)
        self.assertTrue(await restarted.recover())
        for group in ("heating", "hot_water"):
            self.assertIsNone(restarted.state[f"{group}_excess_started_at"])
            self.assertIsNone(restarted.state[f"{group}_excess_deadline"])
        self.assertEqual(restarted.state["heating_preset"], "normal")
        self.assertEqual(restarted.state["hot_water_mode"], "auto")
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_direct_native_boost_and_curve_changes_do_not_restart_excess_clocks(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        clock.advance_hours(1)
        await engine.command("native_hot_water_boost", True)
        await engine.command("native_setting", ("heat_curve_supply_1", 25))
        self.assertEqual(engine.state["heating_excess_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)
        await engine.command("native_setting", ("max_supply_temperature", 35))
        self.assertIsNone(engine.state["heating_excess_deadline"])
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)

    async def test_unix_epoch_start_is_valid_and_expires_at_exact_deadline(self):
        pump = Pump()
        clock = Clock(0)
        engine = pump.engine(now=clock, hot_water_excess_hours=0.1)
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.state["hot_water_excess_started_at"], 0)
        self.assertEqual(engine.state["hot_water_excess_deadline"], 360)
        clock.value = 359.9
        await engine.tick(21, 10)
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        clock.value = 360
        await engine.tick(21, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_native_setting_writes_exact_register_without_changing_thermostats(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("native_setting", ("heating_season_stop", 18))
        self.assertEqual(pump.writes, [("heating_season_stop", 18)])
        self.assertEqual(engine.state["overrides"], {})
        self.assertFalse(engine.state["managed_heating"])
        self.assertFalse(engine.state["managed_hot_water"])
        await engine.tick(19, 10)
        self.assertEqual(pump.writes, [("heating_season_stop", 18)])

    async def test_native_metadata_has_only_verified_addresses_and_explicit_software_limits(self):
        self.assertEqual(len(NATIVE_SETTINGS), 11)
        self.assertEqual(NATIVE_SETTINGS["heating_season_stop"].address, 16)
        self.assertEqual(NATIVE_SETTINGS["min_supply_temperature"].address, 4)
        self.assertEqual(NATIVE_SETTINGS["max_supply_temperature"].address, 3)
        self.assertEqual(NATIVE_SETTINGS["passive_cooling_supply_target"].address, 302)
        self.assertIn("Mixing Valve 1", NATIVE_SETTINGS["passive_cooling_supply_target"].label)
        for i in range(7):
            self.assertEqual(NATIVE_SETTINGS[f"heat_curve_supply_{i + 1}"].address, 6 + i)
        self.assertNotIn("selected_heat_curve", NATIVE_SETTINGS)
        self.assertNotIn("comfort_wheel", NATIVE_SETTINGS)

    async def test_native_controls_reject_outside_bounds_nonfinite_or_fractional_requests(self):
        for key, value in (
            ("heating_season_stop", -11),
            ("heating_season_stop", 41),
            ("min_supply_temperature", 4),
            ("max_supply_temperature", 66),
            ("passive_cooling_supply_target", 31),
            ("passive_cooling_supply_target", 4),
            ("heat_curve_supply_7", 66),
            ("heat_curve_supply_1", 4),
            ("heating_season_stop", 17.5),
            ("heating_season_stop", float("nan")),
            ("heating_season_stop", float("inf")),
            ("heating_season_stop", True),
            ("heating_season_stop", "18"),
            ("selected_heat_curve", 28),
            ("comfort_wheel", 21),
        ):
            with self.subTest(key=key, value=value):
                pump = Pump()
                engine = pump.engine()
                with self.assertRaises(ValueError):
                    await engine.command("native_setting", (key, value))
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["overrides"], {})

    async def test_native_settings_validate_entire_batch_and_readbacks_before_any_write(self):
        cases = (
            ({"heating_season_stop": 18, "passive_cooling_supply_target": 31}, {}),
            ({"heating_season_stop": 18}, {"heating_season_stop": None}),
            ({"heating_season_stop": 18}, {"heating_season_stop": float("nan")}),
            ({"heating_season_stop": 18}, {"heating_season_stop": True}),
            ({"heating_season_stop": 18}, {"heating_season_stop": 200}),
            ({"max_supply_temperature": 35}, {"min_supply_temperature": None}),
            ({"min_supply_temperature": 20}, {"max_supply_temperature": 70}),
            ({"min_supply_temperature": 45}, {}),
            ({"min_supply_temperature": 25, "max_supply_temperature": 24}, {}),
        )
        for requested, readbacks in cases:
            with self.subTest(requested=requested, readbacks=readbacks):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                pump.values.update(readbacks)
                pump.writes.clear()
                with self.assertRaises(ValueError):
                    await engine.command("native_settings", requested)
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["heating_preset"], "pv_charge")
                self.assertTrue(pump.values["fixed_supply_enabled"])

    async def test_native_non_supply_edit_does_not_require_supply_limits(self):
        pump = Pump()
        pump.values.update(min_supply_temperature=None, max_supply_temperature=None)
        engine = pump.engine()
        await engine.command("native_setting", ("passive_cooling_supply_target", 18))
        self.assertEqual(pump.writes, [("passive_cooling_supply_target", 18)])

    async def test_native_supply_pair_crosses_old_bounds_with_valid_intermediate_pairs(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command(
            "native_settings", {"min_supply_temperature": 45, "max_supply_temperature": 50}
        )
        self.assertEqual(
            pump.writes, [("max_supply_temperature", 50), ("min_supply_temperature", 45)]
        )
        pump.writes.clear()
        await engine.command(
            "native_settings", {"min_supply_temperature": 5, "max_supply_temperature": 10}
        )
        self.assertEqual(
            pump.writes, [("min_supply_temperature", 5), ("max_supply_temperature", 10)]
        )
        await engine.command(
            "native_settings", {"min_supply_temperature": 20, "max_supply_temperature": 20}
        )
        self.assertEqual(
            (pump.values["min_supply_temperature"], pump.values["max_supply_temperature"]), (20, 20)
        )

    async def test_native_maximum_edit_restores_pv_before_applying_lower_maximum(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        self.assertEqual(pump.values["fixed_supply_target"], 40)
        pump.writes.clear()
        await engine.command("native_setting", ("max_supply_temperature", 35))
        self.assertLess(
            pump.writes.index(("fixed_supply_enabled", False)),
            pump.writes.index(("max_supply_temperature", 35)),
        )
        self.assertLess(
            pump.writes.index(("fixed_supply_target", 30)),
            pump.writes.index(("max_supply_temperature", 35)),
        )
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertFalse(engine.state["managed_heating"])
        self.assertFalse(engine.state["charge_active"]["heating"])
        self.assertEqual(engine.state["overrides"], {})
        pump.writes.clear()
        await engine.tick(21, 10)
        self.assertEqual(pump.writes, [])

    async def test_native_maximum_same_value_preserves_pv_without_actuator_writes(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        pump.writes.clear()
        await engine.command("native_setting", ("max_supply_temperature", 40))
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["heating_preset"], "pv_charge")

    async def test_changed_native_heat_stop_ends_active_or_paused_excess_and_retains_new_baseline(
        self,
    ):
        for paused in (False, True):
            with self.subTest(paused=paused):
                pump = Pump()
                pump.values.update(
                    comfort_wheel=22.34, heating_season_stop=17.23, heating_enabled=True
                )
                clock = Clock()
                engine = pump.engine(now=clock)
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                if paused:
                    await engine.tick(24, 10)
                self.assertEqual(engine.state["charge_active"]["heating"], not paused)
                pump.writes.clear()
                await engine.command("native_setting", ("heating_season_stop", 18))
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertFalse(engine.state["managed_heating"])
                self.assertFalse(engine.state["charge_active"]["heating"])
                self.assertIsNone(engine.state["heating_excess_deadline"])
                self.assertEqual(pump.values["heating_season_stop"], 18)
                self.assertEqual(pump.values["comfort_wheel"], 22.34)
                self.assertEqual(pump.values["fixed_supply_target"], 30)
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertEqual(engine.state["overrides"], {})
                self.assertLess(
                    pump.writes.index(("heating_season_stop", 17.23)),
                    pump.writes.index(("heating_season_stop", 18)),
                )
                user_write = pump.writes.index(("heating_season_stop", 18))
                self.assertEqual(
                    pump.persisted_before_write[-len(pump.writes) + user_write]["overrides"][
                        "native_settings"
                    ]["heating_season_stop"],
                    17.23,
                )
                pump.writes.clear()
                await engine.tick(21, 10)
                self.assertEqual(pump.values["heating_season_stop"], 18)
                self.assertEqual(pump.writes, [])
                restarted = pump.engine(deepcopy(pump.saved[-1]), now=clock)
                self.assertTrue(await restarted.recover())
                self.assertEqual(pump.values["heating_season_stop"], 18)

    async def test_same_actual_native_heat_stop_keeps_active_or_paused_excess_and_clock(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                pump = Pump()
                clock = Clock()
                engine = pump.engine(now=clock)
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                if paused:
                    await engine.tick(24, 10)
                actual = pump.values["heating_season_stop"]
                deadline = engine.state["heating_excess_deadline"]
                journal = deepcopy(engine.state["overrides"])
                saved_count = len(pump.saved)
                pump.writes.clear()
                clock.advance_hours(1)
                await engine.command("native_setting", ("heating_season_stop", actual))
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["heating_preset"], "pv_charge")
                self.assertEqual(engine.state["heating_excess_deadline"], deadline)
                self.assertEqual(engine.state["overrides"], journal)
                self.assertEqual(len(pump.saved), saved_count)

    async def test_native_heat_stop_edit_lost_readback_rolls_back_exact_normal_original(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                pump = Pump()
                pump.values.update(
                    comfort_wheel=22.34, heating_season_stop=17.23, heating_enabled=True
                )
                original_write = pump.write

                async def lost_user_readback(key, value):
                    await original_write(key, value)
                    if key == "heating_season_stop" and value == 18:
                        raise OSError("Lost readback after the requested baseline was applied")

                pump.write = lost_user_readback
                engine = pump.engine()
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                if paused:
                    await engine.tick(24, 10)
                with self.assertRaises(OSError):
                    await engine.command("native_setting", ("heating_season_stop", 18))
                self.assertEqual(pump.values["heating_season_stop"], 17.23)
                self.assertEqual(pump.values["comfort_wheel"], 22.34)
                self.assertFalse(pump.values["fixed_supply_enabled"])
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertIsNone(engine.state["heating_excess_deadline"])
                self.assertEqual(engine.state["overrides"], {})
                await engine.tick(21, 10)
                self.assertEqual(pump.values["heating_season_stop"], 17.23)

    async def test_native_heat_stop_failed_rollback_recovers_original_never_temporary_offset(self):
        for retry in ("poll", "restart"):
            with self.subTest(retry=retry):
                pump = Pump()
                pump.values.update(
                    comfort_wheel=22.34, heating_season_stop=17.23, heating_enabled=True
                )
                original_write = pump.write
                failed_edit = False
                fail_rollback = True

                async def failed_readback_and_rollback(key, value):
                    nonlocal failed_edit
                    if (
                        key == "heating_season_stop"
                        and value == 17.23
                        and failed_edit
                        and fail_rollback
                    ):
                        raise OSError("Normal baseline rollback unavailable")
                    await original_write(key, value)
                    if key == "heating_season_stop" and value == 18:
                        failed_edit = True
                        raise OSError("Lost readback after baseline edit")

                pump.write = failed_readback_and_rollback
                engine = pump.engine()
                await engine.tick(21, 10)
                await engine.command("heating_preset", "pv_charge")
                with self.assertRaises(OSError):
                    await engine.command("native_setting", ("heating_season_stop", 18))
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertIsNone(engine.state["heating_excess_deadline"])
                self.assertEqual(
                    engine.state["overrides"]["native_settings"]["heating_season_stop"], 17.23
                )
                self.assertIn("native_settings", engine.state["pending_restore"])
                self.assertEqual(
                    pump.saved[-1]["overrides"]["native_settings"]["heating_season_stop"], 17.23
                )
                self.assertFalse(pump.values["fixed_supply_enabled"])
                fail_rollback = False
                if retry == "restart":
                    engine = pump.engine(deepcopy(pump.saved[-1]))
                    self.assertTrue(await engine.recover())
                else:
                    await engine.tick(21, 10)
                self.assertEqual(pump.values["heating_season_stop"], 17.23)
                self.assertEqual(pump.values["comfort_wheel"], 22.34)
                self.assertEqual(engine.state["heating_preset"], "normal")
                self.assertEqual(engine.state["overrides"], {})
                self.assertEqual(engine.state["pending_restore"], [])
                self.assertFalse(pump.values["fixed_supply_enabled"])

    async def test_native_heat_stop_failure_restoring_charge_never_attempts_new_baseline(self):
        pump = Pump()
        pump.values.update(comfort_wheel=22.34, heating_season_stop=17.23)
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        pump.writes.clear()
        pump.failures["heating_season_stop"] = 1
        with self.assertRaises(OSError):
            await engine.command("native_setting", ("heating_season_stop", 18))
        self.assertNotIn(("heating_season_stop", 18), pump.writes)
        self.assertNotIn("native_settings", engine.state["overrides"])
        self.assertEqual(engine.state["overrides"]["heating"]["heating_season_stop"], 17.23)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertIsNone(engine.state["heating_excess_deadline"])
        await engine.tick(21, 10)
        self.assertEqual(pump.values["heating_season_stop"], 17.23)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_native_setting_edit_preserves_existing_normal_thermostat_management(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(18, 10)
        await engine.command("heating_mode", "heat")
        pump.writes.clear()
        await engine.command("native_setting", ("max_supply_temperature", 35))
        self.assertEqual(pump.writes, [("max_supply_temperature", 35)])
        self.assertTrue(engine.state["managed_heating"])
        self.assertEqual(engine.state["heating_mode"], "heat")
        await engine.tick(22, 10)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertEqual(pump.writes, [("max_supply_temperature", 35)])

    async def test_native_setting_batch_restores_exact_originals_after_partial_or_lost_readback(
        self,
    ):
        for lost_readback in (False, True):
            with self.subTest(lost_readback=lost_readback):
                pump = Pump()
                pump.values["min_supply_temperature"] = 17.25
                original = deepcopy(pump.values)
                engine = pump.engine()
                if lost_readback:
                    pump.readback_failures["min_supply_temperature"] = 1
                else:
                    pump.failures["min_supply_temperature"] = 1
                with self.assertRaises(OSError):
                    await engine.command(
                        "native_settings",
                        {
                            "min_supply_temperature": 45,
                            "max_supply_temperature": 50,
                            "heating_season_stop": 18,
                        },
                    )
                self.assertEqual(pump.values, original)
                self.assertEqual(engine.state["overrides"], {})
                self.assertEqual(engine.state["pending_restore"], [])

    async def test_native_setting_failed_rollback_keeps_journal_and_retries(self):
        pump = Pump()
        engine = pump.engine()
        pump.failures.update(min_supply_temperature=1, max_supply_temperature=0)
        pump.readback_failures["max_supply_temperature"] = 2
        with self.assertRaises(OSError):
            await engine.command(
                "native_settings", {"min_supply_temperature": 45, "max_supply_temperature": 50}
            )
        self.assertIn("native_settings", engine.state["pending_restore"])
        self.assertEqual(engine.state["overrides"]["native_settings"]["max_supply_temperature"], 40)
        pump.failures.clear()
        pump.readback_failures.clear()
        await engine.tick(20, 10)
        self.assertEqual(
            (pump.values["min_supply_temperature"], pump.values["max_supply_temperature"]), (17, 40)
        )
        self.assertEqual(engine.state["overrides"], {})
        self.assertEqual(engine.state["pending_restore"], [])

    async def test_native_setting_shutdown_mid_write_recovers_from_durable_backup(self):
        pump = Pump()
        engine = pump.engine()
        pump.crash_after_write = "max_supply_temperature"
        with self.assertRaises(asyncio.CancelledError):
            await engine.command(
                "native_settings", {"min_supply_temperature": 45, "max_supply_temperature": 50}
            )
        self.assertEqual(pump.values["max_supply_temperature"], 50)
        self.assertEqual(
            pump.saved[-1]["overrides"]["native_settings"]["max_supply_temperature"], 40
        )
        restarted = pump.engine(deepcopy(pump.saved[-1]))
        self.assertTrue(await restarted.recover())
        self.assertEqual(
            (pump.values["min_supply_temperature"], pump.values["max_supply_temperature"]), (17, 40)
        )
        self.assertEqual(restarted.state["overrides"], {})

    async def test_native_setting_commit_store_failure_restores_hardware_and_retains_backup(self):
        pump = Pump()
        engine = pump.engine()
        pump.fail_persist_after_write = "heating_season_stop"
        with self.assertRaises(OSError):
            await engine.command("native_setting", ("heating_season_stop", 18))
        self.assertEqual(pump.values["heating_season_stop"], 17)
        self.assertEqual(engine.state["overrides"]["native_settings"]["heating_season_stop"], 17)
        pump.fail_persist = False
        pump.fail_persist_after_write = None
        await engine.tick(20, 10)
        self.assertEqual(engine.state["overrides"], {})

    async def test_native_setting_initial_store_failure_prevents_all_hardware_writes(self):
        pump = Pump()
        pump.fail_persist = True
        engine = pump.engine()
        with self.assertRaises(OSError):
            await engine.command("native_setting", ("heating_season_stop", 18))
        self.assertEqual(pump.writes, [])

    async def test_seven_native_curve_point_controls_leave_selected_curve_and_comfort_untouched(
        self,
    ):
        pump = Pump()
        pump.values["selected_heat_curve"] = 28
        engine = pump.engine()
        await engine.command(
            "native_settings", {f"heat_curve_supply_{i + 1}": 25 + i for i in range(7)}
        )
        self.assertEqual(len(pump.writes), 7)
        self.assertTrue(all(key.startswith("heat_curve_supply_") for key, _ in pump.writes))
        self.assertEqual(pump.values["selected_heat_curve"], 28)
        self.assertEqual(pump.values["comfort_wheel"], 20)
        self.assertEqual(engine.state["overrides"], {})

    async def test_initial_poll_does_not_write_or_interpret_comfort_as_room_temperature(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        engine = pump.engine()
        await engine.recover()
        await engine.tick(28.0, 10.0)
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["heating_mode"], "heat")
        self.assertEqual(engine.state["heating_target"], 20.0)
        self.assertFalse(engine.state["managed_heating"])

    async def test_first_valid_native_dial_hydrates_target_without_startup_write(self):
        pump = Pump()
        pump.values["comfort_wheel"] = None
        engine = pump.engine()
        self.assertIsNone(engine.state["heating_target"])
        pump.values["comfort_wheel"] = 23
        await engine.tick(22.3, 10)
        self.assertEqual(engine.state["heating_target"], 23)
        self.assertEqual(pump.writes, [])
        persisted = pump.engine({"heating_target": 22})
        self.assertEqual(persisted.state["heating_target"], 22)
        pump.values["comfort_wheel"] = 40
        high_native = pump.engine()
        self.assertIsNone(high_native.state["heating_target"])
        await high_native.command("heating_target", 22)
        await high_native.command("heating_mode", "heat")
        self.assertEqual(pump.values["comfort_wheel"], 22)

    async def test_explicit_target_on_unmanaged_native_heat_syncs_direct_23_to_22(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        engine = pump.engine()
        self.assertFalse(engine.state["managed_heating"])
        await engine.command("heating_target", 22)
        self.assertEqual(pump.writes, [("comfort_wheel", 22)])
        self.assertEqual(engine.state["heating_target"], 22)
        self.assertTrue(engine.state["managed_heating"])
        snapshot = pump.persisted_before_write[0]["overrides"]["heating_target_edit"]
        self.assertEqual(snapshot["comfort_wheel"], 23)
        self.assertEqual(snapshot["policy"]["heating_target"], 23)
        self.assertFalse(snapshot["policy"]["managed_heating"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_heat_cool_copies_lower_target_while_idle_and_cooling(self):
        pump = Pump()
        pump.values["comfort_wheel"] = 23
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_range", (21, 25))
        await engine.command("heating_mode", "heat_cool")
        self.assertEqual(pump.values["comfort_wheel"], 21)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        pump.values["comfort_wheel"] = 24
        await engine.tick(26, 10)
        self.assertEqual(pump.values["comfort_wheel"], 21)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        await engine.command("heating_range", (22, 26))
        self.assertEqual(pump.values["comfort_wheel"], 22)
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)

    async def test_off_and_cool_target_edits_do_not_touch_native_dial_or_water_supply(self):
        pump = Pump()
        pump.values["comfort_wheel"] = 23
        engine = pump.engine()
        await engine.command("heating_target", 22)
        await engine.command("heating_mode", "cool")
        await engine.command("heating_target", 24)
        await engine.command("heating_mode", "off")
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
        self.assertFalse(
            any(key in {"comfort_wheel", "passive_cooling_supply_target"} for key, _ in pump.writes)
        )

    async def test_unknown_or_offset_style_native_dial_never_gets_guessed_encoding(self):
        for native in (None, 0, 3, 9.99, 40.01, True, float("nan"), "23"):
            with self.subTest(native=native):
                pump = Pump()
                pump.values["comfort_wheel"] = native
                engine = pump.engine()
                await engine.command("heating_target", 22)
                with self.assertRaises(ValueError):
                    await engine.command("heating_mode", "heat")
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["heating_mode"], "off")

    async def test_unchanged_normal_poll_has_no_native_writes_or_journal_save_churn(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_mode", "heat")
        pump.writes.clear()
        count = len(pump.saved)
        await engine.tick(22.3, 10)
        self.assertEqual(pump.writes, [])
        self.assertEqual(len(pump.saved), count)
        self.assertEqual(engine.state["overrides"], {})

    async def test_target_write_failure_or_lost_readback_restores_exact_native_fraction_and_policy(
        self,
    ):
        for lost_readback in (False, True):
            with self.subTest(lost_readback=lost_readback):
                pump = Pump()
                pump.values.update(comfort_wheel=23.34, heating_enabled=True)
                engine = pump.engine()
                failures = pump.readback_failures if lost_readback else pump.failures
                failures["comfort_wheel"] = 1
                with self.assertRaises(OSError):
                    await engine.command("heating_target", 22)
                self.assertEqual(pump.values["comfort_wheel"], 23.34)
                self.assertEqual(engine.state["heating_target"], 23.34)
                self.assertTrue(pump.values["heating_enabled"])
                self.assertFalse(engine.state["managed_heating"])
                self.assertEqual(engine.state["overrides"], {})

    async def test_lost_target_readback_restores_even_when_cache_still_contains_original(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        cache = deepcopy(pump.values)

        async def verified_write(key, value):
            await pump.write(key, value)
            cache[key] = value

        engine = ControlEngine(cache.get, verified_write, pump.persist, pump.settings)
        pump.readback_failures["comfort_wheel"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_target", 22)
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(engine.state["heating_target"], 23)
        self.assertTrue(pump.values["heating_enabled"])

    async def test_target_journal_initial_storage_failure_prevents_all_native_mutation(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        engine = pump.engine()
        pump.fail_persist = True
        with self.assertRaises(OSError):
            await engine.command("heating_target", 22)
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["heating_target"], 23)
        self.assertEqual(engine.state["overrides"], {})

    async def test_late_target_commit_storage_failure_restores_saved_ha_and_native_state(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        failed = False

        async def persist(state):
            nonlocal failed
            if (
                not failed
                and state["heating_target"] == 22
                and "heating_target_edit" not in state["overrides"]
            ):
                failed = True
                raise OSError("Simulated final target commit failure")
            await pump.persist(state)

        engine = ControlEngine(pump.read, pump.write, persist, pump.settings)
        with self.assertRaises(OSError):
            await engine.command("heating_target", 22)
        self.assertTrue(failed)
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(pump.saved[-1]["heating_target"], 23)
        self.assertFalse(pump.saved[-1]["managed_heating"])
        self.assertTrue(pump.values["heating_enabled"])

    async def test_failed_permission_transfer_after_dial_copy_keeps_both_off_and_original_dial(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=23, passive_cooling_enabled=True)
        engine = pump.engine()
        await engine.command("heating_target", 22)
        pump.failures["heating_enabled"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "heat")
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.state["heating_mode"], "off")
        pump.writes.clear()
        await engine.tick(18, 10)
        self.assertEqual(pump.writes, [])

    async def test_target_copy_crash_recovery_restores_policy_and_both_native_permissions(self):
        for crash_key in ("comfort_wheel", "heating_enabled"):
            with self.subTest(crash_key=crash_key):
                pump = Pump()
                pump.values["comfort_wheel"] = 23
                engine = pump.engine()
                await engine.command("heating_target", 22)
                pump.crash_after_write = crash_key
                with self.assertRaises(asyncio.CancelledError):
                    await engine.command("heating_mode", "heat")
                self.assertIn("heating_target_edit", pump.saved[-1]["overrides"])
                restarted = pump.engine(deepcopy(pump.saved[-1]))
                self.assertEqual(restarted.state["heating_mode"], "off")
                self.assertTrue(await restarted.recover())
                self.assertEqual(pump.values["comfort_wheel"], 23)
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertEqual(restarted.state["heating_target"], 22)
                self.assertFalse(restarted.state["managed_heating"])

    async def test_cancelled_final_target_save_keeps_journal_for_shutdown_restore(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        cancelled = False

        async def persist(state):
            nonlocal cancelled
            if (
                not cancelled
                and state["heating_target"] == 22
                and "heating_target_edit" not in state["overrides"]
            ):
                cancelled = True
                raise asyncio.CancelledError("Simulated cancellation of final target save")
            await pump.persist(state)

        engine = ControlEngine(pump.read, pump.write, persist, pump.settings)
        with self.assertRaises(asyncio.CancelledError):
            await engine.command("heating_target", 22)
        self.assertIn("heating_target_edit", engine.state["overrides"])
        self.assertTrue(await engine.shutdown())
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(engine.state["heating_target"], 23)

    async def test_failed_dial_rollback_attempts_both_off_and_retains_original_retry_intent(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        engine = pump.engine()
        pump.readback_failures["comfort_wheel"] = 2
        with self.assertRaises(OSError):
            await engine.command("heating_target", 22)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertIn("heating_target_edit", engine.state["pending_restore"])
        self.assertTrue(engine.state["overrides"]["heating_target_edit"]["heating_enabled"])
        engine._hydrate()
        self.assertEqual(engine.state["heating_mode"], "heat")
        await engine.tick(22.3, 10)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(engine.state["overrides"], {})

    async def test_deferred_excess_target_edit_copies_on_expiry_from_unmanaged_native_heat(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        clock = Clock()
        engine = pump.engine(now=clock, heating_excess_hours=0.1)
        await engine.tick(22.3, 10)
        await engine.command("heating_preset", "pv_charge")
        deadline = engine.state["heating_excess_deadline"]
        await engine.command("heating_target", 22)
        self.assertEqual(pump.values["comfort_wheel"], 23)
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)
        clock.advance_hours(0.1)
        await engine.tick(22.3, 10)
        self.assertEqual(pump.values["comfort_wheel"], 22)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_auto_sensor_loss_clears_space_requests_without_blocking_boiler_ceiling(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(18, 10)
        await engine.command("heating_mode", "heat_cool")
        await engine.command("hot_water_mode", "energy_excess")
        deadline = engine.state["hot_water_excess_deadline"]
        pump.values.update(hot_water_top_temperature=62, hot_water_weighted_temperature=61)
        with self.assertRaisesRegex(ValueError, "measured inside temperature"):
            await engine.tick(None, 10)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")

    async def test_pending_target_restore_does_not_block_boiler_ceiling_or_restart_it(self):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        deadline = engine.state["hot_water_excess_deadline"]
        pump.readback_failures["comfort_wheel"] = 20
        with self.assertRaises(OSError):
            await engine.command("heating_target", 22)
        pump.values.update(hot_water_top_temperature=62, hot_water_weighted_temperature=61)
        await engine.tick(22.3, 10)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertIn("heating_target_edit", engine.state["pending_restore"])
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
        pump.values.update(hot_water_top_temperature=55, hot_water_weighted_temperature=54)
        await engine.tick(22.3, 10)
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_heating_expiry_target_error_keeps_boiler_ceiling_independent_and_does_not_recharge(
        self,
    ):
        pump = Pump()
        pump.values.update(comfort_wheel=23, heating_enabled=True)
        clock = Clock()
        engine = pump.engine(now=clock, heating_excess_hours=0.1)
        await engine.tick(22.3, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("heating_target", 22)
        await engine.command("hot_water_mode", "energy_excess")
        pump.values.update(
            comfort_wheel=0, hot_water_top_temperature=62, hot_water_weighted_temperature=61
        )
        pump.failures["comfort_wheel"] = 3
        clock.advance_hours(0.1)
        with self.assertRaises(OSError):
            await engine.tick(22.3, 10)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        with self.assertRaises(OSError):
            await engine.tick(22.3, 10)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        await engine.tick(22.3, 10)
        self.assertEqual(pump.values["comfort_wheel"], 22)
        self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_new_temperature_edits_require_whole_degrees_but_measured_values_remain_precise(
        self,
    ):
        for action, value in (
            ("heating_target", 22.5),
            ("heating_range", (21.5, 25)),
            ("heating_range", (21, 25.5)),
            ("hot_water_start", 45.5),
            ("hot_water_target", 55.5),
            ("hot_water_range", (46.5, 55)),
            ("setting", ("max_heating_temperature", 23.5)),
            ("setting", ("hot_water_hysteresis", 2.5)),
            ("setting", ("hot_water_evening_start", 35.5)),
        ):
            with self.subTest(action=action, value=value):
                pump = Pump()
                engine = pump.engine()
                with self.assertRaisesRegex(ValueError, "whole-degree"):
                    await engine.command(action, value)
                self.assertEqual(pump.writes, [])
        pump = Pump()
        pump.values["comfort_wheel"] = 23.34
        engine = pump.engine()
        engine.update_measurements(22.37, 10.42)
        self.assertEqual(engine._inside, 22.37)
        self.assertEqual(engine.state["heating_target"], 23.34)
        await engine.command("setting", ("hysteresis", 0.3))

    async def test_legacy_fractional_targets_and_unchanged_range_endpoints_are_not_rounded(self):
        pump = Pump()
        pump.values.update(
            comfort_wheel=22.5, heating_enabled=True, hot_water_start=45.5, hot_water_stop=55
        )
        engine = pump.engine(
            {
                "heating_target": 22.5,
                "heating_low": 21.5,
                "heating_high": 25,
                "managed_heating": True,
                "heating_mode": "heat",
            }
        )
        await engine.tick(22.37, 10)
        self.assertEqual(pump.values["comfort_wheel"], 22.5)
        await engine.command("heating_range", (21.5, 26))
        self.assertEqual(engine.state["heating_low"], 21.5)
        await engine.command("hot_water_target", 56)
        self.assertEqual(pump.values["hot_water_start"], 45.5)

    async def test_manual_water_uses_confirmed_max_and_restores_normal_settings(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_stop"], 70)
        self.assertEqual(pump.values["hot_water_start"], 65)
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_target"], 50)
        self.assertEqual(engine.state["hot_water_start"], 45)
        self.assertEqual(engine.state["hot_water_mode"], "manual_on")
        self.assertNotIn(("immersion_heater", True), pump.writes)
        await engine.command("hot_water_mode", "auto")
        self.assertEqual(pump.values["hot_water_stop"], 50)
        self.assertEqual(pump.values["hot_water_start"], 45)
        self.assertEqual(engine.state["overrides"], {})

    async def test_manual_water_uses_configurable_55_60_defaults_when_maxima_are_omitted(self):
        pump = Pump()
        del pump.settings["max_hot_water_temperature"]
        del pump.settings["max_start_temperature"]
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(engine.settings["max_hot_water_temperature"], 60)
        self.assertEqual(engine.settings["max_start_temperature"], 55)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (55, 60))

    async def test_water_already_above_start_refuses_unavailable_force_route(self):
        pump = Pump()
        pump.values["hot_water_boost"] = None
        pump.values.update(hot_water_top_temperature=68.0, hot_water_weighted_temperature=66.0)
        engine = pump.engine()
        with self.assertRaisesRegex(ValueError, "native boost"):
            await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual(engine.state["overrides"], {})

    async def test_water_above_start_can_request_native_boost_without_old_checkbox(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=68.0, hot_water_weighted_temperature=66.0)
        engine = pump.engine(enable_undocumented_controls=False)
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_stop"], 70)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["hot_water_mode"], "manual_on")

    async def test_native_cycle_continues_after_crossing_start_threshold(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        pump.values.update(hot_water_top_temperature=68.0, hot_water_weighted_temperature=66.0)
        await engine.tick(20, 10)
        self.assertEqual(pump.values["hot_water_stop"], 70)
        self.assertTrue(engine.state["charge_active"]["hot_water"])

    async def test_water_at_cap_accepts_manual_mode_without_requesting_more_heat(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=70.0, hot_water_weighted_temperature=66.0)
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["hot_water_mode"], "manual_on")
        self.assertFalse(engine.state["charge_active"]["hot_water"])

    async def test_high_top_temperature_ends_charge_even_if_weighted_is_lower(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        pump.values.update(hot_water_top_temperature=70.0, hot_water_weighted_temperature=60.0)
        await engine.tick(21, 12)
        self.assertFalse(engine.state["charge_active"]["hot_water"])
        self.assertEqual(pump.values["hot_water_stop"], 50)
        self.assertEqual(engine.state["hot_water_mode"], "manual_on")
        pump.values.update(hot_water_top_temperature=69.0, hot_water_weighted_temperature=61.0)
        await engine.tick(21, 12)
        self.assertEqual(pump.values["hot_water_stop"], 50)
        pump.values["hot_water_top_temperature"] = 66.0
        await engine.tick(21, 12)
        self.assertEqual(pump.values["hot_water_stop"], 70)

    async def test_maximum_start_equal_stop_is_lowered_by_hysteresis(self):
        pump = Pump()
        engine = pump.engine(max_start_temperature=70.0)
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_start"], 68)
        self.assertEqual(pump.values["hot_water_stop"], 70)

    async def test_normal_water_target_changed_during_charge_applies_after_auto(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        await engine.command("hot_water_target", 55)
        self.assertEqual(pump.values["hot_water_stop"], 70)
        await engine.command("hot_water_mode", "auto")
        self.assertEqual(pump.values["hot_water_stop"], 55)
        self.assertEqual(pump.values["hot_water_start"], 45)

    async def test_invalid_water_thresholds_cannot_be_written(self):
        pump = Pump()
        engine = pump.engine()
        with self.assertRaises(ValueError):
            await engine.command("hot_water_start", 50)
        with self.assertRaises(ValueError):
            await engine.command("hot_water_target", 45)
        self.assertEqual(pump.writes, [])

    async def test_heating_pv_caps_native_flow_and_cancels_on_sensor_loss(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21.0, 12.0)
        await engine.command("heating_preset", "pv_charge")
        self.assertTrue(pump.values["heating_enabled"])
        self.assertTrue(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["fixed_supply_target"], 40.0)
        self.assertEqual(engine.state["heating_mode"], "heat")
        await engine.tick(None, 12.0)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertFalse(pump.values["heating_enabled"])
        self.assertEqual(pump.values["fixed_supply_target"], 30.0)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertFalse(engine.state["managed_heating"])
        self.assertIn("cancelled", engine.state["control_warning"])

    async def test_heating_pv_refuses_missing_or_nonfinite_indoor_reading(self):
        for reading in (None, float("nan"), float("inf")):
            pump = Pump()
            engine = pump.engine()
            await engine.tick(reading, 10)
            with self.assertRaisesRegex(ValueError, "measured inside"):
                await engine.command("heating_preset", "pv_charge")
            self.assertEqual(pump.writes, [])
            self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_heating_ceiling_stops_and_only_restarts_after_margin(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.tick(23, 10)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        await engine.tick(22.9, 10)
        self.assertFalse(pump.values["heating_enabled"])
        await engine.tick(22.6, 10)
        self.assertTrue(pump.values["heating_enabled"])

    async def test_fresh_sensor_crossing_ceiling_prevents_charge_before_next_poll(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        # The external sensor changes after the scheduled Modbus poll, before
        # a user's PV charging command. The coordinator refreshes these values.
        engine.update_measurements(24, 12)
        await engine.command("heating_preset", "pv_charge")
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["fixed_supply_target"], 30)
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertFalse(engine.state["charge_active"]["heating"])
        self.assertFalse(any(key == "fixed_supply_target" for key, _ in pump.writes))

    async def test_normal_heat_above_target_keeps_native_permission_and_target_edits_do_not_revoke_it(
        self,
    ):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_target", 22)
        await engine.command("heating_mode", "heat")
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        for inside in (5, 22, 22.3, 35, None):
            await engine.tick(inside, 10)
            self.assertTrue(pump.values["heating_enabled"])
            self.assertFalse(pump.values["passive_cooling_enabled"])
        pump.writes.clear()
        await engine.command("heating_target", 10)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertEqual(engine.state["heating_target"], 10)
        self.assertEqual(pump.writes, [("comfort_wheel", 10)])

    async def test_normal_cool_below_target_keeps_native_permission_and_never_changes_supply_target(
        self,
    ):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_target", 22)
        await engine.command("heating_mode", "cool")
        for inside in (5, 18, 22, 22.3, 35, None):
            await engine.tick(inside, 10)
            self.assertFalse(pump.values["heating_enabled"])
            self.assertTrue(pump.values["passive_cooling_enabled"])
        await engine.command("heating_target", 35)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.values["passive_cooling_supply_target"], 16)
        self.assertFalse(
            any(key in {"comfort_wheel", "passive_cooling_supply_target"} for key, _ in pump.writes)
        )

    async def test_normal_mode_selection_clears_other_permission_first_and_off_clears_both(self):
        pump = Pump()
        pump.enforce_exclusive_space_requests = True
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_mode", "heat")
        pump.writes.clear()
        await engine.command("heating_mode", "cool")
        self.assertEqual(
            pump.writes, [("heating_enabled", False), ("passive_cooling_enabled", True)]
        )
        pump.writes.clear()
        await engine.command("heating_mode", "heat")
        self.assertEqual(
            pump.writes, [("passive_cooling_enabled", False), ("heating_enabled", True)]
        )
        await engine.command("heating_mode", "off")
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        await engine.tick(5, 10)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertEqual(engine.state["heating_mode"], "off")

    async def test_heat_cool_sensor_requirement_does_not_apply_to_single_modes(self):
        for missing_inside in (None, float("nan"), True):
            with self.subTest(missing_inside=missing_inside):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(missing_inside, 10)
                await engine.command("heating_mode", "heat")
                self.assertTrue(pump.values["heating_enabled"])
                await engine.command("heating_mode", "cool")
                self.assertTrue(pump.values["passive_cooling_enabled"])
                pump.writes.clear()
                with self.assertRaisesRegex(ValueError, "measured inside temperature"):
                    await engine.command("heating_mode", "heat_cool")
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["heating_mode"], "cool")

    async def test_heat_cool_retains_exact_range_hysteresis_boundaries(self):
        pump = Pump()
        pump.enforce_exclusive_space_requests = True
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_range", (21, 25))
        await engine.command("heating_mode", "heat_cool")
        for inside, heat, cool in (
            (20.71, False, False),
            (20.7, True, False),
            (20.99, True, False),
            (21, False, False),
            (25.29, False, False),
            (25.3, False, True),
            (25.01, False, True),
            (25, False, False),
            (19, True, False),
            (26, False, True),
        ):
            with self.subTest(inside=inside):
                await engine.tick(inside, 10)
                self.assertEqual(pump.values["heating_enabled"], heat)
                self.assertEqual(pump.values["passive_cooling_enabled"], cool)

    async def test_normal_heat_return_after_excess_ceiling_enables_native_heat_above_target(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(22.3, 10)
        await engine.command("heating_target", 22)
        await engine.command("heating_mode", "heat")
        await engine.command("heating_preset", "pv_charge")
        deadline = engine.state["heating_excess_deadline"]
        await engine.tick(23, 10)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(engine.state["heating_excess_deadline"], deadline)
        await engine.command("heating_preset", "normal")
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(engine.state["heating_target"], 22)
        self.assertIsNone(engine.state["heating_excess_deadline"])

    async def test_failed_cool_mode_switch_clears_both_and_does_not_reassert_previous_heat(self):
        for lost_readback in (False, True):
            with self.subTest(lost_readback=lost_readback):
                pump = Pump()
                engine = pump.engine()
                await engine.tick(22.3, 10)
                await engine.command("heating_mode", "heat")
                failures = pump.readback_failures if lost_readback else pump.failures
                failures["passive_cooling_enabled"] = 1
                with self.assertRaises(OSError):
                    await engine.command("heating_mode", "cool")
                self.assertFalse(pump.values["heating_enabled"])
                self.assertFalse(pump.values["passive_cooling_enabled"])
                self.assertEqual(engine.state["heating_mode"], "off")
                self.assertFalse(engine.state["managed_heating"])
                pump.writes.clear()
                await engine.tick(22.3, 10)
                self.assertEqual(pump.writes, [])

    async def test_lost_cool_readback_forces_off_even_when_the_cached_cool_permission_is_false(
        self,
    ):
        pump = Pump()
        cache = deepcopy(pump.values)

        async def verified_write(key, value):
            await pump.write(key, value)
            cache[key] = value

        engine = ControlEngine(cache.get, verified_write, pump.persist, pump.settings)
        await engine.tick(22.3, 10)
        await engine.command("heating_mode", "heat")
        pump.readback_failures["passive_cooling_enabled"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "cool")
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(pump.writes[-1], ("passive_cooling_enabled", False))
        self.assertFalse(cache["passive_cooling_enabled"])

    async def test_failed_cool_selection_during_pending_charge_restoration_preserves_journal(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(22.3, 10)
        await engine.command("heating_mode", "heat")
        await engine.command("heating_preset", "pv_charge")
        original = deepcopy(engine.state["overrides"]["heating"])
        pump.failures["fixed_supply_target"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "cool")
        self.assertEqual(engine.state["overrides"]["heating"], original)
        self.assertIn("heating", engine.state["pending_restore"])
        await engine.tick(22.3, 10)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["heating_mode"], "heat")
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_automatic_heat_cool_deadband_and_mutual_exclusion(self):
        pump = Pump()
        pump.enforce_exclusive_space_requests = True
        engine = pump.engine()
        await engine.tick(18, 8)
        await engine.command("heating_range", (20, 24))
        await engine.command("heating_mode", "heat_cool")
        self.assertTrue(pump.values["heating_enabled"])
        await engine.tick(21, 8)
        self.assertFalse(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        await engine.tick(26, 8)
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertFalse(pump.values["heating_enabled"])
        await engine.tick(18, 8)
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertFalse(any(key == "comfort_wheel" for key, _ in pump.writes))

    async def test_evening_uses_native_auto_profile_without_boost_or_excess_clock(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.command("hot_water_mode", "evening")
        self.assertEqual(engine.state["hot_water_mode"], "evening")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (35, 40))
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(engine.state["boost_enabled"])
        self.assertFalse(engine.state["native_boost_coupled"])
        self.assertIsNone(engine.state["hot_water_excess_started_at"])
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        self.assertEqual(
            (engine.state["hot_water_start"], engine.state["hot_water_target"]), (45, 50)
        )
        self.assertFalse(any(key == "immersion_heater" for key, _ in pump.writes))
        clock.advance_hours(24)
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIsNone(engine.state["hot_water_low_deadline"])
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertFalse(any(key == "hot_water_boost" and value for key, value in pump.writes))

    async def test_evening_captures_front_panel_normal_pair_before_the_first_write(self):
        pump = Pump()
        engine = pump.engine()
        pump.values.update(hot_water_start=48, hot_water_stop=58, hot_water_boost=True)
        await engine.command("hot_water_mode", "evening")
        self.assertEqual(
            (engine.state["hot_water_start"], engine.state["hot_water_target"]), (48, 58)
        )
        self.assertEqual(pump.writes[0], ("hot_water_boost", False))
        for (key, _), saved in zip(pump.writes, pump.persisted_before_write):
            if key in {"hot_water_boost", "hot_water_start", "hot_water_stop"}:
                snapshot = saved["overrides"]["hot_water"]
                self.assertEqual(
                    (snapshot["hot_water_start"], snapshot["hot_water_stop"]), (48, 58)
                )
                self.assertFalse(snapshot["hot_water_boost"])
        await engine.command("hot_water_mode", "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (48, 58))
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_evening_pair_validation_supports_inactive_save_without_hardware_caps(self):
        pump = Pump()
        engine = pump.engine(max_start_temperature=None, max_hot_water_temperature=None)
        self.assertEqual(engine._evening_water_limits(require_caps=False), (35, 40))
        await engine.command("setting", ("hot_water_evening_start", 37))
        await engine.command("setting", ("hot_water_evening_stop", 44))
        self.assertEqual(engine._evening_water_limits(require_caps=False), (37, 44))
        self.assertEqual(pump.writes, [])
        with self.assertRaises(ValueError):
            await engine.command("hot_water_mode", "evening")
        self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_evening_invalid_profiles_and_caps_fail_before_any_native_write(self):
        for settings in (
            {"hot_water_evening_start": 29},
            {"hot_water_evening_stop": 61},
            {"hot_water_evening_start": 40},
            {"hot_water_evening_stop": 35},
            {"hot_water_evening_start": True},
            {"hot_water_evening_stop": float("nan")},
            {"max_start_temperature": 34},
            {"max_hot_water_temperature": 39},
            {"max_start_temperature": None},
        ):
            with self.subTest(settings=settings):
                pump = Pump()
                engine = pump.engine(**settings)
                with self.assertRaises(ValueError):
                    await engine.command("hot_water_mode", "evening")
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["overrides"], {})
                self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_evening_configured_pair_crosses_old_pair_in_safe_native_write_order(self):
        pump = Pump()
        engine = pump.engine(hot_water_evening_start=50, hot_water_evening_stop=55)
        await engine.command("hot_water_mode", "evening")
        self.assertEqual(pump.writes, [("hot_water_stop", 55), ("hot_water_start", 50)])
        await engine.command("hot_water_mode", "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))

    async def test_evening_auto_and_off_restore_normal_pair_with_requested_permission(self):
        for mode in ("auto", "off"):
            with self.subTest(mode=mode):
                pump = Pump()
                engine = pump.engine()
                await engine.command("hot_water_mode", "evening")
                await engine.command("hot_water_mode", mode)
                self.assertEqual(engine.state["hot_water_mode"], mode)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertEqual(pump.values["hot_water_enabled"], mode != "off")
                self.assertEqual(engine.state["overrides"], {})
                self.assertFalse(pump.values["hot_water_boost"])

    async def test_excess_to_evening_restores_normal_first_and_preserves_heating_timer(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.tick(20, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "energy_excess")
        heating_deadline = engine.state["heating_excess_deadline"]
        pump.writes.clear()
        await engine.command("hot_water_mode", "evening")
        self.assertLess(
            pump.writes.index(("hot_water_boost", False)), pump.writes.index(("hot_water_stop", 40))
        )
        self.assertLess(
            pump.writes.index(("hot_water_stop", 50)), pump.writes.index(("hot_water_stop", 40))
        )
        self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_start"], 45)
        self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_stop"], 50)
        self.assertEqual(engine.state["heating_excess_deadline"], heating_deadline)
        self.assertIsNone(engine.state["hot_water_excess_deadline"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["hot_water_mode"], "evening")

    async def test_evening_to_excess_restores_normal_before_capturing_timed_originals(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock, hot_water_excess_hours=0.1)
        pump.values.update(hot_water_start=48, hot_water_stop=55)
        await engine.command("hot_water_mode", "evening")
        clock.advance_hours(1)
        pump.writes.clear()
        await engine.command("hot_water_mode", "energy_excess")
        self.assertLess(
            pump.writes.index(("hot_water_stop", 55)), pump.writes.index(("hot_water_stop", 60))
        )
        snapshot = engine.state["overrides"]["hot_water"]
        self.assertEqual((snapshot["hot_water_start"], snapshot["hot_water_stop"]), (48, 55))
        self.assertEqual(engine.state["hot_water_excess_started_at"], 4600)
        clock.advance_hours(0.1)
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (48, 55))
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_evening_reselection_and_live_profile_edit_keep_original_journal(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "evening")
        original = deepcopy(engine.state["overrides"]["hot_water"])
        engine.settings.update(hot_water_evening_start=37, hot_water_evening_stop=44)
        await engine.command("hot_water_mode", "evening")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (37, 44))
        self.assertEqual(engine.state["overrides"]["hot_water"], original)
        await engine.command("setting", ("hot_water_evening_start", 38))
        self.assertEqual(pump.values["hot_water_start"], 38)
        self.assertEqual(engine.state["overrides"]["hot_water"], original)
        pump.values.update(hot_water_start=36, hot_water_stop=43, hot_water_boost=True)
        await engine.tick(20, 10)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (38, 44))
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["overrides"]["hot_water"], original)

    async def test_evening_rejects_normal_range_edits_and_native_boost_on_without_changes(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "evening")
        original = deepcopy(engine.state["overrides"])
        pump.writes.clear()
        for action, value in (
            ("hot_water_range", (46, 55)),
            ("hot_water_start", 46),
            ("hot_water_target", 55),
            ("native_hot_water_boost", True),
            ("setting", ("hot_water_evening_start", 40)),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "Normal|below"):
                    await engine.command(action, value)
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["overrides"], original)
                self.assertEqual(engine.settings["hot_water_evening_start"], 35)

    async def test_evening_failed_native_write_or_lost_readback_restores_normal(self):
        for readback_failure in (False, True):
            with self.subTest(readback_failure=readback_failure):
                pump = Pump()
                engine = pump.engine()
                failures = pump.readback_failures if readback_failure else pump.failures
                failures["hot_water_stop"] = 1
                with self.assertRaises(OSError):
                    await engine.command("hot_water_mode", "evening")
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertEqual(engine.state["overrides"], {})
                self.assertEqual(engine.state["pending_restore"], [])

    async def test_evening_failed_rollback_retains_journal_until_poll_recovers_normal(self):
        pump = Pump()
        engine = pump.engine()
        pump.readback_failures["hot_water_start"] = 2
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "evening")
        self.assertIn("hot_water", engine.state["pending_restore"])
        self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_start"], 45)
        self.assertFalse(pump.values["hot_water_enabled"])
        await engine.tick(20, 10)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual(engine.state["overrides"], {})

    async def test_failed_live_evening_setting_restores_previous_saved_profile_value(self):
        for saved_before in (False, True):
            with self.subTest(saved_before=saved_before):
                pump = Pump()
                engine = pump.engine()
                if saved_before:
                    await engine.command("setting", ("hot_water_evening_start", 36))
                await engine.command("hot_water_mode", "evening")
                previous = engine.settings["hot_water_evening_start"]
                pump.failures["hot_water_start"] = 1
                with self.assertRaises(OSError):
                    await engine.command("setting", ("hot_water_evening_start", 38))
                self.assertEqual(engine.settings["hot_water_evening_start"], previous)
                if saved_before:
                    self.assertEqual(
                        pump.saved[-1]["settings"]["hot_water_evening_start"], previous
                    )
                else:
                    self.assertNotIn("hot_water_evening_start", pump.saved[-1]["settings"])
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )

    async def test_evening_failed_off_restoration_keeps_durable_off_intent(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "evening")
        pump.failures["hot_water_start"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "off")
        self.assertEqual(pump.saved[-1]["hot_water_exit_mode"], "off")
        self.assertFalse(pump.saved[-1]["overrides"]["hot_water"]["hot_water_enabled"])
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))

    async def test_evening_restart_and_shutdown_restore_original_disabled_normal(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                pump = Pump()
                pump.values.update(hot_water_enabled=False, hot_water_boost=True)
                engine = pump.engine()
                await engine.command("hot_water_mode", "evening")
                if not shutdown:
                    engine = pump.engine(deepcopy(pump.saved[-1]))
                self.assertTrue(await (engine.shutdown() if shutdown else engine.recover()))
                self.assertFalse(pump.values["hot_water_enabled"])
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                )
                self.assertEqual(engine.state["hot_water_mode"], "off")
                self.assertEqual(engine.state["overrides"], {})

    async def test_evening_native_auto_works_without_boost_support_or_room_and_tank_readings(self):
        pump = Pump()
        pump.values.update(
            hot_water_boost=20000,
            hot_water_top_temperature=None,
            hot_water_weighted_temperature=None,
        )
        engine = pump.engine()
        await engine.command("hot_water_mode", "evening")
        await engine.tick(None, None)
        self.assertEqual(engine.state["hot_water_mode"], "evening")
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertFalse(any(key == "hot_water_boost" for key, _ in pump.writes))
        self.assertNotIn("hot_water_boost", engine.state["overrides"]["hot_water"])

    async def test_evening_shared_sg_guard_uses_evening_stop_without_transient_demand(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=45, hot_water_weighted_temperature=44)
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(20, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "evening")
        self.assertFalse(pump.values["hot_water_enabled"])
        pump.writes.clear()
        await engine.tick(20, 10)
        self.assertFalse(any(key == "hot_water_enabled" and value for key, value in pump.writes))
        pump.values.update(hot_water_top_temperature=39, hot_water_weighted_temperature=38)
        await engine.tick(20, 10)
        self.assertTrue(pump.values["hot_water_enabled"])
        pump.values["hot_water_top_temperature"] = 40
        await engine.tick(20, 10)
        self.assertFalse(pump.values["hot_water_enabled"])
        await engine.command("heating_preset", "normal")
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (35, 40))
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_evening_inherits_guarded_normal_permission_for_shutdown_and_restart(self):
        for original_enabled in (False, True):
            for restart in (False, True):
                with self.subTest(original_enabled=original_enabled, restart=restart):
                    pump = Pump()
                    pump.values.update(
                        hot_water_enabled=original_enabled,
                        hot_water_top_temperature=55,
                        hot_water_weighted_temperature=54,
                    )
                    engine = pump.engine(smart_grid_mode="sg_ready")
                    await engine.tick(20, 10)
                    await engine.command("heating_preset", "pv_charge")
                    self.assertFalse(pump.values["hot_water_enabled"])
                    await engine.command("hot_water_mode", "evening")
                    await engine.command("hot_water_mode", "evening")
                    self.assertEqual(
                        engine.state["overrides"]["hot_water"]["hot_water_enabled"],
                        original_enabled,
                    )
                    self.assertTrue(engine.state["overrides"]["water_guard"]["hot_water_enabled"])
                    if restart:
                        engine = pump.engine(deepcopy(pump.saved[-1]), smart_grid_mode="sg_ready")
                    self.assertTrue(await (engine.recover() if restart else engine.shutdown()))
                    self.assertEqual(pump.values["hot_water_enabled"], original_enabled)
                    self.assertEqual(
                        (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                    )
                    self.assertEqual(engine.state["overrides"], {})
                    self.assertEqual(
                        engine.state["hot_water_mode"], "auto" if original_enabled else "off"
                    )

    async def test_failed_evening_permission_write_does_not_rebase_disabled_normal_sg_guard(self):
        pump = Pump()
        pump.values.update(
            hot_water_enabled=False, hot_water_top_temperature=34, hot_water_weighted_temperature=33
        )
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(20, 10)
        await engine.command("heating_preset", "pv_charge")
        pump.failures["hot_water_enabled"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "evening")
        self.assertFalse(engine.state["overrides"]["water_guard"]["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "off")
        await engine.command("heating_preset", "normal")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))

    async def test_failed_evening_off_preserves_off_across_every_sg_restore_order(self):
        for original_enabled in (False, True):
            for finish in ("poll", "shutdown", "restart"):
                with self.subTest(original_enabled=original_enabled, finish=finish):
                    pump = Pump()
                    pump.values.update(
                        hot_water_enabled=original_enabled,
                        hot_water_top_temperature=55,
                        hot_water_weighted_temperature=54,
                    )
                    engine = pump.engine(smart_grid_mode="sg_ready")
                    await engine.tick(20, 10)
                    await engine.command("heating_preset", "pv_charge")
                    await engine.command("hot_water_mode", "evening")
                    pump.failures["hot_water_start"] = 1
                    with self.assertRaises(OSError):
                        await engine.command("hot_water_mode", "off")
                    saved = deepcopy(pump.saved[-1])
                    self.assertEqual(saved["pending_restore"], ["hot_water"])
                    self.assertFalse(saved["overrides"]["hot_water"]["hot_water_enabled"])
                    self.assertFalse(saved["overrides"]["water_guard"]["hot_water_enabled"])
                    if finish == "restart":
                        engine = pump.engine(saved, smart_grid_mode="sg_ready")
                        self.assertTrue(await engine.recover())
                    elif finish == "shutdown":
                        self.assertTrue(await engine.shutdown())
                    else:
                        await engine.tick(20, 10)
                        await engine.command("heating_preset", "normal")
                    self.assertFalse(pump.values["hot_water_enabled"])
                    self.assertFalse(pump.values["hot_water_boost"])
                    self.assertEqual(engine.state["hot_water_mode"], "off")
                    self.assertEqual(engine.state["overrides"], {})
                    self.assertEqual(
                        (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
                    )

    async def test_energy_excess_immediately_sets_fixed_pair_and_native_boost(self):
        pump = Pump()
        pump.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=57)
        engine = pump.engine(hot_water_boost_start=55, hot_water_boost_stop=58)
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertTrue(engine.state["native_boost_coupled"])
        self.assertEqual(engine.state["hot_water_start"], 45)
        self.assertEqual(engine.state["hot_water_target"], 50)
        self.assertFalse(any(key == "immersion_heater" for key, _ in pump.writes))
        original = deepcopy(engine.state["overrides"])
        pump.writes.clear()
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["overrides"], original)

    async def test_two_degree_minimum_gap_accepts_verified_start_cap_of_58(self):
        for action, value in (
            ("hot_water_mode", "energy_excess"),
            ("hot_water_boost_enabled", True),
            ("boost_once", None),
        ):
            with self.subTest(action=action):
                pump = Pump()
                pump.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=59)
                engine = pump.engine(
                    hot_water_hysteresis=2,
                    max_start_temperature=58,
                    max_hot_water_temperature=60,
                )
                await engine.command(action, value)
                self.assertEqual(
                    (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60)
                )
                self.assertTrue(pump.values["hot_water_boost"])
                self.assertTrue(pump.values["hot_water_enabled"])
                self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_start"], 45)
                for key, saved in zip(pump.writes, pump.persisted_before_write):
                    if key[0] in {"hot_water_start", "hot_water_stop"}:
                        self.assertIn("hot_water", saved["overrides"])

    async def test_new_gap_requests_reject_less_than_one_or_more_than_thirty(self):
        for gap in (0, 0.5, 0.99, 30.01, True, float("nan"), float("inf")):
            with self.subTest(gap=gap):
                pump = Pump()
                engine = pump.engine()
                with self.assertRaises(ValueError):
                    await engine.command("setting", ("hot_water_hysteresis", gap))
                self.assertEqual(engine.settings["hot_water_hysteresis"], 2)
                self.assertEqual(pump.writes, [])
                bad_profile = pump.engine(hot_water_hysteresis=gap)
                with self.assertRaises(ValueError):
                    await bad_profile.command("hot_water_mode", "energy_excess")
                self.assertEqual(pump.writes, [])

    async def test_large_gap_uses_lower_start_and_never_writes_equal_thresholds(self):
        pump = Pump()
        engine = pump.engine(hot_water_hysteresis=30, max_start_temperature=30)
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (30, 60))
        pair = {"hot_water_start": 45, "hot_water_stop": 50}
        for key, value in pump.writes:
            if key in pair:
                pair[key] = value
                self.assertLess(pair["hot_water_start"], pair["hot_water_stop"])

    async def test_excess_rejects_start_cap_below_legal_30_degree_threshold_before_any_write(self):
        pump = Pump()
        engine = pump.engine(hot_water_hysteresis=2, max_start_temperature=29)
        with self.assertRaises(ValueError):
            await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(pump.writes, [])
        self.assertIsNone(engine.state["hot_water_excess_started_at"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_paused_energy_waits_for_hottest_reading_to_reach_restart_threshold(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=54)
        await engine.tick(20, 10)
        for top, weighted in ((59, 54), (58.01, 54), (56, 59)):
            with self.subTest(top=top, weighted=weighted):
                pump.values.update(
                    hot_water_top_temperature=top, hot_water_weighted_temperature=weighted
                )
                await engine.tick(20, 10)
                self.assertFalse(pump.values["hot_water_boost"])
                self.assertFalse(pump.values["hot_water_enabled"])
        pump.values.update(hot_water_top_temperature=58, hot_water_weighted_temperature=56)
        await engine.tick(20, 10)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])

    async def test_live_gap_change_updates_active_start_without_rebasing_or_extending_timer(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.command("hot_water_mode", "energy_excess")
        original = deepcopy(engine.state["overrides"]["hot_water"])
        deadline = engine.state["hot_water_excess_deadline"]
        clock.advance_hours(1)
        await engine.command("setting", ("hot_water_hysteresis", 5))
        self.assertEqual(pump.values["hot_water_start"], 58)
        await engine.tick(20, 10)
        self.assertEqual(pump.values["hot_water_start"], 55)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["overrides"]["hot_water"], original)
        self.assertEqual(engine.state["hot_water_excess_started_at"], 1000)
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
        await engine.command("hot_water_mode", "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))

    async def test_live_gap_change_updates_paused_start_then_uses_new_restart_threshold(self):
        pump = Pump()
        clock = Clock()
        engine = pump.engine(now=clock)
        await engine.command("hot_water_mode", "energy_excess")
        original = deepcopy(engine.state["overrides"]["hot_water"])
        deadline = engine.state["hot_water_excess_deadline"]
        pump.values["hot_water_top_temperature"] = 60
        await engine.tick(20, 10)
        pump.values["hot_water_top_temperature"] = 58.5
        clock.advance_hours(1)
        await engine.command("setting", ("hot_water_hysteresis", 2))
        await engine.tick(20, 10)
        self.assertEqual(pump.values["hot_water_start"], 58)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["overrides"]["hot_water"], original)
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)
        pump.values["hot_water_top_temperature"] = 58
        await engine.tick(20, 10)
        self.assertTrue(pump.values["hot_water_boost"])

    async def test_live_gap_change_retains_capped_start_without_rebasing_or_extending_timer(self):
        pump = Pump()
        engine = pump.engine(max_start_temperature=57, hot_water_hysteresis=3)
        await engine.command("hot_water_mode", "energy_excess")
        original = deepcopy(engine.state["overrides"]["hot_water"])
        deadline = engine.state["hot_water_excess_deadline"]
        pump.writes.clear()
        await engine.command("setting", ("hot_water_hysteresis", 2))
        self.assertEqual(engine.settings["hot_water_hysteresis"], 2)
        self.assertEqual(engine.hot_water_excess_info()["effective_start_temperature"], 57)
        self.assertEqual(engine.hot_water_excess_info()["effective_restart_gap"], 3)
        self.assertEqual(pump.writes, [])
        self.assertEqual(engine.state["overrides"]["hot_water"], original)
        self.assertEqual(engine.state["hot_water_excess_deadline"], deadline)

    async def test_failed_live_threshold_update_at_ceiling_still_disables_boost_and_demand(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("setting", ("hot_water_hysteresis", 5))
        pump.values["hot_water_top_temperature"] = 60
        pump.failures["hot_water_start"] = 1
        with self.assertRaises(OSError):
            await engine.tick(20, 10)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(engine.state["charge_active"]["hot_water"])
        self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_start"], 45)
        await engine.tick(20, 10)
        self.assertEqual(pump.values["hot_water_start"], 55)

    async def test_older_equal_sixty_override_restores_original_pair_on_restart(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        old_state = deepcopy(pump.saved[-1])
        pump.values.update(hot_water_start=60, hot_water_stop=60, hot_water_boost=True)
        restarted = pump.engine(old_state)
        self.assertTrue(await restarted.recover())
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual(restarted.state["overrides"], {})

    async def test_recovery_can_restore_an_exact_older_equal_sixty_original(self):
        pump = Pump()
        pump.values.update(hot_water_start=60, hot_water_stop=60)
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(pump.values["hot_water_start"], 58)
        await engine.command("hot_water_mode", "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (60, 60))
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_energy_excess_pause_and_resume_keep_gap_pair_and_selected_mode(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=54)
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertTrue(engine.state["boost_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        pump.values["hot_water_top_temperature"] = 57
        await engine.tick(20, 10)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertTrue(engine.state["native_boost_coupled"])

    async def test_energy_excess_reasserts_finished_native_one_time_boost_below_ceiling(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.values["hot_water_boost"] = False
        pump.writes.clear()
        await engine.tick(20, 10)
        self.assertEqual(pump.writes, [("hot_water_boost", True)])
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")

    async def test_energy_ceiling_attempts_water_off_even_when_native_boost_off_write_fails(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.values["hot_water_top_temperature"] = 60
        pump.failures["hot_water_boost"] = 1
        with self.assertRaises(OSError):
            await engine.tick(20, 10)
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(engine.state["native_boost_coupled"])
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertTrue(engine.state["overrides"]["hot_water"])
        await engine.tick(20, 10)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(engine.state["native_boost_coupled"])

    async def test_energy_excess_can_be_selected_at_ceiling_without_heating(self):
        pump = Pump()
        pump.values.update(hot_water_boost=True, hot_water_top_temperature=62)
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])

    async def test_energy_excess_auto_restores_exact_pump_pair_but_turns_original_boost_off(self):
        pump = Pump()
        engine = pump.engine()
        # A front-panel edit after HA initialization must be captured exactly.
        pump.values.update(hot_water_start=48, hot_water_stop=58, hot_water_boost=True)
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("hot_water_mode", "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (48, 58))
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(engine.state["boost_enabled"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_energy_excess_off_restores_pair_and_disables_both_water_and_boost(self):
        pump = Pump()
        pump.values["hot_water_boost"] = True
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("hot_water_mode", "off")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual(engine.state["hot_water_mode"], "off")

    async def test_energy_excess_requires_verified_limits_native_control_and_tank_before_writes(
        self,
    ):
        cases = (
            ({"max_start_temperature": 29}, {}),
            ({"max_hot_water_temperature": 59}, {}),
            ({"max_start_temperature": None}, {}),
            ({"smart_grid_mode": "sg_ready"}, {"hot_water_boost": None}),
            ({}, {"hot_water_boost": 20000}),
            ({}, {"hot_water_boost": 2}),
            ({}, {"hot_water_boost": "1"}),
            (
                {},
                {"hot_water_top_temperature": None, "hot_water_weighted_temperature": float("nan")},
            ),
        )
        for options, values in cases:
            with self.subTest(options=options, values=values):
                pump = Pump()
                pump.values.update(values)
                engine = pump.engine(**options)
                with self.assertRaises(ValueError):
                    await engine.command("hot_water_mode", "energy_excess")
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["hot_water_mode"], "auto")
                self.assertEqual(engine.state["overrides"], {})

    async def test_energy_excess_range_edits_remain_normal_settings_until_exit(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("hot_water_range", (48, 58))
        await engine.command("hot_water_start", 46)
        await engine.command("hot_water_target", 59)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_start"], 46)
        self.assertEqual(engine.state["overrides"]["hot_water"]["hot_water_stop"], 59)
        await engine.command("hot_water_mode", "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (46, 59))

    async def test_energy_excess_restart_restores_normal_pair_without_resuming_boost(self):
        pump = Pump()
        pump.values["hot_water_boost"] = True
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        await engine.command("hot_water_range", (47, 56))
        restarted = pump.engine(deepcopy(pump.saved[-1]))
        self.assertTrue(await restarted.recover())
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (47, 56))
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual(restarted.state["hot_water_mode"], "auto")
        self.assertFalse(restarted.state["managed_hot_water"])

    async def test_energy_excess_sensor_loss_cancels_and_forces_native_boost_off(self):
        pump = Pump()
        pump.values["hot_water_boost"] = True
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.values.update(hot_water_top_temperature=None, hot_water_weighted_temperature=None)
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(engine.state["boost_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIn("unavailable", engine.state["control_warning"])

    async def test_energy_excess_sensor_loss_returns_to_previous_off_permission(self):
        pump = Pump()
        pump.values["hot_water_enabled"] = False
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.values.update(hot_water_top_temperature=None, hot_water_weighted_temperature=None)
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))

    async def test_energy_excess_failed_off_retains_durable_off_intent_until_restore_succeeds(self):
        pump = Pump()
        pump.values["hot_water_boost"] = True
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.failures["hot_water_start"] = 2
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "off")
        self.assertEqual(engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual(engine.state["hot_water_exit_mode"], "off")
        self.assertFalse(engine.state["overrides"]["hot_water"]["hot_water_boost"])
        self.assertFalse(engine.state["overrides"]["hot_water"]["hot_water_enabled"])
        await engine.tick(20, 10)
        self.assertTrue(engine.state["pending_restore"])
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertEqual(engine.state["overrides"], {})
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_energy_excess_failed_native_boost_entry_rolls_back_without_selected_mode(self):
        pump = Pump()
        engine = pump.engine()
        pump.failures["hot_water_boost"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "energy_excess")
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(engine.state["boost_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["overrides"], {})

    async def test_energy_excess_auto_retry_keeps_native_boost_off_despite_original_true(self):
        pump = Pump()
        pump.values["hot_water_boost"] = True
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.failures["hot_water_boost"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "auto")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(engine.state["overrides"]["hot_water"]["hot_water_boost"])
        self.assertFalse(pump.saved[-1]["overrides"]["hot_water"]["hot_water_boost"])
        await engine.tick(20, 10)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["overrides"], {})

    async def test_direct_native_boost_writes_only_flag_without_temperature_configuration(self):
        pump = Pump()
        pump.settings.clear()
        pump.values.update(hot_water_top_temperature=None, hot_water_weighted_temperature=None)
        engine = pump.engine(enable_undocumented_controls=False)
        await engine.command("native_hot_water_boost", True)
        await engine.command("native_hot_water_boost", False)
        self.assertEqual(pump.writes, [("hot_water_boost", True), ("hot_water_boost", False)])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["overrides"], {})

    async def test_direct_boost_cannot_oppose_energy_excess_or_bypass_ceiling_pause(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "energy_excess")
        pump.writes.clear()
        with self.assertRaisesRegex(ValueError, "Normal or Off"):
            await engine.command("native_hot_water_boost", False)
        self.assertEqual(pump.writes, [])
        pump.values["hot_water_top_temperature"] = 60
        await engine.tick(20, 10)
        pump.writes.clear()
        with self.assertRaisesRegex(ValueError, "ceiling"):
            await engine.command("native_hot_water_boost", True)
        self.assertEqual(pump.writes, [])
        self.assertFalse(pump.values["hot_water_boost"])

    async def test_native_binary_controls_reject_missing_or_invalid_reads_without_writes(self):
        for action, key in (
            ("native_hot_water_boost", "hot_water_boost"),
            ("anti_legionella", "anti_legionella_enabled"),
        ):
            for value in (None, 2, -1, 20000, float("nan"), "1"):
                with self.subTest(action=action, value=value):
                    pump = Pump()
                    pump.values[key] = value
                    engine = pump.engine()
                    with self.assertRaises(ValueError):
                        await engine.command(action, True)
                    self.assertEqual(pump.writes, [])

    async def test_native_space_permissions_can_both_be_enabled_and_are_not_undone_by_tick(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("native_heating_enabled", True)
        await engine.command("native_cooling_enabled", True)
        self.assertEqual(
            pump.writes, [("heating_enabled", True), ("passive_cooling_enabled", True)]
        )
        self.assertEqual(engine.state["heating_mode"], "heat_cool")
        self.assertFalse(engine.state["managed_heating"])
        pump.writes.clear()
        await engine.tick(30, 10)
        self.assertEqual(pump.writes, [])
        await engine.command("native_heating_enabled", False)
        self.assertEqual(engine.state["heating_mode"], "cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])
        await engine.command("native_cooling_enabled", False)
        self.assertEqual(engine.state["heating_mode"], "off")
        self.assertEqual(
            pump.writes, [("heating_enabled", False), ("passive_cooling_enabled", False)]
        )

    async def test_native_permission_switch_cancels_pv_and_restores_other_permission(self):
        pump = Pump()
        pump.values.update(passive_cooling_enabled=True, heating_enabled=True)
        engine = pump.engine()
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        self.assertTrue(pump.values["fixed_supply_enabled"])
        await engine.command("native_heating_enabled", False)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertEqual(engine.state["heating_mode"], "cool")
        self.assertFalse(engine.state["managed_heating"])
        self.assertEqual(engine.state["overrides"], {})

    async def test_native_permission_switch_unmanages_previously_selected_thermostat(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(18, 10)
        await engine.command("heating_mode", "heat")
        await engine.command("native_cooling_enabled", True)
        self.assertEqual(engine.state["heating_mode"], "heat_cool")
        pump.writes.clear()
        await engine.tick(28, 10)
        self.assertEqual(pump.writes, [])
        await engine.command("heating_mode", "heat_cool")
        self.assertFalse(pump.values["heating_enabled"])
        self.assertTrue(pump.values["passive_cooling_enabled"])
        self.assertTrue(engine.state["managed_heating"])

    async def test_normal_water_range_enforces_thirty_to_sixty_and_hardware_caps(self):
        pump = Pump()
        engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=59)
        for pair in ((29, 50), (45, 61), (56, 58), (48, 60), (50, 50)):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                await engine.command("hot_water_range", pair)
            self.assertEqual(pump.writes, [])
        await engine.command("hot_water_range", (30, 59))
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (30, 59))

    async def test_failed_mode_write_does_not_claim_success(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(18, 8)
        pump.failures["heating_enabled"] = 1
        with self.assertRaises(OSError):
            await engine.command("heating_mode", "heat")
        self.assertEqual(engine.state["heating_mode"], "off")
        self.assertFalse(engine.state["managed_heating"])

    async def test_partial_charging_failure_rolls_back_and_keeps_normal_mode(self):
        pump = Pump()
        engine = pump.engine()
        pump.failures["hot_water_start"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_stop"], 50)
        self.assertEqual(pump.values["hot_water_start"], 45)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual(engine.state["overrides"], {})

    async def test_failed_restore_keeps_durable_snapshot_and_retries_without_recharging(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_mode", "manual_on")
        pump.failures["hot_water_start"] = 2
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "auto")
        self.assertIn("hot_water", engine.state["overrides"])
        self.assertIn("hot_water", pump.saved[-1]["overrides"])
        await engine.tick(20, 10)
        self.assertIn("hot_water", engine.state["pending_restore"])
        await engine.tick(20, 10)
        self.assertEqual(engine.state["pending_restore"], [])
        self.assertEqual(engine.state["overrides"], {})
        self.assertEqual(pump.values["hot_water_stop"], 50)
        self.assertFalse(engine.state["managed_hot_water"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_store_failure_prevents_any_charging_write(self):
        pump = Pump()
        engine = pump.engine()
        pump.fail_persist = True
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.writes, [])
        self.assertEqual(pump.values["hot_water_stop"], 50)

    async def test_reboot_recovers_both_charges_without_reapplying_previous_selection(self):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "manual_on")
        crashed_state = deepcopy(pump.saved[-1])
        restarted = pump.engine(crashed_state, smart_grid_mode="sg_ready")
        self.assertTrue(await restarted.recover())
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertFalse(pump.values["fixed_supply_enabled"])
        self.assertEqual(pump.values["hot_water_stop"], 50)
        write_count = len(pump.writes)
        await restarted.tick(18, 10)
        self.assertEqual(len(pump.writes), write_count)
        self.assertEqual(restarted.state["heating_preset"], "normal")
        self.assertFalse(restarted.state["managed_hot_water"])

    async def test_shared_sg_reference_count_and_water_guard(self):
        pump = Pump()
        pump.values["hot_water_top_temperature"] = 55
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertFalse(pump.values["hot_water_enabled"])
        await engine.command("hot_water_mode", "manual_on")
        self.assertTrue(pump.values["hot_water_enabled"])
        await engine.command("heating_preset", "normal")
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertEqual(engine.state["sg_owners"], ["hot_water"])
        await engine.command("hot_water_mode", "auto")
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertEqual(engine.state["overrides"], {})

    async def test_water_sg_boost_guards_unmanaged_heating_at_normal_room_target(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("hot_water_mode", "manual_on")
        self.assertFalse(pump.values["heating_enabled"])
        await engine.tick(19, 10)
        self.assertTrue(pump.values["heating_enabled"])
        await engine.tick(21, 10)
        self.assertFalse(pump.values["heating_enabled"])
        await engine.command("hot_water_mode", "auto")
        self.assertTrue(pump.values["heating_enabled"])

    async def test_water_guard_recovers_while_heating_boost_continues(self):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        pump.values["hot_water_top_temperature"] = 55
        await engine.tick(21, 10)
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        pump.values["hot_water_top_temperature"] = 48
        await engine.tick(21, 10)
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_auxiliary_heater_permission_selects_auto_and_never_forced_on(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("auxiliary_heater", True)
        self.assertEqual(pump.values["immersion_heater"], 2)
        await engine.command("auxiliary_heater", False)
        self.assertEqual(pump.values["immersion_heater"], 0)
        self.assertNotIn(("immersion_heater", 1), pump.writes)

    async def test_boost_once_returns_to_previous_off_mode(self):
        pump = Pump()
        engine = pump.engine(enable_undocumented_controls=True)
        await engine.command("hot_water_mode", "off")
        await engine.command("boost_once")
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])
        pump.values["hot_water_top_temperature"] = 70
        await engine.tick(20, 10)
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(pump.values["hot_water_enabled"])

    async def test_native_legionella_control_works_without_old_checkbox(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("anti_legionella", False)
        self.assertFalse(pump.values["anti_legionella_enabled"])
        self.assertEqual(pump.writes, [("anti_legionella_enabled", False)])

    async def test_invalid_setting_leaves_settings_unchanged(self):
        pump = Pump()
        engine = pump.engine()
        original = deepcopy(engine.settings)
        for setting in (("max_heating_temperature", 90), ("hysteresis", float("nan")), ("typo", 1)):
            with self.assertRaises(ValueError):
                await engine.command("setting", setting)
            self.assertEqual(engine.settings, original)

    async def test_current_options_win_over_stale_saved_settings_after_restart(self):
        pump = Pump()
        engine = pump.engine(smart_grid_mode="sg_ready")
        await engine.command("setting", ("max_heating_temperature", 24))
        await engine.command("setting", ("smart_grid_mode", "sg_ready"))
        restarted = pump.engine(
            deepcopy(pump.saved[-1]),
            max_heating_temperature=25,
            smart_grid_mode="disabled",
        )
        self.assertEqual(restarted.settings["max_heating_temperature"], 25)
        self.assertEqual(restarted.settings["smart_grid_mode"], "disabled")

    async def test_power_limit_configuration_never_writes_shared_sg_register(self):
        pump = Pump()
        pump.values["smart_grid_request"] = 3
        engine = pump.engine(smart_grid_mode="power_limit")
        await engine.command("setting", ("smart_grid_mode", "power_limit"))
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "manual_on")
        await engine.command("heating_preset", "normal")
        await engine.command("hot_water_mode", "auto")
        self.assertFalse(any(key == "smart_grid_request" for key, _ in pump.writes))
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertEqual(engine.state["sg_owners"], [])

    async def test_explicit_boost_profile_controls_only_charging_pair(self):
        pump = Pump()
        engine = pump.engine(hot_water_boost_start=55, hot_water_boost_stop=60)
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_start"], 55)
        self.assertEqual(pump.values["hot_water_stop"], 60)
        self.assertEqual(engine.state["hot_water_start"], 45)
        self.assertEqual(engine.state["hot_water_target"], 50)
        await engine.command("hot_water_mode", "auto")
        self.assertEqual(pump.values["hot_water_start"], 45)
        self.assertEqual(pump.values["hot_water_stop"], 50)

    async def test_invalid_or_unconfirmed_boost_profiles_do_not_write(self):
        for options in (
            {"hot_water_boost_start": 29, "hot_water_boost_stop": 60},
            {"hot_water_boost_start": 55, "hot_water_boost_stop": 61},
            {"hot_water_boost_start": 55, "hot_water_boost_stop": 55},
            {"hot_water_boost_start": 55},
            {"hot_water_boost_start": 55, "hot_water_boost_stop": 60, "max_start_temperature": 54},
            {
                "hot_water_boost_start": 55,
                "hot_water_boost_stop": 60,
                "max_hot_water_temperature": 59,
            },
        ):
            pump = Pump()
            engine = pump.engine(**options)
            with self.assertRaises(ValueError):
                await engine.command("hot_water_mode", "manual_on")
            self.assertEqual(pump.writes, [])
            self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_explicit_profile_above_start_requires_a_native_force_route(self):
        pump = Pump()
        pump.values["hot_water_boost"] = None
        pump.values.update(hot_water_top_temperature=58, hot_water_weighted_temperature=56)
        profile = {"hot_water_boost_start": 55, "hot_water_boost_stop": 60}
        engine = pump.engine(**profile)
        with self.assertRaisesRegex(ValueError, "native boost"):
            await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.writes, [])
        engine = pump.engine(**profile, smart_grid_mode="sg_ready")
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertEqual(pump.values["hot_water_stop"], 60)

    async def test_explicit_profile_maintenance_restarts_at_weighted_start_not_margin(self):
        pump = Pump()
        engine = pump.engine(
            hot_water_boost_start=55, hot_water_boost_stop=60, smart_grid_mode="sg_ready"
        )
        await engine.command("hot_water_mode", "manual_on")
        pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=58)
        await engine.tick(21, 10)
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(engine.state["charge_active"]["hot_water"])
        self.assertEqual(pump.values["smart_grid_request"], 0)
        self.assertIn("hot_water", engine.state["overrides"])
        pump.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=56)
        await engine.tick(21, 10)
        self.assertFalse(pump.values["hot_water_enabled"])
        pump.values["hot_water_weighted_temperature"] = 55
        await engine.tick(21, 10)
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertTrue(engine.state["charge_active"]["hot_water"])

    async def test_lower_boost_stop_does_not_resume_higher_auto_target_at_ceiling(self):
        pump = Pump()
        pump.values.update(
            hot_water_start=48,
            hot_water_stop=60,
            hot_water_top_temperature=52,
            hot_water_weighted_temperature=51.7,
        )
        engine = pump.engine(hot_water_boost_start=40, hot_water_boost_stop=45)
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_stop"], 45)
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_target"], 60)
        self.assertTrue(await engine.shutdown())
        self.assertEqual(pump.values["hot_water_start"], 48)
        self.assertEqual(pump.values["hot_water_stop"], 60)
        self.assertTrue(pump.values["hot_water_enabled"])

    async def test_first_manual_pause_failure_restores_normal_pair(self):
        pump = Pump()
        pump.values.update(
            hot_water_start=48,
            hot_water_stop=60,
            hot_water_top_temperature=52,
            hot_water_weighted_temperature=51.7,
        )
        pump.failures["hot_water_stop"] = 1
        engine = pump.engine(hot_water_boost_start=40, hot_water_boost_stop=45)
        with self.assertRaises(OSError):
            await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["hot_water_start"], 48)
        self.assertEqual(pump.values["hot_water_stop"], 60)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual(engine.state["overrides"], {})

    async def test_shared_heating_sg_does_not_reenable_paused_lower_water_profile(self):
        pump = Pump()
        pump.values.update(
            hot_water_start=48,
            hot_water_stop=60,
            hot_water_top_temperature=52,
            hot_water_weighted_temperature=51.7,
        )
        engine = pump.engine(
            hot_water_boost_start=40, hot_water_boost_stop=45, smart_grid_mode="sg_ready"
        )
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("hot_water_mode", "manual_on")
        self.assertEqual(pump.values["smart_grid_request"], 3)
        self.assertFalse(pump.values["hot_water_enabled"])
        pump.values.update(hot_water_top_temperature=46, hot_water_weighted_temperature=43)
        await engine.tick(21, 10)
        self.assertFalse(pump.values["hot_water_enabled"])

    async def test_heating_switch_restores_last_heat_cool_mode(self):
        pump = Pump()
        engine = pump.engine()
        await engine.tick(18, 10)
        await engine.command("heating_mode", "heat_cool")
        await engine.command("heating_enabled", False)
        self.assertEqual(engine.state["heating_mode"], "off")
        self.assertEqual(engine.state["last_heating_mode"], "heat_cool")
        await engine.command("heating_enabled", True)
        self.assertEqual(engine.state["heating_mode"], "heat_cool")
        self.assertTrue(pump.values["heating_enabled"])
        await engine.tick(26, 10)
        await engine.command("heating_enabled", True)
        self.assertEqual(engine.state["heating_mode"], "heat_cool")
        self.assertTrue(pump.values["passive_cooling_enabled"])

    async def test_heating_switch_default_on_and_pv_cancellation(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("heating_enabled", True)
        self.assertEqual(engine.state["heating_mode"], "heat")
        await engine.tick(21, 10)
        await engine.command("heating_preset", "pv_charge")
        await engine.command("heating_enabled", True)
        self.assertEqual(engine.state["heating_preset"], "pv_charge")
        await engine.command("heating_enabled", False)
        self.assertEqual(engine.state["heating_preset"], "normal")
        self.assertFalse(pump.values["fixed_supply_enabled"])
        await engine.command("heating_enabled", True)
        self.assertEqual(engine.state["heating_mode"], "heat")
        self.assertEqual(engine.state["heating_preset"], "normal")

    async def test_hot_water_switch_on_selects_auto_and_preserves_active_manual(self):
        pump = Pump()
        engine = pump.engine(hot_water_boost_start=55, hot_water_boost_stop=60)
        await engine.command("hot_water_enabled", False)
        await engine.command("hot_water_enabled", True)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual(pump.values["hot_water_stop"], 50)
        await engine.command("hot_water_mode", "manual_on")
        writes_before = len(pump.writes)
        await engine.command("hot_water_enabled", True)
        self.assertEqual(engine.state["hot_water_mode"], "manual_on")
        self.assertEqual(len(pump.writes), writes_before)

    async def test_normal_water_pair_can_cross_previous_thresholds_safely(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_range", (55, 60))
        self.assertEqual(pump.writes, [("hot_water_stop", 60), ("hot_water_start", 55)])
        self.assertEqual(engine.state["hot_water_start"], 55)
        self.assertEqual(engine.state["hot_water_target"], 60)
        await engine.command("hot_water_range", (35, 40))
        self.assertEqual(pump.values["hot_water_start"], 35)
        self.assertEqual(pump.values["hot_water_stop"], 40)

    async def test_normal_pair_rejects_hardware_limit_and_rolls_back_partial_failure(self):
        pump = Pump()
        engine = pump.engine()
        for pair in ((66, 70), (60, 71), (50, 50)):
            with self.assertRaises(ValueError):
                await engine.command("hot_water_range", pair)
        self.assertEqual(pump.writes, [])
        pump.failures["hot_water_start"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_range", (55, 60))
        self.assertEqual(pump.values["hot_water_start"], 45)
        self.assertEqual(pump.values["hot_water_stop"], 50)
        self.assertEqual(engine.state["hot_water_start"], 45)
        self.assertEqual(engine.state["hot_water_target"], 50)
        self.assertEqual(engine.state["overrides"], {})

    async def test_normal_pair_edit_during_manual_updates_recovery_without_touching_boost(self):
        pump = Pump()
        engine = pump.engine(hot_water_boost_start=55, hot_water_boost_stop=60)
        await engine.command("hot_water_mode", "manual_on")
        writes_before = len(pump.writes)
        await engine.command("hot_water_range", (48, 58))
        self.assertEqual(len(pump.writes), writes_before)
        self.assertEqual(pump.values["hot_water_start"], 55)
        self.assertEqual(pump.values["hot_water_stop"], 60)
        self.assertEqual(engine.state["hot_water_start"], 48)
        self.assertEqual(pump.saved[-1]["overrides"]["hot_water"]["hot_water_stop"], 58)
        self.assertTrue(await engine.shutdown())
        self.assertEqual(pump.values["hot_water_start"], 48)
        self.assertEqual(pump.values["hot_water_stop"], 58)

    async def test_fixed_one_shot_ignores_manual_profile_and_restores_native_front_panel_pair(self):
        pump = Pump()
        engine = pump.engine(hot_water_boost_start=52, hot_water_boost_stop=56)
        # The controller's pair changed after the engine cached normal settings.
        pump.values.update(hot_water_start=48, hot_water_stop=55)
        await engine.command("boost_once")
        self.assertEqual(pump.values["hot_water_start"], 58)
        self.assertEqual(pump.values["hot_water_stop"], 60)
        self.assertEqual(engine.state["hot_water_start"], 48)
        self.assertEqual(engine.state["hot_water_target"], 55)
        self.assertTrue(engine.state["boost_once"])
        writes_before = len(pump.writes)
        await engine.command("boost_once")
        self.assertEqual(len(pump.writes), writes_before)
        pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=57)
        await engine.tick(21, 10)
        self.assertEqual(pump.values["hot_water_start"], 48)
        self.assertEqual(pump.values["hot_water_stop"], 55)
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertFalse(engine.state["boost_once"])
        self.assertEqual(engine.state["overrides"], {})
        self.assertFalse(any(key == "immersion_heater" for key, _ in pump.writes))

    async def test_fixed_boost_actions_refuse_unverified_or_lower_hardware_caps(self):
        for action, value in (("boost_once", None), ("hot_water_boost_enabled", True)):
            for options in (
                {"max_start_temperature": 29},
                {"max_hot_water_temperature": 59},
                {"max_start_temperature": None},
                {"max_hot_water_temperature": None},
            ):
                pump = Pump()
                engine = pump.engine(**options)
                with self.assertRaises(ValueError):
                    await engine.command(action, value)
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["overrides"], {})
                self.assertFalse(engine.state["boost_enabled"])
                self.assertFalse(engine.state["boost_once"])

    async def test_latched_boost_keeps_gap_pair_until_off_with_exact_restoration(self):
        pump = Pump()
        pump.values.update(hot_water_start=48, hot_water_stop=55)
        engine = pump.engine(hot_water_boost_start=52, hot_water_boost_stop=56)
        await engine.command("hot_water_boost_enabled", True)
        self.assertTrue(engine.state["boost_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=57)
        await engine.tick(21, 10)
        self.assertTrue(engine.state["boost_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertIn("hot_water", pump.saved[-1]["overrides"])
        await engine.command("hot_water_boost_enabled", False)
        self.assertFalse(engine.state["boost_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (48, 55))
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")

    async def test_latched_boost_couples_native_flag_pauses_at_ceiling_and_resumes(self):
        pump = Pump()
        engine = pump.engine(enable_undocumented_controls=True)
        await engine.command("hot_water_boost_enabled", True)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(engine.state["native_boost_available"])
        self.assertTrue(engine.state["native_boost_coupled"])
        pump.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=57)
        await engine.tick(21, 10)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(engine.state["native_boost_coupled"])
        self.assertTrue(engine.state["boost_enabled"])
        pump.values.update(hot_water_top_temperature=57, hot_water_weighted_temperature=56.5)
        await engine.tick(21, 10)
        self.assertTrue(pump.values["hot_water_boost"])
        self.assertTrue(pump.values["hot_water_enabled"])
        self.assertTrue(engine.state["native_boost_coupled"])
        await engine.command("hot_water_boost_enabled", False)
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertFalse(engine.state["native_boost_coupled"])
        self.assertFalse(any(key == "immersion_heater" for key, _ in pump.writes))

    async def test_unsupported_native_boost_uses_only_verified_thresholds(self):
        pump = Pump()
        del pump.values["hot_water_boost"]
        engine = pump.engine(enable_undocumented_controls=True)
        await engine.command("hot_water_boost_enabled", True)
        self.assertTrue(engine.state["boost_enabled"])
        self.assertFalse(engine.state["native_boost_available"])
        self.assertFalse(engine.state["native_boost_coupled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        await engine.command("hot_water_boost_enabled", False)
        self.assertFalse(any(key == "hot_water_boost" for key, _ in pump.writes))

    async def test_latched_on_and_off_are_idempotent_and_keep_original_snapshot(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_boost_enabled", True)
        writes_before = len(pump.writes)
        await engine.command("hot_water_boost_enabled", True)
        self.assertEqual(len(pump.writes), writes_before)
        original = engine.state["overrides"]["hot_water"]
        self.assertEqual((original["hot_water_start"], original["hot_water_stop"]), (45, 50))
        await engine.command("hot_water_boost_enabled", False)
        writes_before = len(pump.writes)
        await engine.command("hot_water_boost_enabled", False)
        self.assertEqual(len(pump.writes), writes_before)

    async def test_individual_normal_sliders_update_latched_recovery_pair(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("hot_water_boost_enabled", True)
        writes_before = len(pump.writes)
        await engine.command("hot_water_start", 49)
        await engine.command("hot_water_target", 58)
        self.assertEqual(len(pump.writes), writes_before)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (58, 60))
        snapshot = pump.saved[-1]["overrides"]["hot_water"]
        self.assertEqual((snapshot["hot_water_start"], snapshot["hot_water_stop"]), (49, 58))
        await engine.command("hot_water_boost_enabled", False)
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (49, 58))

    async def test_latched_restart_and_shutdown_restore_off_mode_and_native_flag(self):
        for shutdown in (False, True):
            pump = Pump()
            pump.values.update(hot_water_enabled=False, hot_water_boost=True)
            engine = pump.engine(enable_undocumented_controls=True)
            await engine.command("hot_water_boost_enabled", True)
            if not shutdown:
                engine = pump.engine(deepcopy(pump.saved[-1]), enable_undocumented_controls=True)
            self.assertTrue(await (engine.shutdown() if shutdown else engine.recover()))
            self.assertFalse(engine.state["boost_enabled"])
            self.assertEqual(engine.state["hot_water_mode"], "off")
            self.assertFalse(pump.values["hot_water_enabled"])
            self.assertTrue(pump.values["hot_water_boost"])
            self.assertEqual(
                (pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50)
            )

    async def test_latched_restore_failure_keeps_snapshot_and_retries_original_off_mode(self):
        pump = Pump()
        pump.values["hot_water_enabled"] = False
        engine = pump.engine(enable_undocumented_controls=True)
        await engine.command("hot_water_boost_enabled", True)
        pump.failures["hot_water_start"] = 2
        with self.assertRaises(OSError):
            await engine.command("hot_water_boost_enabled", False)
        self.assertTrue(engine.state["boost_enabled"])
        self.assertIn("hot_water", engine.state["overrides"])
        self.assertFalse(engine.state["native_boost_coupled"])
        await engine.tick(21, 10)
        self.assertIn("hot_water", engine.state["pending_restore"])
        await engine.tick(21, 10)
        self.assertFalse(engine.state["boost_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["overrides"], {})

    async def test_fixed_boost_failed_second_threshold_write_rolls_back(self):
        pump = Pump()
        engine = pump.engine()
        pump.failures["hot_water_start"] = 1
        with self.assertRaises(OSError):
            await engine.command("hot_water_boost_enabled", True)
        self.assertFalse(engine.state["boost_enabled"])
        self.assertEqual(engine.state["hot_water_mode"], "auto")
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertEqual(engine.state["overrides"], {})

    async def test_manual_profile_validation_remains_independent_of_latched_boost(self):
        pump = Pump()
        engine = pump.engine(hot_water_boost_start=60, hot_water_boost_stop=60)
        with self.assertRaises(ValueError):
            engine._water_limits()
        await engine.command("hot_water_boost_enabled", True)
        engine.settings.update(hot_water_boost_start=55, hot_water_boost_stop=55)
        with self.assertRaises(ValueError):
            engine._water_limits()
        self.assertEqual(engine._water_limits(once=True)[:2], (60, 58))
        await engine.command("hot_water_boost_enabled", False)
        with self.assertRaises(ValueError):
            await engine.command("hot_water_range", (60, 60))

    async def test_fixed_boost_refuses_missing_tank_measurements_even_with_native_coupling(self):
        for action, value in (("boost_once", None), ("hot_water_boost_enabled", True)):
            pump = Pump()
            pump.values.update(
                hot_water_top_temperature=None, hot_water_weighted_temperature=float("nan")
            )
            engine = pump.engine(enable_undocumented_controls=True)
            with self.assertRaisesRegex(ValueError, "valid tank temperature"):
                await engine.command(action, value)
            self.assertEqual(pump.writes, [])
            self.assertEqual(engine.state["overrides"], {})
            self.assertFalse(engine.state["boost_enabled"])

    async def test_latched_boost_sensor_loss_cancels_restores_and_does_not_resume(self):
        pump = Pump()
        pump.values["hot_water_enabled"] = False
        engine = pump.engine(enable_undocumented_controls=True)
        await engine.command("hot_water_boost_enabled", True)
        pump.values.update(
            hot_water_top_temperature=float("inf"), hot_water_weighted_temperature=None
        )
        await engine.tick(21, 10)
        self.assertFalse(engine.state["boost_enabled"])
        self.assertFalse(engine.state["native_boost_coupled"])
        self.assertEqual(engine.state["hot_water_mode"], "off")
        self.assertFalse(pump.values["hot_water_enabled"])
        self.assertFalse(pump.values["hot_water_boost"])
        self.assertEqual((pump.values["hot_water_start"], pump.values["hot_water_stop"]), (45, 50))
        self.assertIn("cancelled", engine.state["control_warning"])
        writes_before = len(pump.writes)
        pump.values.update(hot_water_top_temperature=48, hot_water_weighted_temperature=47)
        await engine.tick(21, 10)
        self.assertEqual(len(pump.writes), writes_before)
        self.assertFalse(engine.state["boost_enabled"])

    async def test_invalid_native_boost_enum_is_not_snapshotted_or_written(self):
        pump = Pump()
        pump.values["hot_water_boost"] = 20000
        engine = pump.engine(enable_undocumented_controls=True)
        await engine.command("hot_water_boost_enabled", True)
        self.assertTrue(engine.state["boost_enabled"])
        self.assertFalse(engine.state["native_boost_available"])
        self.assertFalse(engine.state["native_boost_coupled"])
        self.assertNotIn("hot_water_boost", engine.state["overrides"]["hot_water"])
        await engine.command("hot_water_boost_enabled", False)
        self.assertEqual(pump.values["hot_water_boost"], 20000)
        self.assertFalse(any(key == "hot_water_boost" for key, _ in pump.writes))
        pump.values.update(hot_water_top_temperature=68, hot_water_weighted_temperature=66)
        with self.assertRaisesRegex(ValueError, "native boost"):
            await engine.command("hot_water_mode", "manual_on")


if __name__ == "__main__":
    unittest.main()
