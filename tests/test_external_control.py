"""External ownership keeps solar planning and slow-floor policy outside the plugin."""

import ast
import unittest
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_control import Pump
from tests.test_platforms import Coordinator, MODULES


# Coordinator tests use a simulated device; only the missing dependency's
# exception types are needed here, not its network or register implementation.
errors = ModuleType("modbus_connection")
errors.ModbusError = type("ModbusError", (Exception,), {})
errors.IllegalDataAddressError = type("IllegalDataAddressError", (errors.ModbusError,), {})
errors.IllegalFunctionError = type("IllegalFunctionError", (errors.ModbusError,), {})
try:
    import modbus_connection
except ImportError:
    with patch.dict("sys.modules", {"modbus_connection": errors}):
        from tests import test_coordinator as coordinator_tests
else:
    from tests import test_coordinator as coordinator_tests


class ExternalControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_external_start_and_solar_temperature_swings_do_not_write(self):
        pump = Pump()
        pump.values.update(heating_enabled=True, passive_cooling_enabled=False)
        engine = pump.engine(control_mode="external")
        self.assertTrue(await engine.recover())
        for inside, outside in ((18, -5), (24, 14), (27, 20), (22, 10), (None, None)):
            await engine.tick(inside, outside)
        self.assertEqual(pump.writes, [])
        self.assertFalse(engine.state["managed_heating"])
        self.assertFalse(engine.state["managed_hot_water"])

    async def test_internal_policy_actions_are_rejected_before_any_write(self):
        for action, value in (
            ("heating_mode", "off"), ("heating_mode", "heat_cool"),
            ("heating_preset", "pv_charge"), ("heating_preset", "vacation"),
            ("heating_temperature_edit", {"temperature": 23}),
            ("hot_water_mode", "energy_excess"), ("hot_water_mode", "off"),
            ("hot_water_temperature_edit", {"target_temp_low": 45, "target_temp_high": 58}),
            ("hot_water_boost_enabled", True), ("boost_once", None),
            ("passive_cooling", True), ("heating_enabled", True),
        ):
            with self.subTest(action=action, value=value):
                pump = Pump()
                engine = pump.engine(control_mode="external")
                original = deepcopy(engine.state)
                with self.assertRaisesRegex(ValueError, "External Control / EMHASS"):
                    await engine.command(action, value)
                self.assertEqual(pump.writes, [])
                self.assertEqual(engine.state, original)

    async def test_native_controls_preserve_separate_emhass_sequence(self):
        pump = Pump()
        pump.values["hot_water_stop"] = 58
        engine = pump.engine(control_mode="external")
        await engine.command("native_heating_target", 23.25)
        self.assertFalse(pump.values["heating_enabled"])
        await engine.command("native_heating_enabled", True)
        await engine.command("native_hot_water_start", 55)
        await engine.command("hot_water_enabled", False)
        await engine.tick(27, 20)
        await engine.command("hot_water_enabled", True)
        await engine.command("native_hot_water_start", 45)
        self.assertEqual(pump.writes, [
            ("comfort_wheel", 23.25), ("heating_enabled", True),
            ("hot_water_start", 55), ("hot_water_enabled", False),
            ("hot_water_enabled", True), ("hot_water_start", 45),
        ])
        self.assertEqual(pump.values["hot_water_stop"], 58)
        self.assertFalse(engine.state["managed_hot_water"])

    async def test_native_cooling_boost_and_hygiene_are_still_explicit_commands(self):
        pump = Pump()
        engine = pump.engine(control_mode="external")
        for action, value in (("native_cooling_enabled", True),
                              ("native_hot_water_boost", True), ("anti_legionella", False)):
            await engine.command(action, value)
        await engine.tick(28, 20)
        self.assertEqual(pump.writes, [
            ("passive_cooling_enabled", True), ("hot_water_boost", True),
            ("anti_legionella_enabled", False),
        ])

    async def test_switch_to_external_releases_normal_control_without_switching_pump_off(self):
        pump = Pump()
        engine = pump.engine()
        await engine.command("heating_mode", "heat")
        original = deepcopy(pump.values)
        pump.writes.clear()
        await engine.command("setting", ("control_mode", "external"))
        await engine.tick(27, 20)
        self.assertEqual(pump.values, original)
        self.assertEqual(pump.writes, [])
        self.assertFalse(engine.state["managed_heating"])
        self.assertEqual(pump.saved[-1]["settings"]["control_mode"], "external")

    async def test_active_profiles_restore_before_external_handoff(self):
        for action, value in (("heating_preset", "low"),
                              ("heating_preset", "pv_charge"),
                              ("hot_water_mode", "energy_excess"),
                              ("hot_water_mode", "evening")):
            with self.subTest(profile=value):
                pump = Pump()
                pump.values["heating_enabled"] = True
                engine = pump.engine()
                engine.update_measurements(19, 10)
                original = deepcopy(pump.values)
                await engine.command(action, value)
                await engine.set_control_mode("external")
                self.assertEqual(pump.values, original)
                self.assertEqual(engine.state["overrides"], {})
                self.assertEqual(engine.state["sg_owners"], [])
                pump.writes.clear()
                await engine.tick(28, 20)
                self.assertEqual(pump.writes, [])

    async def test_handoff_waits_for_failed_restoration_and_retains_journal(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        engine = pump.engine()
        engine.update_measurements(19, 10)
        await engine.command("heating_preset", "low")
        pump.failures["comfort_wheel"] = 20
        with self.assertRaisesRegex(ValueError, "Restore temporary settings"):
            await engine.set_control_mode("external")
        self.assertFalse(engine.external_control)
        self.assertTrue(engine.state["pending_restore"])
        self.assertTrue(engine.state["overrides"])
        pump.failures.clear()
        await engine.set_control_mode("external")
        self.assertTrue(engine.external_control)
        self.assertEqual(pump.values["comfort_wheel"], 20)

    async def test_external_restart_cannot_resume_stored_managed_auto(self):
        pump = Pump()
        pump.values["heating_enabled"] = True
        state = {"managed_heating": True, "managed_hot_water": True,
                 "heating_mode": "heat_cool", "heating_preset": "normal"}
        engine = pump.engine(control_mode="external", state=state)
        await engine.recover()
        await engine.tick(28, 20)
        self.assertEqual(pump.writes, [])
        self.assertTrue(pump.values["heating_enabled"])
        self.assertFalse(engine.state["managed_heating"])

    async def test_return_to_internal_does_not_automatically_start_a_thermostat(self):
        pump = Pump()
        engine = pump.engine(control_mode="external")
        await engine.set_control_mode("internal")
        await engine.tick(18, -5)
        self.assertEqual(pump.writes, [])
        await engine.command("heating_mode", "heat")
        self.assertTrue(pump.values["heating_enabled"])

    async def test_external_mode_still_recovers_an_interrupted_temperature_write(self):
        pump = Pump()
        engine = pump.engine(control_mode="external")
        pump.readback_failures["comfort_wheel"] = 1
        with self.assertRaises(OSError):
            await engine.command("native_heating_target", 23.25)
        self.assertEqual(pump.values["comfort_wheel"], 20)
        self.assertEqual(pump.writes, [("comfort_wheel", 23.25), ("comfort_wheel", 20)])
        self.assertFalse(pump.values["heating_enabled"])

    async def test_handoff_storage_failure_does_not_select_external_mode(self):
        pump = Pump()
        engine = pump.engine()
        pump.fail_persist = True
        with self.assertRaises(OSError):
            await engine.set_control_mode("external")
        self.assertFalse(engine.external_control)
        self.assertEqual(pump.writes, [])

    async def test_humidity_guard_remains_active_in_external_mode(self):
        pump = Pump()
        pump.values["passive_cooling_enabled"] = True
        engine = pump.engine(control_mode="external", inside_humidity_sensor="sensor.humidity",
                             cooling_humidity_enabled=True)
        await engine.tick(25, 20, 90)
        self.assertFalse(pump.values["passive_cooling_enabled"])
        self.assertTrue(engine.cooling_humidity_info()["paused"])

    async def test_thermostat_entities_keep_ids_but_are_unavailable_and_reject_writes(self):
        coordinator = Coordinator()
        heating = MODULES["climate"].ThermiaClimate(coordinator)
        water = MODULES["climate"].ThermiaHotWaterClimate(coordinator)
        ids = (heating._attr_unique_id, water._attr_unique_id)
        self.assertTrue(heating.available)
        self.assertTrue(water.available)
        await coordinator.engine.set_control_mode("external")
        self.assertFalse(heating.available)
        self.assertFalse(water.available)
        self.assertEqual(ids, (heating._attr_unique_id, water._attr_unique_id))
        with self.assertRaisesRegex(ValueError, "External Control"):
            await heating.async_set_hvac_mode(MODULES["climate"].HVACMode.HEAT)
        self.assertEqual(coordinator.writes, [])
        await coordinator.engine.set_control_mode("internal")
        self.assertTrue(heating.available)
        self.assertTrue(water.available)

    async def test_hot_water_switch_uses_native_readback_in_external_mode(self):
        coordinator = Coordinator()
        coordinator.engine.settings["control_mode"] = "external"
        coordinator.engine.state["hot_water_mode"] = "off"
        switch = MODULES["switch"].ThermiaSwitch(
            coordinator, "hot_water_enabled", "Hot Water", "hot_water_enabled", "mdi:water-boiler"
        )
        self.assertTrue(switch.is_on)
        coordinator.registers["hot_water_enabled"] = False
        self.assertFalse(switch.is_on)

    def test_legacy_options_keep_internal_mode_and_invalid_modes_are_rejected(self):
        const = MODULES["const"]
        self.assertEqual(const.normalize_options({})["control_mode"], "internal")
        self.assertEqual(const.normalize_options({"control_mode": "external"})["control_mode"],
                         "external")
        with self.assertRaises(ValueError):
            const.normalize_options({"control_mode": "other"})


class ExternalCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def make_coordinator(self):
        return coordinator_tests.CoordinatorTests().with_charge_controls()

    async def test_configure_external_only_keeps_native_settings_unchanged(self):
        coordinator, device, _ = self.make_coordinator()
        original = deepcopy(device.values)
        options = deepcopy(coordinator.engine.settings) | {"control_mode": "external"}
        coordinator.engine.recover = AsyncMock(wraps=coordinator.engine.recover)
        await coordinator.async_configure_controls(options, control_changes={})
        coordinator.engine.recover.assert_awaited_once()
        self.assertEqual(device.values, original)
        self.assertEqual(device.writes, [])
        self.assertTrue(coordinator.engine.external_control)
        self.assertFalse(coordinator.engine.state["managed_heating"])

    async def test_options_listener_releases_managed_normal_before_next_tick(self):
        coordinator, device, _ = self.make_coordinator()
        await coordinator.async_command("heating_mode", "heat")
        device.writes.clear()
        coordinator._pending_options = {"control_mode": "external"}
        self.assertTrue(await coordinator.async_apply_pending_options())
        await coordinator.engine.tick(28, 20)
        self.assertEqual(device.writes, [])
        self.assertTrue(coordinator.engine.external_control)
        self.assertFalse(coordinator.engine.state["managed_heating"])

    async def test_coordinator_blocks_climate_but_allows_direct_target_and_water_permission(self):
        coordinator, device, _ = self.make_coordinator()
        await coordinator.engine.set_control_mode("external")
        with self.assertRaisesRegex(coordinator_tests.HomeAssistantError, "External Control"):
            await coordinator.async_command("heating_mode", "heat_cool")
        await coordinator.async_command("native_heating_target", 23.25)
        await coordinator.async_command("hot_water_enabled", False)
        self.assertEqual(device.writes, [("comfort_wheel", 23.25), ("hot_water_enabled", False)])

    async def test_configure_handoff_refuses_unconfirmed_profile_restoration(self):
        coordinator, device, _ = self.make_coordinator()
        await coordinator.async_command("heating_preset", "low")
        options = deepcopy(coordinator.engine.settings) | {"control_mode": "external"}
        original_write = device.async_write

        async def unavailable_dial(key, value):
            if key == "comfort_wheel":
                raise OSError("Controller unavailable during restoration")
            return await original_write(key, value)

        device.async_write = unavailable_dial
        with self.assertRaises(coordinator_tests.HomeAssistantError):
            await coordinator.async_configure_controls(options, control_changes={})
        self.assertFalse(coordinator.engine.external_control)
        self.assertTrue(coordinator.engine.state["pending_restore"])
        device.async_write = original_write
        await coordinator.async_configure_controls(options, control_changes={})
        self.assertTrue(coordinator.engine.external_control)
        self.assertEqual(device.values["comfort_wheel"], 20)


class NewEntryDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_connection_step_reads_only_and_creates_external_entry(self):
        # Execute the actual step without the unused form/schema dependency.
        path = Path(__file__).parents[1] / "custom_components/thermia_genesis_modbus/config_flow.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        flow_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and node.name == "ThermiaConfigFlow")
        step = next(node for node in flow_class.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "async_step_user")
        unit = SimpleNamespace(read_input_registers=AsyncMock(return_value=[0]))
        requests = []

        @asynccontextmanager
        async def temporary_unit(hass, params, unit_id):
            requests.append((params, unit_id))
            yield unit

        namespace = {
            "CONF_UNIT_ID": "unit_id", "NAME": MODULES["const"].NAME,
            "async_get_temporary_unit": temporary_unit,
            "ModbusTcpParams": lambda **kwargs: kwargs,
            "ModbusError": errors.ModbusError,
            "HomeAssistantError": coordinator_tests.HomeAssistantError,
        }
        exec(compile(ast.Module(body=[step], type_ignores=[]), str(path), "exec"), namespace)
        flow = SimpleNamespace(
            hass=SimpleNamespace(config_entries=SimpleNamespace(async_entries=Mock(return_value=[]))),
            async_set_unique_id=AsyncMock(), _abort_if_unique_id_configured=Mock(),
            async_create_entry=Mock(side_effect=lambda **kwargs: kwargs),
        )
        result = await namespace["async_step_user"](
            flow, {"host": " Pump.LOCAL ", "port": 502, "unit_id": 1}
        )
        self.assertEqual(result["options"], {"control_mode": "external"})
        self.assertEqual(result["data"], {"host": "Pump.LOCAL", "port": 502, "unit_id": 1})
        self.assertEqual(requests, [({"host": "Pump.LOCAL", "port": 502}, 1)])
        unit.read_input_registers.assert_awaited_once_with(1, 1)
        flow.async_set_unique_id.assert_awaited_once_with("pump.local:502:1")
        pump = Pump()
        engine = pump.engine(**MODULES["const"].normalize_options(result["options"]))
        await engine.recover()
        await engine.tick(28, 20)
        self.assertEqual(pump.writes, [])
