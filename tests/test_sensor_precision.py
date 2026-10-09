"""Preserve measured-temperature precision through the platform entities."""

import unittest
from copy import deepcopy
from types import SimpleNamespace

from test_platforms import MODULES, Coordinator


class SensorPrecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_and_tank_sensors_preserve_source_precision_and_register_defaults(
        self,
    ):
        coordinator = Coordinator()
        coordinator.effective_inside = 22.34
        measurements = {
            "indoor_temperature": 25.4,
            "hot_water_top_temperature": 25.0,
            "hot_water_lower_temperature": 24.27,
            "hot_water_weighted_temperature": 25.36,
        }
        coordinator.registers.update(measurements, supply_temperature=34.56)
        original_registers = deepcopy(coordinator.registers)
        original_state = deepcopy(coordinator.engine.state)
        entities = []
        await MODULES["sensor"].async_setup_entry(
            None, SimpleNamespace(runtime_data=coordinator), entities.extend
        )
        registered = {entity._attr_unique_id: entity for entity in entities}
        for key, reading in measurements.items():
            with self.subTest(sensor=key):
                entity = registered[f"pump-id_{key}"]
                self.assertEqual(entity._attr_suggested_display_precision, 2)
                self.assertEqual(entity.native_value, reading)
                self.assertIsInstance(entity.native_value, float)
        inside = registered["pump-id_effective_inside_temperature"]
        self.assertIsNone(getattr(inside, "_attr_suggested_display_precision", None))
        self.assertEqual(inside.native_value, 22.34)
        supply = registered["pump-id_supply_temperature"]
        self.assertEqual(supply._attr_suggested_display_precision, 2)
        self.assertEqual(supply.native_value, 34.56)
        self.assertEqual(
            registered["pump-id_max_supply_temperature"]._attr_suggested_display_precision, 2
        )
        self.assertEqual(
            registered["pump-id_operational_mode"]._attr_suggested_display_precision, 0
        )
        self.assertEqual(coordinator.registers, original_registers)
        self.assertEqual(coordinator.engine.state, original_state)
        self.assertEqual(coordinator.commands, [])
        self.assertEqual(coordinator.writes, [])
