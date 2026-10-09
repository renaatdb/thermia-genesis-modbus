"""Exercise the real decoder and transport against a refusing Modbus device."""

import importlib
import sys
import types
import unittest
from pathlib import Path

from modbus_connection import IllegalDataAddressError, ModbusTimeoutError

PACKAGE = "thermia_adapter_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus")]
sys.modules[PACKAGE] = package
catalogue = importlib.import_module(f"{PACKAGE}.registers")
adapter = importlib.import_module(f"{PACKAGE}.device")


class Unit:
    def __init__(self):
        self.data = {space: {} for space in ("input", "holding", "coil", "discrete")}
        self.reads = []
        self.timeout = False
        self.clamp = False

    async def read(self, space, address, count):
        self.reads.append((space, address, count))
        if self.timeout:
            raise ModbusTimeoutError()
        table = self.data[space]
        if any(key not in table for key in range(address, address + count)):
            raise IllegalDataAddressError()
        return [table[key] for key in range(address, address + count)]

    async def read_input_registers(self, address, count):
        return await self.read("input", address, count)

    async def read_holding_registers(self, address, count):
        return await self.read("holding", address, count)

    async def read_coils(self, address, count):
        return await self.read("coil", address, count)

    async def read_discrete_inputs(self, address, count):
        return await self.read("discrete", address, count)

    async def write_register(self, address, value):
        self.data["holding"][address] = min(value, 6000) if self.clamp else value

    async def write_coil(self, address, value):
        self.data["coil"][address] = value


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_bad_register_does_not_hide_neighbors(self):
        unit = Unit()
        unit.data["input"] = {15: 5500, 17: 4700}
        device = adapter.ThermiaDevice(unit)
        device.specs = {
            spec.key: spec
            for spec in catalogue.REGISTERS
            if spec.space == "input" and spec.address in (15, 16, 17)
        }
        values = await device.async_update()
        self.assertEqual(values["hot_water_top_temperature"], 55)
        self.assertEqual(values["hot_water_weighted_temperature"], 47)
        self.assertIsNone(values["hot_water_lower_temperature"])
        unit.reads.clear()
        await device.async_update()
        self.assertFalse(any(address <= 16 < address + count for _, address, count in unit.reads))

    async def test_timeout_never_marks_a_register_permanently_unsupported(self):
        unit = Unit()
        unit.timeout = True
        device = adapter.ThermiaDevice(unit)
        with self.assertRaises(ModbusTimeoutError):
            await device.async_update()
        self.assertEqual(device.unsupported, set())

    async def test_negative_temperature_and_counter_word_order(self):
        specs = {item.key: item for item in catalogue.REGISTERS}
        self.assertEqual(adapter.decode(specs["outdoor_temperature"], [65536 - 575]), -5.75)
        self.assertIsNone(adapter.decode(specs["outdoor_temperature"], [20000]))
        self.assertEqual(adapter.decode(specs["compressor_operating_hours"], [2, 2345]), 133417)
        self.assertEqual(adapter.decode(specs["meter_energy"], [2345, 2]), 13341.7)
        self.assertEqual(adapter.decode(specs["compressor_operating_hours"], [0, 20000]), 20000)

    async def test_write_requires_actual_readback_and_read_only_is_rejected(self):
        unit = Unit()
        device = adapter.ThermiaDevice(unit)
        self.assertEqual(await device.async_write("hot_water_stop", 55), 55)
        unit.clamp = True
        with self.assertRaisesRegex(ValueError, "did not confirm"):
            await device.async_write("hot_water_stop", 70)
        with self.assertRaisesRegex(ValueError, "read-only"):
            await device.async_write("outdoor_temperature", 10)

    async def test_domestic_comfort_target_is_written_without_an_offset(self):
        unit = Unit()
        unit.data["holding"][5] = 2300
        device = adapter.ThermiaDevice(unit)
        self.assertEqual(await device.async_read_value("comfort_wheel"), 23)
        self.assertEqual(await device.async_write("comfort_wheel", 22), 22)
        self.assertEqual(unit.data["holding"][5], 2200)
        self.assertEqual(unit.reads[-1], ("holding", 5, 1))
        self.assertEqual(device.values["comfort_wheel"], 22)
        for target in (5, 41):
            with self.subTest(target=target), self.assertRaises(ValueError):
                await device.async_write("comfort_wheel", target)
            self.assertEqual(unit.data["holding"][5], 2200)

    async def test_comfort_target_clamping_is_reported_instead_of_adopted(self):
        unit = Unit()
        device = adapter.ThermiaDevice(unit)

        async def clamp_comfort(address, value):
            unit.data["holding"][address] = 2100

        unit.write_register = clamp_comfort
        with self.assertRaisesRegex(ValueError, "did not confirm comfort_wheel=22"):
            await device.async_write("comfort_wheel", 22)
        self.assertEqual(device.values["comfort_wheel"], 21)

    async def test_native_boost_and_anti_legionella_are_probed_without_opt_in(self):
        unit = Unit()
        specs = catalogue.REGISTERS_BY_KEY
        unit.data["holding"][specs["hot_water_boost"].address] = 0
        unit.data["coil"][specs["anti_legionella_enabled"].address] = True
        device = adapter.ThermiaDevice(unit)
        device.specs = {key: specs[key] for key in adapter.OPTIONAL_WRITES}
        values = await device.async_update()
        self.assertEqual(values["hot_water_boost"], 0)
        self.assertIs(values["anti_legionella_enabled"], True)
        self.assertEqual(await device.async_write("hot_water_boost", True), 1)

    async def test_missing_boost_does_not_hide_supported_anti_legionella(self):
        unit = Unit()
        specs = catalogue.REGISTERS_BY_KEY
        unit.data["coil"][specs["anti_legionella_enabled"].address] = True
        device = adapter.ThermiaDevice(unit)
        device.specs = {key: specs[key] for key in adapter.OPTIONAL_WRITES}
        values = await device.async_update()
        self.assertIsNone(values["hot_water_boost"])
        self.assertIs(values["anti_legionella_enabled"], True)
        self.assertIn("hot_water_boost", device.unsupported)
        with self.assertRaises(ValueError):
            await device.async_write("hot_water_boost", True)

    async def test_explicit_legacy_adapter_filter_remains_available_for_recovery(self):
        device = adapter.ThermiaDevice(Unit(), undocumented=False)
        self.assertNotIn("hot_water_boost", device.specs)
        self.assertNotIn("anti_legionella_enabled", device.specs)
        with self.assertRaises(ValueError):
            await device.async_write("hot_water_boost", 1)


if __name__ == "__main__":
    unittest.main()
