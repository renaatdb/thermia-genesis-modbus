"""Native automation edits preserve the user's separate permission sequence."""

import asyncio
import unittest
from copy import deepcopy

from tests.test_control import Pump
from tests.test_platforms import Coordinator, MODULES


class NativeAutomationTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_fractional_heating_target_before_permission(self):
        for heating, cooling in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(heating=heating, cooling=cooling):
                pump = Pump()
                pump.values.update(heating_enabled=heating, passive_cooling_enabled=cooling)
                original = deepcopy(pump.values)
                engine = pump.engine()
                await engine.command("native_heating_target", 23.25)
                await engine.tick(19, 10)
                self.assertEqual(pump.writes, [("comfort_wheel", 23.25)])
                self.assertEqual(pump.values, original | {"comfort_wheel": 23.25})
                self.assertFalse(engine.state["managed_heating"])
                self.assertEqual(engine.state["overrides"], {})
                self.assertEqual(
                    pump.persisted_before_write[0]["overrides"]["automation_temperature"],
                    {"comfort_wheel": 20},
                )

    async def test_emhass_boiler_cycle_changes_only_start_and_restores_45_58(self):
        pump = Pump()
        pump.values.update(hot_water_stop=58, passive_cooling_enabled=True)
        engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
        original = deepcopy(pump.values)
        await engine.command("native_hot_water_start", 55)
        await engine.tick(23, 10)
        self.assertEqual(pump.values, original | {"hot_water_start": 55})
        await engine.command("native_hot_water_start", 45)
        await engine.tick(23, 10)
        self.assertEqual(pump.values, original)
        self.assertEqual(pump.writes, [("hot_water_start", 55), ("hot_water_start", 45)])
        self.assertFalse(engine.state["managed_hot_water"])

    async def test_stop_edit_preserves_start_and_all_permissions_even_water_off(self):
        pump = Pump()
        pump.values.update(hot_water_enabled=False)
        original = deepcopy(pump.values)
        engine = pump.engine()
        await engine.command("native_hot_water_stop", 58)
        await engine.tick(23, 10)
        self.assertEqual(pump.values, original | {"hot_water_stop": 58})
        self.assertEqual(pump.writes, [("hot_water_stop", 58)])
        self.assertEqual(engine.state["hot_water_mode"], "off")

    async def test_invalid_values_and_crossed_thresholds_never_write(self):
        cases = [
            ("native_heating_target", value)
            for value in (True, "23", None, float("nan"), float("inf"), 10**500, 9, 36, 23.251)
        ] + [
            ("native_hot_water_start", value) for value in (True, "55", 56, 55.1, 58)
        ] + [("native_hot_water_stop", 45), ("native_hot_water_stop", 61)]
        for action, value in cases:
            with self.subTest(action=action, value=value):
                pump = Pump()
                pump.values["hot_water_stop"] = 58
                engine = pump.engine(max_start_temperature=55, max_hot_water_temperature=60)
                with self.assertRaises(ValueError):
                    await engine.command(action, value)
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state["overrides"], {})

    async def test_crossed_pair_is_rejected_without_moving_the_other_end(self):
        for action, value in (("native_hot_water_start", 58), ("native_hot_water_stop", 45)):
            pump = Pump()
            pump.values["hot_water_stop"] = 58
            engine = pump.engine(max_start_temperature=60, max_hot_water_temperature=60)
            with self.assertRaisesRegex(ValueError, "START must remain below STOP"):
                await engine.command(action, value)
            self.assertEqual(pump.writes, [])

    async def test_missing_limit_or_readback_is_rejected(self):
        for missing in ("max_start_temperature", "hot_water_start", "hot_water_stop"):
            pump = Pump()
            pump.values["hot_water_stop"] = 58
            options = {}
            if missing.startswith("max_"):
                options[missing] = None
            else:
                pump.values[missing] = None
            engine = pump.engine(**options)
            with self.assertRaises(ValueError):
                await engine.command("native_hot_water_start", 55)
            self.assertEqual(pump.writes, [])

    async def test_unchanged_edit_does_not_write(self):
        pump = Pump()
        engine = pump.engine()
        for action, value in (
            ("native_heating_target", 20), ("native_hot_water_start", 45),
            ("native_hot_water_stop", 50),
        ):
            await engine.command(action, value)
        self.assertEqual(pump.writes, [])

    async def test_managed_thermostat_requires_explicit_native_switch_handoff(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("heating_mode", "heat")
        pump.writes.clear()
        with self.assertRaisesRegex(ValueError, "release thermostat control"):
            await engine.command("native_heating_target", 23.25)
        self.assertEqual(pump.writes, [])
        await engine.command("native_heating_enabled", False)
        pump.writes.clear()
        await engine.command("native_heating_target", 23.25)
        self.assertEqual(pump.writes, [("comfort_wheel", 23.25)])
        self.assertFalse(pump.values["heating_enabled"])

    async def test_water_presets_boost_and_smart_grid_are_not_overwritten(self):
        for kind in ("preset", "boost", "smart_grid"):
            pump = Pump()
            engine = pump.engine()
            if kind == "preset":
                engine.state["hot_water_mode"] = "evening"
                engine.state["managed_hot_water"] = True
            elif kind == "boost":
                pump.values["hot_water_boost"] = True
            else:
                engine.state["sg_owners"] = ["heating"]
            with self.assertRaises(ValueError):
                await engine.command("native_hot_water_start", 46)
            self.assertEqual(pump.writes, [])

    async def test_readback_failure_restores_only_the_changed_temperature(self):
        for action, key, new in (
            ("native_heating_target", "comfort_wheel", 23.25),
            ("native_hot_water_start", "hot_water_start", 55),
            ("native_hot_water_stop", "hot_water_stop", 59),
        ):
            pump = Pump()
            pump.values["hot_water_stop"] = 58
            original = deepcopy(pump.values)
            engine = pump.engine()
            pump.readback_failures[key] = 1
            with self.assertRaises(OSError):
                await engine.command(action, new)
            self.assertEqual(pump.values, original)
            self.assertEqual(pump.writes, [(key, new), (key, original[key])])
            self.assertEqual(engine.state["overrides"], {})

    async def test_crash_after_write_retains_journal_and_recovers_on_restart(self):
        for action, key, new in (
            ("native_heating_target", "comfort_wheel", 23.25),
            ("native_hot_water_start", "hot_water_start", 55),
            ("native_hot_water_stop", "hot_water_stop", 59),
        ):
            pump = Pump()
            pump.values["hot_water_stop"] = 58
            original = deepcopy(pump.values)
            engine = pump.engine()
            pump.crash_after_write = key
            with self.assertRaises(asyncio.CancelledError):
                await engine.command(action, new)
            self.assertEqual(pump.values[key], new)
            restarted = pump.engine(state=pump.saved[-1])
            self.assertTrue(await restarted.recover())
            self.assertEqual(pump.values, original)
            self.assertEqual(restarted.state["overrides"], {})

    async def test_failed_rollback_is_retried_without_touching_permissions(self):
        pump = Pump()
        pump.values["hot_water_stop"] = 58
        original = deepcopy(pump.values)
        engine = pump.engine()
        pump.readback_failures["hot_water_start"] = 2
        with self.assertRaises(OSError):
            await engine.command("native_hot_water_start", 55)
        self.assertIn("automation_temperature", engine.state["pending_restore"])
        self.assertTrue(await engine.recover())
        self.assertEqual(pump.values, original)
        self.assertTrue(all(key == "hot_water_start" for key, _ in pump.writes))

    async def test_storage_failure_before_write_never_touches_pump(self):
        pump = Pump()
        engine = pump.engine()
        pump.fail_persist = True
        with self.assertRaises(OSError):
            await engine.command("native_heating_target", 23.25)
        self.assertEqual(pump.writes, [])


class NativeAutomationPlatformTests(unittest.IsolatedAsyncioTestCase):
    async def test_entities_route_native_edits_and_setup_never_writes(self):
        coordinator = Coordinator()
        coordinator.registers.update(heating_enabled=False, hot_water_stop=58)
        entities = []
        await MODULES["number"].async_setup_entry(
            None, type("Entry", (), {"runtime_data": coordinator})(), entities.extend
        )
        self.assertEqual(coordinator.writes, [])
        by_key = {entity._attr_unique_id: entity for entity in entities}
        target = by_key["pump-id_native_heating_target"]
        start = by_key["pump-id_hot_water_start"]
        stop = by_key["pump-id_hot_water_stop"]
        await target.async_set_native_value(23.25)
        await start.async_set_native_value(55)
        await start.async_set_native_value(45)
        self.assertEqual(coordinator.writes, [
            ("comfort_wheel", 23.25), ("hot_water_start", 55), ("hot_water_start", 45),
        ])
        self.assertEqual((target.native_value, start.native_value, stop.native_value), (23.25, 45, 58))
        coordinator.engine.settings["max_start_temperature"] = 55
        self.assertEqual(start.native_max_value, 55)
        self.assertEqual(target.native_max_value, 35)


if __name__ == "__main__":
    unittest.main()
