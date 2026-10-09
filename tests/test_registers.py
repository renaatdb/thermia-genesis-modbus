"""Protect the manufacturer map details that can affect real control."""

import importlib.util
import sys
import unittest
from pathlib import Path


def _load_catalogue():
    path = Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus" / "registers.py"
    spec = importlib.util.spec_from_file_location("thermia_register_test_catalogue", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


catalogue = _load_catalogue()


class RegisterCatalogueTests(unittest.TestCase):
    def test_no_duplicates_or_overlapping_counter_words(self):
        self.assertEqual(len(catalogue.REGISTERS), len(catalogue.REGISTERS_BY_KEY))
        occupied = set()
        for spec in catalogue.REGISTERS:
            self.assertIn(spec.space, ("coil", "discrete", "input", "holding"))
            for address in range(spec.address, spec.address + spec.count):
                self.assertNotIn((spec.space, address), occupied, spec.key)
                occupied.add((spec.space, address))

    def test_documented_pv_and_outdoor_controls(self):
        expected = {
            "heating_enabled": ("coil", 9),
            "hot_water_enabled": ("coil", 8),
            "fixed_supply_enabled": ("coil", 41),
            "fixed_supply_target": ("holding", 116),
            "outdoor_source": ("holding", 117),
            "bms_outdoor_temperature": ("holding", 118),
            "smart_grid_request": ("holding", 124),
            "immersion_heater": ("holding", 321),
        }
        for key, pair in expected.items():
            spec = catalogue.REGISTERS_BY_KEY[key]
            self.assertEqual((spec.space, spec.address), pair)
            self.assertTrue(spec.writable)
        self.assertEqual(catalogue.REGISTERS_BY_KEY["indoor_temperature"].scale, 0.1)
        self.assertTrue(catalogue.REGISTERS_BY_KEY["outdoor_temperature"].signed)

    def test_heat_pump_and_system_temperature_points_remain_distinct(self):
        expected = {
            "condenser_in_temperature": 8,
            "condenser_out_temperature": 9,
            "supply_temperature": 12,
            "return_temperature": 27,
        }
        for key, address in expected.items():
            spec = catalogue.REGISTERS_BY_KEY[key]
            self.assertEqual(
                (spec.space, spec.address, spec.scale, spec.signed), ("input", address, 0.01, True)
            )
        self.assertIn(
            "Heat-pump return", catalogue.REGISTERS_BY_KEY["condenser_in_temperature"].name
        )
        self.assertIn(
            "Heat-pump supply", catalogue.REGISTERS_BY_KEY["condenser_out_temperature"].name
        )
        self.assertIn("System supply", catalogue.REGISTERS_BY_KEY["supply_temperature"].name)
        self.assertIn("System return", catalogue.REGISTERS_BY_KEY["return_temperature"].name)

    def test_counter_endianness_and_no_missing_temperature_sentinel(self):
        for key, address in (
            ("compressor_operating_hours", 48),
            ("hot_water_operating_hours", 50),
            ("additional_heater_operating_hours", 52),
        ):
            spec = catalogue.REGISTERS_BY_KEY[key]
            self.assertEqual(
                (spec.address, spec.count, spec.word_order, spec.missing),
                (address, 2, "big", False),
            )
        energy = catalogue.REGISTERS_BY_KEY["meter_energy"]
        self.assertEqual(
            (energy.address, energy.count, energy.word_order, energy.scale, energy.missing),
            (83, 2, "little", 0.1, False),
        )

    def test_activity_bits_preserve_concurrent_demands(self):
        flags = (1 << 2) | (1 << 3) | (1 << 7)
        active = [name for bit, name in catalogue.ACTIVE_DEMAND_BITS.items() if flags & (1 << bit)]
        self.assertEqual(active, ["Hot water", "Heating", "Passive cooling"])
        self.assertNotIn(8, catalogue.ACTIVE_DEMAND_BITS)

    def test_unverified_controls_are_separate_and_disabled(self):
        self.assertEqual(catalogue.UNVERIFIED_KEYS, {"anti_legionella_enabled", "hot_water_boost"})
        for key in catalogue.UNVERIFIED_KEYS:
            self.assertFalse(catalogue.REGISTERS_BY_KEY[key].enabled)
        self.assertFalse(catalogue.REGISTERS_BY_KEY["smart_grid_input_1"].writable)
        self.assertEqual(catalogue.REGISTERS_BY_KEY["inverter_temperature"].scale, 1)


if __name__ == "__main__":
    unittest.main()
