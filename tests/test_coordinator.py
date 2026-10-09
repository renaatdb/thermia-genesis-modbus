"""Narrow coordinator checks with only its HA scheduling/storage API stubbed.

These exercise the actual coordinator, temperature selection and control-policy
boundary. They are not a Home Assistant runtime or real heat-pump test.
"""

import asyncio
import importlib
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


class UpdateFailed(Exception):
    pass


class HomeAssistantError(Exception):
    pass


class DataUpdateCoordinator:
    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.last_update_success = True
        self._listeners = []

    def async_add_listener(self, callback, context=None):
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback)

    def async_update_listeners(self):
        for listener in tuple(self._listeners):
            listener()


class Store:
    def __init__(self, *args):
        self.saved = []

    async def async_save(self, state):
        self.saved.append(state)

    async def async_load(self):
        return None


def _module(name, **symbols):
    module = ModuleType(name)
    module.__dict__.update(symbols)
    return module


def callback(function):
    function._hass_callback = True
    return function


def _load_coordinator():
    package_name = "thermia_coordinator_test"
    package = _module(package_name)
    package.__path__ = [
        str(Path(__file__).parents[1] / "custom_components" / "thermia_genesis_modbus")
    ]
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", HomeAssistantError=HomeAssistantError
        ),
        "homeassistant.core": _module("homeassistant.core", callback=callback),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.event": _module(
            "homeassistant.helpers.event",
            async_track_state_change_event=lambda *args: lambda: None,
            async_track_state_report_event=lambda *args: lambda: None,
        ),
        "homeassistant.helpers.storage": _module("homeassistant.helpers.storage", Store=Store),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=UpdateFailed,
        ),
        "homeassistant.util": _module(
            "homeassistant.util", dt=SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc))
        ),
        package_name: package,
    }
    with patch.dict(sys.modules, stubs):
        coordinator = importlib.import_module(f"{package_name}.coordinator")
        catalogue = importlib.import_module(f"{package_name}.registers")
    return coordinator, catalogue


coordinator_module, catalogue = _load_coordinator()


class Device:
    def __init__(self):
        self.unit = SimpleNamespace()
        self.specs = catalogue.REGISTERS_BY_KEY
        self.unsupported = set()
        self.errors = {}
        self.values = {}
        self.writes = []
        self.physical_temperature = 4.5

    async def async_write(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value
        return value

    async def async_read_value(self, key):
        if key == "outdoor_temperature" and self.values.get("outdoor_source") == 0:
            return self.physical_temperature
        return self.values.get(key)

    async def async_update(self):
        return dict(self.values)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_rapid_committed_selection_is_published_before_debounced_refresh(self):
        coordinator, device, _ = self.with_charge_controls()
        history = []
        requested = []

        def record_selection():
            self.assertTrue(coordinator._lock.locked())
            self.assertEqual(coordinator._values, device.values)
            saved = coordinator._store.saved[-1]["control"]
            self.assertEqual(saved["heating_mode"], coordinator.engine.state["heating_mode"])
            self.assertEqual(saved["heating_preset"], coordinator.engine.state["heating_preset"])
            self.assertEqual(saved["hot_water_mode"], coordinator.engine.state["hot_water_mode"])
            history.append(coordinator._mode_preset_selection())

        async def deferred_refresh():
            # A refresh requested inside Core's cooldown returns before the
            # trailing poll. It must not be the sole publisher of selections.
            self.assertFalse(coordinator._lock.locked())
            self.assertEqual(history[-1], coordinator._mode_preset_selection())
            requested.append(coordinator._mode_preset_selection())

        coordinator.async_add_listener(record_selection)
        coordinator.async_request_refresh = AsyncMock(side_effect=deferred_refresh)
        changes = (
            ("heating_preset", "low", ("heat", "low", "auto")),
            ("heating_preset", "vacation", ("heat", "vacation", "auto")),
            ("heating_mode", "cool", ("cool", "vacation", "auto")),
            ("heating_preset", "normal", ("cool", "normal", "auto")),
            ("heating_mode", "heat_cool", ("heat_cool", "normal", "auto")),
            ("hot_water_mode", "evening", ("heat_cool", "normal", "evening")),
            ("hot_water_mode", "energy_excess", ("heat_cool", "normal", "energy_excess")),
            ("hot_water_mode", "auto", ("heat_cool", "normal", "auto")),
            ("heating_mode", "off", ("off", None, "auto")),
        )
        for action, value, expected in changes:
            await coordinator.async_command(action, value)
            self.assertEqual(history[-1], expected)
        expected_history = [expected for _, _, expected in changes]
        self.assertEqual(history, expected_history)
        self.assertEqual(requested, expected_history)

    async def test_unchanged_preset_reselection_resets_clock_without_selection_notification(self):
        for action, value, started_key, deadline_key in (
            ("heating_preset", "pv_charge", "heating_excess_started_at", "heating_excess_deadline"),
            (
                "hot_water_mode",
                "energy_excess",
                "hot_water_excess_started_at",
                "hot_water_excess_deadline",
            ),
            ("hot_water_mode", "evening", "hot_water_low_started_at", "hot_water_low_deadline"),
        ):
            with self.subTest(preset=value):
                coordinator, device, _ = self.with_charge_controls()
                clock = [1000.0]
                coordinator.engine.now = lambda: clock[0]
                notify = Mock()
                coordinator.async_add_listener(notify)
                await coordinator.async_command(action, value)
                notify.assert_called_once()
                first_deadline = coordinator.engine.state[deadline_key]
                native = deepcopy(device.values)
                notify.reset_mock()
                clock[0] = 1060.0

                await coordinator.async_command(action, value)

                notify.assert_not_called()
                self.assertEqual(coordinator.engine.state[started_key], clock[0])
                self.assertEqual(coordinator.engine.state[deadline_key], first_deadline + 60)
                self.assertEqual(device.values, native)
                self.assertEqual(coordinator.async_request_refresh.await_count, 2)

    async def test_failed_native_selection_never_publishes_unconfirmed_state(self):
        coordinator, device, _ = self.with_charge_controls()
        notify = Mock()
        coordinator.async_add_listener(notify)
        original_write = device.async_write

        async def lost_target_confirmation(key, value):
            result = await original_write(key, value)
            if key == "comfort_wheel" and value == 18:
                raise OSError("Native target readback was lost")
            return result

        device.async_write = lost_target_confirmation
        with self.assertRaisesRegex(HomeAssistantError, "readback was lost"):
            await coordinator.async_command("heating_preset", "low")

        self.assertIn(("comfort_wheel", 18), device.writes)
        self.assertEqual(device.values["comfort_wheel"], 20)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        notify.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_selection_is_not_published_until_final_tick_has_succeeded(self):
        coordinator, _, _ = self.with_charge_controls()
        notify = Mock()
        coordinator.async_add_listener(notify)
        coordinator.engine.tick = AsyncMock(side_effect=OSError("Final safety check failed"))

        with self.assertRaisesRegex(HomeAssistantError, "Final safety check failed"):
            await coordinator.async_command("heating_mode", "cool")

        self.assertEqual(coordinator.engine.state["heating_mode"], "cool")
        notify.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_temperature_edit_and_command_triggered_expiry_publish_preset_exit(self):
        for end in ("temperature_edit", "expiry"):
            with self.subTest(end=end):
                coordinator, _, _ = self.with_charge_controls()
                coordinator.engine.settings["heating_excess_hours"] = 0.1
                clock = [1000.0]
                coordinator.engine.now = lambda: clock[0]
                history = []
                coordinator.async_add_listener(
                    lambda: history.append(coordinator._mode_preset_selection())
                )
                await coordinator.async_command("heating_preset", "pv_charge")
                history.clear()

                if end == "temperature_edit":
                    await coordinator.async_command("heating_temperature_edit", {"temperature": 22})
                else:
                    clock[0] = 1361.0
                    await coordinator.async_command("hot_water_mode", "auto")

                self.assertEqual(history, [("heat", "normal", "auto")])
                self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])

    async def test_device_edit_after_unset_entry_migration_preserves_saved_custom_caps_and_active_timer(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.entry.options = {
            "max_start_temperature": None,
            "max_hot_water_temperature": None,
            "configuration_version": 6,
        }
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        coordinator._store.async_load = AsyncMock(
            return_value={
                "control": {},
                "settings": {
                    "max_start_temperature": 65,
                    "max_hot_water_temperature": 70,
                    "hot_water_excess_hours": 0.5,
                    "configuration_version": 6,
                },
            }
        )
        await coordinator.async_load()
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
        deadline = coordinator.engine.state["hot_water_excess_deadline"]
        clock[0] += 600
        await coordinator.async_command("setting", ("hot_water_hysteresis", 1))
        self.assertEqual(coordinator.entry.options["max_start_temperature"], 65)
        self.assertEqual(coordinator.entry.options["max_hot_water_temperature"], 70)
        coordinator.engine.shutdown = AsyncMock(wraps=coordinator.engine.shutdown)
        coordinator._pending_options = coordinator_module.normalize_options(
            coordinator.entry.options
        )
        self.assertTrue(await coordinator.async_apply_pending_options())
        coordinator.engine.shutdown.assert_not_awaited()
        self.assertEqual(coordinator.engine.settings["max_start_temperature"], 65)
        self.assertEqual(coordinator.engine.settings["max_hot_water_temperature"], 70)
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (59, 60)
        )

    async def test_default_capped_excess_start_and_device_gap_edit_preserve_actual_restart_and_timer(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.engine.settings.update(max_start_temperature=55, max_hot_water_temperature=60)
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        await coordinator.async_command("hot_water_mode", "energy_excess")
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (55, 60)
        )
        original = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
        deadline = coordinator.engine.state["hot_water_excess_deadline"]
        device.values["hot_water_top_temperature"] = 60
        await coordinator._async_update_data()
        device.values["hot_water_top_temperature"] = 57
        await coordinator._async_update_data()
        self.assertFalse(device.values["hot_water_boost"])
        clock[0] += 3600
        await coordinator.async_command("setting", ("hot_water_hysteresis", 1))
        self.assertEqual(coordinator.entry.options["hot_water_hysteresis"], 1)
        self.assertEqual(coordinator.entry.options["configuration_version"], 7)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (55, 60)
        )
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original)
        device.values["hot_water_top_temperature"] = 55
        await coordinator._async_update_data()
        self.assertTrue(device.values["hot_water_boost"])
        self.assertEqual(coordinator.engine.hot_water_excess_info()["effective_restart_gap"], 5)

    async def test_default_maximum_migration_preserves_saved_custom_limits_when_entry_is_unset(
        self,
    ):
        for entry_options, saved_settings, expected in (
            ({}, {}, (55, 60)),
            ({"max_start_temperature": None, "max_hot_water_temperature": None}, {}, (55, 60)),
            (
                {"max_start_temperature": None, "max_hot_water_temperature": None},
                {"max_start_temperature": 65, "max_hot_water_temperature": 70},
                (65, 70),
            ),
            (
                {"max_start_temperature": 60, "max_hot_water_temperature": 65},
                {"max_start_temperature": 65, "max_hot_water_temperature": 70},
                (60, 65),
            ),
        ):
            with self.subTest(entry_options=entry_options, saved_settings=saved_settings):
                coordinator, _, _ = self.make_coordinator(entry_options)
                coordinator._store.async_load = AsyncMock(
                    return_value={"control": {}, "settings": saved_settings}
                )
                await coordinator.async_load()
                self.assertEqual(
                    (
                        coordinator.engine.settings["max_start_temperature"],
                        coordinator.engine.settings["max_hot_water_temperature"],
                    ),
                    expected,
                )
                self.assertEqual(coordinator.engine.settings["configuration_version"], 7)

    async def test_coordinator_collision_edit_repairs_pair_and_failure_restores_original_without_excess(
        self,
    ):
        for failed in (False, True):
            with self.subTest(failed=failed):
                coordinator, device, _ = self.with_charge_controls()
                device.values.update(hot_water_start=54, hot_water_stop=55)
                coordinator._values = dict(device.values)
                coordinator.engine.state.update(hot_water_start=54, hot_water_target=55)
                coordinator.engine.settings.update(
                    max_start_temperature=55, max_hot_water_temperature=60
                )
                await coordinator.async_command("hot_water_mode", "energy_excess")
                real_write = device.async_write
                lost = False

                async def write(key, value):
                    nonlocal lost
                    result = await real_write(key, value)
                    if failed and not lost and key == "hot_water_stop" and value == 54:
                        lost = True
                        raise OSError("Repaired STOP readback lost")
                    return result

                device.async_write = write
                if failed:
                    with self.assertRaisesRegex(HomeAssistantError, "readback lost"):
                        await coordinator.async_command(
                            "hot_water_temperature_edit", {"temperature": 54}
                        )
                    expected = (54, 55)
                else:
                    await coordinator.async_command(
                        "hot_water_temperature_edit", {"temperature": 54}
                    )
                    expected = (49, 54)
                self.assertEqual(
                    (device.values["hot_water_start"], device.values["hot_water_stop"]), expected
                )
                self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
                self.assertFalse(device.values["hot_water_boost"])
                self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
                self.assertFalse(coordinator.engine.state["overrides"])

    async def test_loaded_retired_supply_preference_cannot_limit_native_maximum_charging(self):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.entry.options = {"charge_supply_temperature": 35, "configuration_version": 5}
        coordinator._store.async_load = AsyncMock(
            return_value={
                "settings": {"charge_supply_temperature": 60, "configuration_version": 5},
                "control": {"settings": {"charge_supply_temperature": None}},
            }
        )
        device.values["max_supply_temperature"] = 38.34
        device.values["fixed_supply_target"] = 30.34
        coordinator._values = dict(device.values)
        await coordinator.async_load()
        self.assertNotIn("charge_supply_temperature", coordinator.engine.settings)
        self.assertNotIn("charge_supply_temperature", coordinator.engine.state["settings"])
        self.assertEqual(coordinator.engine.settings["configuration_version"], 7)
        await coordinator.async_command("heating_preset", "pv_charge")
        self.assertEqual(device.values["fixed_supply_target"], 38.34)
        await coordinator.async_command("heating_preset", "normal")
        self.assertEqual(device.values["fixed_supply_target"], 30.34)
        self.assertFalse(device.values["fixed_supply_enabled"])

    async def test_device_water_gap_setting_changes_live_native_start_without_restarting_timer(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        coordinator.entry.options = {"charge_supply_temperature": 35, "configuration_version": 5}
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
        deadline = coordinator.engine.state["hot_water_excess_deadline"]
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (58, 60)
        )
        clock[0] += 3600
        await coordinator.async_command("setting", ("hot_water_hysteresis", 4))
        self.assertEqual(coordinator.entry.options["hot_water_hysteresis"], 4)
        self.assertNotIn("charge_supply_temperature", coordinator.entry.options)
        self.assertEqual(coordinator.entry.options["configuration_version"], 7)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (56, 60)
        )
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual(coordinator.engine.state["hot_water_excess_started_at"], 1000)
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], deadline)
        self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original)

    async def test_unrelated_device_edit_preserves_legacy_fractional_duration_through_options_listener(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        # Old runtime storage may contain the duration before options included it.
        coordinator.engine.settings.update(
            heating_excess_hours=1.5, hot_water_excess_hours=0.5, hot_water_low_hours=12.25
        )
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        await coordinator.async_command("hot_water_mode", "energy_excess")
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], 2800)
        clock[0] += 600
        await coordinator.async_command("setting", ("hot_water_hysteresis", 4))
        # Preserve verified caps for the fixture's real options-listener path.
        coordinator.entry.options.update(max_start_temperature=60, max_hot_water_temperature=60)
        coordinator._pending_options = coordinator_module.normalize_options(
            coordinator.entry.options
        )
        self.assertTrue(await coordinator.async_apply_pending_options())
        for key, expected in (
            ("heating_excess_hours", 1.5),
            ("hot_water_excess_hours", 0.5),
            ("hot_water_low_hours", 12.25),
        ):
            self.assertEqual(coordinator.entry.options[key], expected)
            self.assertEqual(coordinator.engine.settings[key], expected)
        self.assertEqual(coordinator.engine.state["hot_water_excess_started_at"], 1000)
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], 2800)
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "energy_excess")
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (56, 60)
        )

    async def test_changed_duration_options_reject_fractional_hours_but_unchanged_legacy_save_is_valid(
        self,
    ):
        coordinator, device, _ = self.with_native_controls(
            {
                "heating_excess_hours": 1.5,
                "hot_water_excess_hours": 0.5,
                "hot_water_low_hours": 12.25,
                "configuration_version": 6,
            }
        )
        coordinator.async_request_refresh = AsyncMock()
        original = deepcopy(coordinator.engine.settings)
        await coordinator.async_configure_controls(original, control_changes={})
        self.assertEqual(coordinator.engine.settings, original)
        for key in ("heating_excess_hours", "hot_water_excess_hours", "hot_water_low_hours"):
            for value in (0.5, 2.5):
                if value == original[key]:
                    continue
                with self.subTest(key=key, value=value):
                    device.writes.clear()
                    with self.assertRaises(HomeAssistantError):
                        await coordinator.async_configure_controls(
                            original | {key: value}, control_changes={"heating_enabled": False}
                        )
                    self.assertEqual(coordinator.engine.settings, original)
                    self.assertFalse(device.writes)
                    coordinator._pending_options = original | {key: value}
                    with self.assertRaises(ValueError):
                        await coordinator.async_apply_pending_options()
                    self.assertEqual(coordinator.engine.settings, original)
                    self.assertFalse(device.writes)
                    coordinator._pending_options = None

    async def test_device_humidity_limit_edits_update_active_cooling_and_persist_independently(
        self,
    ):
        for preset, key, humidity, new_limit, other_key, other_limit in (
            ("normal", "cooling_humidity_limit", 81, 85, "cooling_vacation_humidity_limit", 65),
            ("vacation", "cooling_vacation_humidity_limit", 70, 75, "cooling_humidity_limit", 80),
        ):
            with self.subTest(preset=preset):
                coordinator, device, states = self.with_native_controls(
                    {
                        "inside_humidity_sensor": "sensor.rh",
                        "cooling_humidity_enabled": True,
                        "max_hot_water_temperature": 60,
                        "max_start_temperature": 60,
                    }
                )
                coordinator.async_request_refresh = AsyncMock()
                coordinator.hass.config_entries = SimpleNamespace(
                    async_update_entry=Mock(
                        side_effect=lambda entry, options: setattr(entry, "options", options)
                    )
                )
                rh = self.sensor_state(str(humidity))
                rh.attributes["unit_of_measurement"] = "%"
                states["sensor.rh"] = rh
                device.values.update(indoor_temperature=25, passive_cooling_supply_target=16)
                coordinator._values = dict(device.values)
                await coordinator.async_command("heating_mode", "cool")
                if preset == "vacation":
                    await coordinator.async_command("heating_preset", "vacation")
                original = deepcopy(coordinator.engine.state["overrides"])
                self.assertFalse(device.values["passive_cooling_enabled"])
                await coordinator.async_command("setting", (key, new_limit))
                self.assertEqual(coordinator.entry.options[key], new_limit)
                self.assertEqual(coordinator.entry.options["configuration_version"], 7)
                self.assertEqual(coordinator.engine.settings[key], new_limit)
                self.assertEqual(coordinator.engine.settings[other_key], other_limit)
                self.assertTrue(device.values["passive_cooling_enabled"])
                self.assertEqual(coordinator.engine.state["heating_preset"], preset)
                self.assertEqual(
                    coordinator.engine.cooling_humidity_info()["humidity_limit"], new_limit
                )
                self.assertEqual(
                    coordinator.engine.state["overrides"]["cooling_humidity"][
                        "passive_cooling_supply_target"
                    ],
                    original["cooling_humidity"]["passive_cooling_supply_target"],
                )
                if preset == "vacation":
                    self.assertEqual(
                        coordinator.engine.state["overrides"]["heating_profile"],
                        original["heating_profile"],
                    )
                # Exercise the real options-listener path without reloading.
                coordinator._pending_options = coordinator_module.normalize_options(
                    coordinator.entry.options
                )
                self.assertTrue(await coordinator.async_apply_pending_options())
                self.assertEqual(coordinator.engine.settings[key], new_limit)
                self.assertEqual(coordinator.engine.settings[other_key], other_limit)
                self.assertEqual(coordinator.engine.state["heating_preset"], preset)
                self.assertTrue(device.values["passive_cooling_enabled"])

    async def test_device_explicit_normal_65_is_current_choice_not_legacy_default_after_restart(
        self,
    ):
        coordinator, device, _ = self.with_native_controls(
            {
                "cooling_humidity_limit": 65,
                "configuration_version": 3,
                "sensor_timeout": 900,
                "hot_water_hysteresis": 1.5,
            }
        )
        coordinator.async_request_refresh = AsyncMock()
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        self.assertEqual(coordinator.engine.settings["cooling_humidity_limit"], 80)
        await coordinator.async_command("setting", ("cooling_humidity_limit", 65))
        normalized = coordinator_module.normalize_options(coordinator.entry.options)
        self.assertEqual(normalized["configuration_version"], 7)
        self.assertEqual(normalized["cooling_humidity_limit"], 65)
        self.assertEqual(normalized["cooling_vacation_humidity_limit"], 65)
        self.assertEqual(normalized["sensor_timeout"], 900)
        self.assertEqual(normalized["hot_water_hysteresis"], 1.5)
        restarted, restarted_device, _ = self.with_native_controls(coordinator.entry.options)
        restarted_device.values = deepcopy(device.values)
        restarted._store.async_load = AsyncMock(return_value=deepcopy(coordinator._store.saved[-1]))
        await restarted.async_load()
        self.assertEqual(restarted.engine.settings["cooling_humidity_limit"], 65)
        self.assertEqual(restarted.engine.settings["cooling_vacation_humidity_limit"], 65)
        self.assertEqual(restarted.engine.settings["configuration_version"], 7)

    async def test_device_vacation_limit_edit_migrates_old_normal_default_without_reintroducing_it(
        self,
    ):
        coordinator, _, _ = self.with_native_controls(
            {"cooling_humidity_limit": 65, "configuration_version": 2, "sensor_timeout": 900}
        )
        coordinator.async_request_refresh = AsyncMock()
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        await coordinator.async_command("setting", ("cooling_vacation_humidity_limit", 70))
        self.assertEqual(coordinator.entry.options["cooling_humidity_limit"], 80)
        self.assertEqual(coordinator.entry.options["cooling_vacation_humidity_limit"], 70)
        self.assertEqual(coordinator.entry.options["sensor_timeout"], 0)
        self.assertEqual(coordinator.entry.options["configuration_version"], 7)
        self.assertEqual(
            coordinator_module.normalize_options(coordinator.entry.options)[
                "cooling_humidity_limit"
            ],
            80,
        )

    async def test_configuration_humidity_limits_commit_failure_restores_both_and_safe_old_profile(
        self,
    ):
        coordinator, device, states = self.with_native_controls(
            {
                "inside_humidity_sensor": "sensor.rh",
                "cooling_humidity_enabled": True,
                "cooling_humidity_limit": 78,
                "cooling_vacation_humidity_limit": 67,
                "configuration_version": 5,
            }
        )
        coordinator.async_request_refresh = AsyncMock()
        rh = self.sensor_state("70")
        rh.attributes["unit_of_measurement"] = "%"
        states["sensor.rh"] = rh
        device.values.update(indoor_temperature=25, passive_cooling_supply_target=16)
        coordinator._values = dict(device.values)
        await coordinator.async_command("heating_mode", "cool")
        await coordinator.async_command("heating_preset", "vacation")
        self.assertFalse(device.values["passive_cooling_enabled"])
        old_settings = deepcopy(coordinator.engine.settings)
        original = deepcopy(coordinator.engine.state["overrides"])
        real_save = coordinator._store.async_save
        failed = False

        async def fail_commit(payload):
            nonlocal failed
            if (
                not failed
                and "configuration_restore" not in payload["control"]
                and payload["settings"]["cooling_vacation_humidity_limit"] == 74
            ):
                failed = True
                raise OSError("Limit configuration commit failed")
            await real_save(payload)

        coordinator._store.async_save = fail_commit
        with self.assertRaisesRegex(HomeAssistantError, "Limit configuration commit failed"):
            await coordinator.async_configure_controls(
                old_settings
                | {"cooling_humidity_limit": 82, "cooling_vacation_humidity_limit": 74},
                control_changes={},
            )
        self.assertTrue(failed)
        self.assertFalse(device.values["passive_cooling_enabled"])
        self.assertEqual(coordinator.engine.settings, old_settings)
        self.assertEqual(coordinator.engine.state["heating_preset"], "vacation")
        self.assertEqual(
            coordinator.engine.state["overrides"]["heating_profile"], original["heating_profile"]
        )
        self.assertEqual(device.values["passive_cooling_supply_target"], 16)
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.cooling_humidity_info()["humidity_limit"], 67)
        await coordinator._async_update_data()
        self.assertFalse(device.values["passive_cooling_enabled"])
        self.assertEqual(
            coordinator.engine.state["overrides"]["cooling_humidity"][
                "passive_cooling_supply_target"
            ],
            original["cooling_humidity"]["passive_cooling_supply_target"],
        )

    async def test_humidity_limit_options_validation_happens_before_control_changes(self):
        for key in ("cooling_humidity_limit", "cooling_vacation_humidity_limit"):
            for value in (29, 91, 65.5, True, None):
                with self.subTest(key=key, value=value):
                    coordinator, device, _ = self.with_native_controls()
                    original = dict(coordinator.engine.settings)
                    with self.assertRaises(HomeAssistantError):
                        await coordinator.async_configure_controls(
                            original | {key: value}, control_changes={"heating_enabled": False}
                        )
                    self.assertFalse(device.writes)
                    self.assertEqual(coordinator.engine.settings, original)
                    coordinator._pending_options = original | {key: value}
                    with self.assertRaises(ValueError):
                        await coordinator.async_apply_pending_options()
                    self.assertEqual(coordinator.engine.settings, original)
                    self.assertFalse(device.writes)

    async def test_device_low_setting_command_saves_options_and_updates_native_dial_without_rebasing_profile(
        self,
    ):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        await coordinator.async_command("heating_preset", "low")
        self.assertEqual(device.values["comfort_wheel"], 18)
        original = deepcopy(coordinator.engine.state["overrides"]["heating_profile"])
        await coordinator.async_command("setting", ("heating_low_offset", 3))
        self.assertEqual(coordinator.entry.options["heating_low_offset"], 3)
        self.assertEqual(coordinator.engine.settings["heating_low_offset"], 3)
        self.assertEqual(coordinator.engine.state["heating_preset"], "low")
        self.assertEqual(coordinator.engine.state["heating_target"], 20)
        self.assertEqual(device.values["comfort_wheel"], 17)
        self.assertEqual(coordinator.engine.state["overrides"]["heating_profile"], original)
        await coordinator.async_command("heating_preset", "normal")
        self.assertEqual(device.values["comfort_wheel"], 20)

    async def test_device_low_water_profile_setting_commands_save_options_and_preserve_low_timer(
        self,
    ):
        for key, value, expected in (
            ("hot_water_evening_start", 36, (36, 40)),
            ("hot_water_evening_stop", 42, (35, 42)),
        ):
            with self.subTest(key=key):
                coordinator, device, _ = self.with_native_controls()
                coordinator.async_request_refresh = AsyncMock()
                coordinator.hass.config_entries = SimpleNamespace(
                    async_update_entry=Mock(
                        side_effect=lambda entry, options: setattr(entry, "options", options)
                    )
                )
                clock = [1000.0]
                coordinator.engine.now = lambda: clock[0]
                await coordinator.async_command("hot_water_mode", "evening")
                original = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
                started = coordinator.engine.state["hot_water_low_started_at"]
                deadline = coordinator.engine.state["hot_water_low_deadline"]
                clock[0] += 3600
                await coordinator.async_command("setting", (key, value))
                self.assertEqual(coordinator.entry.options[key], value)
                self.assertEqual(coordinator.engine.settings[key], value)
                self.assertEqual(
                    (device.values["hot_water_start"], device.values["hot_water_stop"]), expected
                )
                self.assertEqual(coordinator.engine.state["hot_water_mode"], "evening")
                self.assertEqual(coordinator.engine.state["hot_water_low_started_at"], started)
                self.assertEqual(coordinator.engine.state["hot_water_low_deadline"], deadline)
                self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original)
                await coordinator.async_command("hot_water_mode", "auto")
                self.assertEqual(
                    (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
                )

    async def test_failed_excess_off_native_restore_is_retried_as_off_without_reviving_charge(self):
        for key in ("comfort_wheel", "heating_season_stop"):
            with self.subTest(key=key):
                coordinator, device, _ = self.with_charge_controls()
                coordinator.engine.settings["max_heating_temperature"] = 26
                await coordinator.async_command("heating_preset", "pv_charge")
                original_write = device.async_write
                failed = False

                async def fail_original_restore(register, value):
                    nonlocal failed
                    if (
                        register == key
                        and value == (20 if key == "comfort_wheel" else 17)
                        and not failed
                    ):
                        failed = True
                        raise OSError("Original native setting could not be confirmed")
                    return await original_write(register, value)

                device.async_write = fail_original_restore
                device.writes.clear()
                with self.assertRaises(HomeAssistantError):
                    await coordinator.async_command("heating_mode", "off")
                self.assertEqual(coordinator.engine.state["heating_mode"], "off")
                self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
                self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
                self.assertFalse(device.values["heating_enabled"])
                self.assertFalse(device.values["passive_cooling_enabled"])
                await coordinator._async_update_data()
                self.assertEqual(device.values["comfort_wheel"], 20)
                self.assertEqual(device.values["heating_season_stop"], 17)
                self.assertFalse(device.values["heating_enabled"])
                self.assertFalse(device.values["passive_cooling_enabled"])
                self.assertEqual(coordinator.engine.state["heating_mode"], "off")
                self.assertNotIn(("heating_enabled", True), device.writes)
                self.assertNotIn(("passive_cooling_enabled", True), device.writes)

    async def test_expired_configuration_restore_prioritizes_dial_heat_stop_and_off_before_unrelated_read_failure(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        device.values.update(
            comfort_wheel=22.34, heating_season_stop=17.23, min_supply_temperature=17
        )
        coordinator._values = dict(device.values)
        coordinator.engine.state["heating_target"] = 22.34
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        coordinator.engine.settings.update(max_heating_temperature=26, heating_excess_hours=0.1)
        await coordinator.async_command("heating_mode", "off")
        await coordinator.async_command("heating_preset", "pv_charge")
        self.assertAlmostEqual(device.values["heating_season_stop"], 19.23)
        self.assertEqual(device.values["comfort_wheel"], 26)
        original_write = device.async_write
        original_read = device.async_read_value
        blocked = [False]

        async def applied_but_unconfirmed(key, value):
            result = await original_write(key, value)
            if key == "anti_legionella_enabled" and value is False and not blocked[0]:
                blocked[0] = True
                raise OSError("Programme confirmation was lost")
            return result

        async def unrelated_failed_read(key):
            if blocked[0] and key == "max_supply_temperature":
                raise OSError("Unrelated maximum supply read unavailable")
            return await original_read(key)

        device.async_write = applied_but_unconfirmed
        device.async_read_value = unrelated_failed_read
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={"anti_legionella_enabled": False},
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        clock[0] = 1400
        device.writes.clear()
        await coordinator._async_update_data()
        self.assertIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(device.values["comfort_wheel"], 22.34)
        self.assertAlmostEqual(device.values["heating_season_stop"], 17.23)
        self.assertFalse(device.values["fixed_supply_enabled"])
        self.assertEqual(device.values["fixed_supply_target"], 30)
        self.assertFalse(device.values["heating_enabled"])
        self.assertFalse(device.values["passive_cooling_enabled"])
        self.assertNotIn(("heating_enabled", True), device.writes)
        backup = coordinator.engine.state["configuration_restore"]
        self.assertEqual(backup["native"]["comfort_wheel"], 22.34)
        self.assertAlmostEqual(backup["native"]["heating_season_stop"], 17.23)
        blocked[0] = False
        device.async_write = original_write
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.state["heating_mode"], "off")
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")

    async def test_generic_setting_command_saves_options_and_updates_excess_native_target_without_restarting_clock(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.hass.config_entries = SimpleNamespace(
            async_update_entry=Mock(
                side_effect=lambda entry, options: setattr(entry, "options", options)
            )
        )
        await coordinator.async_command("heating_preset", "pv_charge")
        deadline = coordinator.engine.state["heating_excess_deadline"]
        original = deepcopy(coordinator.engine.state["overrides"]["heating"])
        await coordinator.async_command("setting", ("max_heating_temperature", 26))
        self.assertEqual(device.values["comfort_wheel"], 26)
        self.assertEqual(coordinator.entry.options["max_heating_temperature"], 26)
        await coordinator.async_command("setting", ("heating_excess_heat_stop_offset", 5))
        self.assertEqual(device.values["heating_season_stop"], 22)
        self.assertEqual(coordinator.entry.options["heating_excess_heat_stop_offset"], 5)
        self.assertEqual(coordinator.engine.state["heating_excess_deadline"], deadline)
        self.assertEqual(coordinator.engine.state["overrides"]["heating"], original)
        for key, invalid in (
            ("max_heating_temperature", 26.5),
            ("heating_excess_heat_stop_offset", 2.5),
        ):
            before = deepcopy(coordinator.entry.options)
            device.writes.clear()
            with self.assertRaises(HomeAssistantError):
                await coordinator.async_command("setting", (key, invalid))
            self.assertEqual(device.writes, [])
            self.assertEqual(coordinator.entry.options, before)

    async def test_configure_active_excess_rejects_heat_stop_offset_above_cap_before_writes(self):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("heating_preset", "pv_charge")
        original_settings = dict(coordinator.engine.settings)
        original = deepcopy(coordinator.engine.state)
        device.writes.clear()
        with self.assertRaisesRegex(HomeAssistantError, "40"):
            await coordinator.async_configure_controls(
                {**original_settings, "heating_excess_heat_stop_offset": 24}, control_changes={}
            )
        self.assertEqual(device.writes, [])
        self.assertEqual(coordinator.engine.settings, original_settings)
        self.assertEqual(coordinator.engine.state["overrides"], original["overrides"])
        self.assertEqual(
            coordinator.engine.state["heating_excess_deadline"], original["heating_excess_deadline"]
        )

    async def test_cool_rejects_heating_only_presets_without_native_writes(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("heating_mode", "cool")
        for preset in ("low", "pv_charge"):
            device.writes.clear()
            with self.assertRaisesRegex(HomeAssistantError, "Cool"):
                await coordinator.async_command("heating_preset", preset)
            self.assertEqual(device.writes, [])
            self.assertEqual(coordinator.engine.state["heating_mode"], "cool")

    async def test_configure_active_heating_profile_applies_without_rebasing_normal_journal(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("heating_preset", "low")
        original = deepcopy(coordinator.engine.state["overrides"]["heating_profile"])
        self.assertEqual(device.values["comfort_wheel"], 18)
        await coordinator.async_configure_controls(
            {**coordinator.engine.settings, "heating_low_offset": 3}, control_changes={}
        )
        self.assertEqual(device.values["comfort_wheel"], 17)
        self.assertEqual(coordinator.engine.state["overrides"]["heating_profile"], original)
        self.assertEqual(coordinator.engine.state["heating_target"], 20)
        await coordinator.async_command("heating_preset", "normal")
        self.assertEqual(device.values["comfort_wheel"], 20)

    async def test_configure_failed_profile_edit_restores_previous_effective_target_and_preferences(
        self,
    ):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("heating_preset", "low")
        original = deepcopy(coordinator.engine.state["overrides"]["heating_profile"])
        old_settings = deepcopy(coordinator.engine.settings)
        write = device.async_write
        failed = False

        async def lost_target(key, value):
            nonlocal failed
            result = await write(key, value)
            if key == "comfort_wheel" and value == 17 and not failed:
                failed = True
                raise OSError("Applied profile target was not confirmed")
            return result

        device.async_write = lost_target
        with self.assertRaisesRegex(HomeAssistantError, "not confirmed"):
            await coordinator.async_configure_controls(
                {**old_settings, "heating_low_offset": 3}, control_changes={}
            )
        self.assertEqual(device.values["comfort_wheel"], 18)
        self.assertEqual(coordinator.engine.settings, old_settings)
        self.assertEqual(coordinator.engine.state["heating_preset"], "low")
        self.assertEqual(coordinator.engine.state["overrides"]["heating_profile"], original)
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_configure_low_duration_and_profile_edit_does_not_restart_clock(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        await coordinator.async_command("hot_water_mode", "evening")
        clock[0] += 3600
        await coordinator.async_configure_controls(
            {
                **coordinator.engine.settings,
                "hot_water_evening_start": 36,
                "hot_water_evening_stop": 42,
                "hot_water_low_hours": 2,
            },
            control_changes={},
        )
        self.assertEqual(coordinator.engine.state["hot_water_low_started_at"], 1000)
        self.assertEqual(coordinator.engine.state["hot_water_low_deadline"], 8200)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (36, 42)
        )
        clock[0] = 8300
        await coordinator._async_update_data()
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )

    async def test_low_expiry_prioritized_over_unrelated_configuration_restore_failure(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        device.values["hot_water_boost"] = False
        coordinator._values["hot_water_boost"] = False
        coordinator.engine.settings["hot_water_low_hours"] = 0.1
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        await coordinator.async_command("hot_water_mode", "evening")
        write = device.async_write
        blocked = [True]

        async def programme_failure(key, value):
            if blocked[0] and key == "anti_legionella_enabled":
                if not value:
                    await write(key, value)
                raise OSError("Unrelated programme confirmation unavailable")
            return await write(key, value)

        device.async_write = programme_failure
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={"anti_legionella_enabled": False},
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        clock[0] = 1400
        await coordinator._async_update_data()
        self.assertIn("configuration_restore", coordinator.engine.state)
        self.assertTrue(coordinator.engine.state["configuration_restore"]["expired_low_mode"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        blocked[0] = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertIsNone(coordinator.engine.state["hot_water_low_deadline"])

    async def test_selected_humidity_raw_diagnostics_and_failed_poll_refresh_are_current(self):
        coordinator, device, states = self.with_native_controls(
            {"inside_humidity_sensor": "sensor.rh", "cooling_humidity_enabled": True}
        )
        coordinator.async_request_refresh = AsyncMock()
        rh = self.sensor_state("60")
        rh.attributes["unit_of_measurement"] = "%"
        states["sensor.rh"] = rh
        await coordinator._async_update_data()
        self.assertEqual(coordinator.effective_inside_humidity, 60)
        rh.state = "101"

        async def poll_failure():
            raise OSError("Pump offline")

        device.async_update = poll_failure
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        details = coordinator.humidity_source_details()
        self.assertEqual(details["selected_sensor_state"], "101")
        self.assertEqual(details["selected_sensor_unit"], "%")
        self.assertIsNone(details["external_value"])
        self.assertEqual(coordinator.engine.cooling_humidity_info()["status"], "missing_humidity")

    async def test_configuration_humidity_toggle_restores_original_before_cooling_permission(self):
        coordinator, device, states = self.with_native_controls(
            {"inside_humidity_sensor": "sensor.rh", "cooling_humidity_enabled": True}
        )
        coordinator.async_request_refresh = AsyncMock()
        rh = self.sensor_state("60")
        rh.attributes["unit_of_measurement"] = "%"
        states["sensor.rh"] = rh
        device.values.update(indoor_temperature=28, passive_cooling_supply_target=16.34)
        coordinator._values = dict(device.values)
        await coordinator.async_command("heating_mode", "cool")
        self.assertGreater(device.values["passive_cooling_supply_target"], 16.34)
        device.writes.clear()
        await coordinator.async_configure_controls(
            {**coordinator.engine.settings, "cooling_humidity_enabled": False}, control_changes={}
        )
        self.assertEqual(device.values["passive_cooling_supply_target"], 16.34)
        self.assertTrue(device.values["passive_cooling_enabled"])
        target_index = device.writes.index(("passive_cooling_supply_target", 16.34))
        self.assertEqual(device.writes[target_index - 1], ("passive_cooling_enabled", False))
        self.assertGreater(device.writes.index(("passive_cooling_enabled", True)), target_index)
        self.assertNotIn("cooling_humidity", coordinator.engine.state["overrides"])

    def make_coordinator(self, options=None):
        states = {}
        hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
        entry = SimpleNamespace(entry_id="test_pump", options=options or {})
        device = Device()
        coordinator = coordinator_module.ThermiaCoordinator(hass, entry, device)
        coordinator._recover_pending = False
        return coordinator, device, states

    def sensor_state(self, value, *, age=0):
        now = datetime.now(timezone.utc)
        return SimpleNamespace(
            state=value,
            attributes={"unit_of_measurement": "°C"},
            last_updated=now - timedelta(seconds=age),
            last_reported=now - timedelta(seconds=age),
        )

    def with_native_controls(self, options=None):
        coordinator, device, states = self.make_coordinator(options)
        device.values = {
            "main_demand": 99,
            "active_demand_flags": 1 << 10,
            "heating_enabled": True,
            "hot_water_enabled": True,
            "passive_cooling_enabled": False,
            "comfort_wheel": 20,
            "heating_season_stop": 17,
            "hot_water_start": 45,
            "hot_water_stop": 50,
            "hot_water_weighted_temperature": 47,
            "hot_water_top_temperature": 49,
            "immersion_heater": 2,
            "anti_legionella_enabled": True,
            "indoor_temperature": 20,
            "outdoor_temperature": 5,
        }
        coordinator._values = dict(device.values)
        coordinator.engine.settings.update(
            max_hot_water_temperature=60,
            max_start_temperature=60,
        )
        coordinator.engine._hydrate()
        return coordinator, device, states

    def with_charge_controls(self):
        coordinator, device, states = self.with_native_controls()
        device.values.update(
            hot_water_boost=False,
            fixed_supply_enabled=False,
            fixed_supply_target=30,
            max_supply_temperature=40,
            smart_grid_request=0,
        )
        coordinator._values = dict(device.values)
        coordinator.engine.now = lambda: 1000.0
        coordinator.async_request_refresh = AsyncMock()
        return coordinator, device, states

    async def test_temperature_edits_wait_for_confirmed_readback_and_share_the_command_lock(self):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("heating_preset", "pv_charge")
        device.writes.clear()
        confirmed = asyncio.Event()
        release_readback = asyncio.Event()
        original_write = device.async_write

        async def wait_for_confirmation(key, value):
            self.assertTrue(coordinator._lock.locked())
            result = await original_write(key, value)
            if key == "comfort_wheel" and value == 22:
                journal = coordinator._store.saved[-1]["control"]["overrides"][
                    "heating_target_edit"
                ]
                self.assertEqual(journal["comfort_wheel"], 20)
                self.assertEqual(journal["policy"]["heating_preset"], "normal")
                confirmed.set()
                await release_readback.wait()
            return result

        device.async_write = wait_for_confirmation
        first = asyncio.create_task(
            coordinator.async_command("heating_temperature_edit", {"temperature": 22})
        )
        second = None
        try:
            await asyncio.wait_for(confirmed.wait(), 2)
            self.assertEqual(device.values["comfort_wheel"], 22)
            self.assertEqual(coordinator.value("comfort_wheel"), 20)
            self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
            second = asyncio.create_task(
                coordinator.async_command("heating_temperature_edit", {"temperature": 23})
            )
            await asyncio.sleep(0)
            self.assertNotIn(("comfort_wheel", 23), device.writes)
        finally:
            release_readback.set()
            await first
            if second is not None:
                await second
        self.assertEqual(coordinator.value("comfort_wheel"), 23)
        self.assertEqual(device.values["comfort_wheel"], 23)
        self.assertEqual(coordinator.engine.state["heating_target"], 23)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertFalse(device.values["fixed_supply_enabled"])
        self.assertIsNone(coordinator.engine.state["heating_excess_started_at"])
        self.assertNotIn("heating_target_edit", coordinator.engine.state["overrides"])

    async def test_heating_edit_preserves_displayed_heat_after_excess_from_off(self):
        for original_mode in ("off",):
            with self.subTest(original_mode=original_mode):
                coordinator, device, _ = self.with_charge_controls()
                await coordinator.async_command("heating_mode", original_mode)
                await coordinator.async_command("heating_preset", "pv_charge")
                self.assertEqual(coordinator.engine.state["heating_mode"], "heat")
                await coordinator.async_command("heating_temperature_edit", {"temperature": 22})
                self.assertEqual(coordinator.engine.state["heating_mode"], "heat")
                self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
                self.assertEqual(device.values["comfort_wheel"], 22)
                self.assertTrue(device.values["heating_enabled"])
                self.assertFalse(device.values["passive_cooling_enabled"])
                self.assertFalse(device.values["fixed_supply_enabled"])
                self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])

    async def test_water_edit_uses_refreshed_native_display_to_keep_untouched_normal_side(self):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original_deadline = coordinator.engine.state["hot_water_excess_deadline"]
        coordinator.engine.settings["hot_water_hysteresis"] = 4
        await coordinator._async_update_data()
        self.assertEqual(
            (coordinator.value("hot_water_start"), coordinator.value("hot_water_stop")), (56, 60)
        )
        device.writes.clear()
        await coordinator.async_command(
            "hot_water_temperature_edit",
            {
                "target_temp_low": 56,
                "target_temp_high": 60,
            },
        )
        self.assertEqual(device.writes, [])
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], original_deadline)
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "energy_excess")
        await coordinator.async_command(
            "hot_water_temperature_edit",
            {
                "target_temp_low": 56,
                "target_temp_high": 59,
            },
        )
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 59)
        )
        self.assertEqual(
            (coordinator.value("hot_water_start"), coordinator.value("hot_water_stop")), (45, 59)
        )
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertFalse(device.values["hot_water_boost"])
        self.assertIsNone(coordinator.engine.state["hot_water_excess_started_at"])
        self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
        self.assertNotIn("hot_water", coordinator.engine.state["overrides"])
        self.assertNotIn("water_temperature_edit", coordinator.engine.state["overrides"])

    async def test_low_mode_edit_and_normal_off_edit_keep_the_requested_permission(self):
        for initial_mode, requested_off, expected_mode in (
            ("evening", False, "auto"),
            ("evening", True, "off"),
            ("off", False, "off"),
        ):
            with self.subTest(initial_mode=initial_mode, requested_off=requested_off):
                coordinator, device, _ = self.with_charge_controls()
                await coordinator.async_command("hot_water_mode", initial_mode)
                payload = {
                    "target_temp_low": 36 if initial_mode == "evening" else 45,
                    "target_temp_high": 40 if initial_mode == "evening" else 55,
                }
                if requested_off:
                    payload["hvac_mode"] = "off"
                await coordinator.async_command("hot_water_temperature_edit", payload)
                expected_pair = (36, 50) if initial_mode == "evening" else (45, 55)
                self.assertEqual(
                    (device.values["hot_water_start"], device.values["hot_water_stop"]),
                    expected_pair,
                )
                self.assertEqual(coordinator.engine.state["hot_water_mode"], expected_mode)
                self.assertEqual(device.values["hot_water_enabled"], expected_mode != "off")
                self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
                self.assertFalse(device.values["hot_water_boost"])

    async def test_failed_heating_confirmation_restores_original_dial_and_cancels_excess(self):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("heating_preset", "pv_charge")
        physical_comfort = 20
        original_write = device.async_write
        original_read = device.async_read_value
        lost_readback = False

        async def lose_target_confirmation(key, value):
            nonlocal physical_comfort, lost_readback
            result = await original_write(key, value)
            if key == "comfort_wheel":
                physical_comfort = value
                if value == 22 and not lost_readback:
                    lost_readback = True
                    device.values[key] = None
                    raise OSError("Pump applied the target but its readback was lost")
            return result

        async def read_physical_value(key):
            return physical_comfort if key == "comfort_wheel" else await original_read(key)

        device.async_write = lose_target_confirmation
        device.async_read_value = AsyncMock(side_effect=read_physical_value)
        with self.assertRaisesRegex(HomeAssistantError, "readback was lost"):
            await coordinator.async_command("heating_temperature_edit", {"temperature": 22})
        self.assertTrue(lost_readback)
        self.assertEqual(physical_comfort, 20)
        self.assertEqual(coordinator.value("comfort_wheel"), 20)
        device.async_read_value.assert_any_await("comfort_wheel")
        self.assertEqual(coordinator.engine.state["heating_target"], 20)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertFalse(device.values["fixed_supply_enabled"])
        self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
        self.assertEqual(coordinator._store.saved[-1]["control"]["heating_preset"], "normal")
        writes_after_recovery = list(device.writes)
        await coordinator._async_update_data()
        self.assertEqual(device.writes, writes_after_recovery)

    async def test_failed_water_edit_keeps_durable_normal_and_boost_off_until_restart_recovery(
        self,
    ):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original_write = device.async_write
        broken = False

        async def fail_new_stop_and_rollback(key, value):
            nonlocal broken
            if broken and key == "hot_water_stop" and value == 50:
                raise OSError("Normal STOP restoration is not confirmed")
            result = await original_write(key, value)
            if key == "hot_water_stop" and value == 59 and not broken:
                broken = True
                raise OSError("The new STOP was applied without confirmation")
            return result

        device.async_write = fail_new_stop_and_rollback
        with self.assertRaisesRegex(HomeAssistantError, "without confirmation"):
            await coordinator.async_command(
                "hot_water_temperature_edit",
                {
                    "target_temp_low": 58,
                    "target_temp_high": 59,
                },
            )
        durable = deepcopy(coordinator._store.saved[-1])
        journal = durable["control"]["overrides"]["water_temperature_edit"]
        self.assertEqual((journal["hot_water_start"], journal["hot_water_stop"]), (45, 50))
        self.assertFalse(journal["hot_water_boost"])
        self.assertEqual(journal["policy"]["hot_water_mode"], "auto")
        self.assertFalse(journal["policy"]["boost_enabled"])
        self.assertIn("water_temperature_edit", durable["control"]["pending_restore"])
        self.assertIsNone(durable["control"]["hot_water_excess_deadline"])
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertFalse(device.values["hot_water_boost"])
        self.assertFalse(device.values["hot_water_enabled"])
        await coordinator._async_update_data()
        self.assertIn("water_temperature_edit", coordinator.engine.state["pending_restore"])
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")

        broken = False
        device.async_write = original_write
        restarted = coordinator_module.ThermiaCoordinator(
            coordinator.hass, coordinator.entry, device
        )
        restarted._store.async_load = AsyncMock(return_value=durable)
        await restarted.async_load()
        restarted.engine.now = lambda: 100000.0
        await restarted._async_update_data()
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(restarted.engine.state["hot_water_mode"], "auto")
        self.assertIsNone(restarted.engine.state["hot_water_excess_deadline"])
        self.assertEqual(restarted.engine.state["overrides"], {})
        writes_after_recovery = list(device.writes)
        await restarted._async_update_data()
        self.assertEqual(device.writes, writes_after_recovery)

    async def test_water_edit_commit_storage_error_restores_normal_without_resuming_excess(self):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original_save = coordinator._store.async_save
        failed = False

        async def fail_final_commit(payload):
            nonlocal failed
            if (
                not failed
                and device.values["hot_water_stop"] == 59
                and "water_temperature_edit" not in payload["control"]["overrides"]
            ):
                failed = True
                raise OSError("Edited thermostat state could not be committed")
            await original_save(payload)

        coordinator._store.async_save = fail_final_commit
        with self.assertRaisesRegex(HomeAssistantError, "could not be committed"):
            await coordinator.async_command(
                "hot_water_temperature_edit",
                {
                    "target_temp_low": 58,
                    "target_temp_high": 59,
                },
            )
        self.assertTrue(failed)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertEqual(
            (coordinator.value("hot_water_start"), coordinator.value("hot_water_stop")), (45, 50)
        )
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertFalse(device.values["hot_water_boost"])
        self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
        saved = coordinator._store.saved[-1]["control"]
        self.assertEqual((saved["hot_water_start"], saved["hot_water_target"]), (45, 50))
        self.assertFalse(saved["boost_enabled"])
        self.assertEqual(saved["overrides"], {})

    async def test_failed_normal_water_edit_preserves_front_panel_permission_and_boost(self):
        coordinator, device, _ = self.with_charge_controls()
        await coordinator.async_command("hot_water_mode", "auto")
        # A front-panel edit can change native flags while the HA thermostat
        # retains its selected Normal mode. Rollback must preserve that readback.
        device.values.update(hot_water_enabled=False, hot_water_boost=True)
        await coordinator._async_update_data()
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        original_write = device.async_write
        failed = False

        async def applied_start_without_confirmation(key, value):
            nonlocal failed
            result = await original_write(key, value)
            if key == "hot_water_start" and value == 44 and not failed:
                failed = True
                raise OSError("Native START readback was lost")
            return result

        device.async_write = applied_start_without_confirmation
        with self.assertRaisesRegex(HomeAssistantError, "START readback was lost"):
            await coordinator.async_command(
                "hot_water_temperature_edit",
                {
                    "target_temp_low": 44,
                    "target_temp_high": 50,
                },
            )
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertFalse(device.values["hot_water_enabled"])
        self.assertTrue(device.values["hot_water_boost"])
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertEqual(coordinator.engine.state["overrides"], {})

    async def test_water_edit_preserves_heating_sg_without_enabling_water_above_new_stop(self):
        coordinator, device, _ = self.with_charge_controls()
        coordinator.engine.settings["smart_grid_mode"] = "sg_ready"
        await coordinator.async_command("heating_preset", "pv_charge")
        await coordinator.async_command("hot_water_mode", "energy_excess")
        device.values.update(hot_water_top_temperature=59, hot_water_weighted_temperature=57)
        await coordinator._async_update_data()
        original_write = device.async_write
        intermediate = []

        async def record_native_permissions(key, value):
            result = await original_write(key, value)
            intermediate.append(
                (
                    key,
                    value,
                    device.values["smart_grid_request"],
                    device.values["hot_water_enabled"],
                )
            )
            return result

        device.async_write = record_native_permissions
        await coordinator.async_command(
            "hot_water_temperature_edit",
            {
                "target_temp_low": 58,
                "target_temp_high": 58,
            },
        )
        self.assertTrue(intermediate)
        self.assertEqual(intermediate[0][:2], ("hot_water_enabled", False))
        self.assertFalse(any(sg == 3 and enabled for _, _, sg, enabled in intermediate))
        self.assertEqual(coordinator.engine.state["sg_owners"], ["heating"])
        self.assertEqual(coordinator.engine.state["heating_preset"], "pv_charge")
        self.assertEqual(device.values["smart_grid_request"], 3)
        self.assertFalse(device.values["hot_water_enabled"])
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 58)
        )
        await coordinator.async_command("heating_preset", "normal")
        self.assertEqual(device.values["smart_grid_request"], 0)
        self.assertTrue(device.values["hot_water_enabled"])
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertEqual(coordinator.engine.state["sg_owners"], [])

    async def test_first_outdoor_sample_initializes_supported_missing_bms_value(self):
        coordinator, device, states = self.make_coordinator({"outside_sensor": "sensor.garden"})
        states["sensor.garden"] = self.sensor_state("12.34")
        device.values = {
            "outdoor_source": 0,
            "bms_outdoor_temperature": None,
            "outdoor_temperature": 4.5,
        }
        coordinator._values = dict(device.values)
        await coordinator._async_outdoor_feed()
        self.assertEqual(device.writes, [("bms_outdoor_temperature", 12.34), ("outdoor_source", 1)])
        self.assertEqual(coordinator.engine.state["outdoor_source_snapshot"], 0)
        self.assertEqual(coordinator.effective_outside, 12.34)

    async def test_lost_comfort_readback_can_refresh_before_recovery_write(self):
        coordinator, device, _ = self.with_native_controls()
        physical_comfort = 23
        lose_confirmation = True

        async def write_comfort(key, value):
            nonlocal physical_comfort, lose_confirmation
            self.assertEqual(key, "comfort_wheel")
            device.writes.append((key, value))
            physical_comfort = value
            if lose_confirmation:
                lose_confirmation = False
                device.values[key] = None
                raise ValueError("Comfort write reached pump but confirmation was lost")
            device.values[key] = value
            return value

        async def read_comfort(key):
            self.assertEqual(key, "comfort_wheel")
            return physical_comfort

        device.async_write = write_comfort
        device.async_read_value = AsyncMock(side_effect=read_comfort)
        with self.assertRaisesRegex(ValueError, "confirmation was lost"):
            await coordinator._async_write("comfort_wheel", 22)
        self.assertIsNone(coordinator.value("comfort_wheel"))
        self.assertEqual(physical_comfort, 22)
        await coordinator._async_write("comfort_wheel", 23)
        device.async_read_value.assert_awaited_once_with("comfort_wheel")
        self.assertEqual(physical_comfort, 23)
        self.assertEqual(coordinator.value("comfort_wheel"), 23)
        self.assertEqual(device.writes, [("comfort_wheel", 22), ("comfort_wheel", 23)])

    async def test_configuration_commit_storage_failure_restores_confirmed_native_change(self):
        coordinator, device, _ = self.with_native_controls()
        original_settings = dict(coordinator.engine.settings)
        original_policy = deepcopy(coordinator.engine.state)
        options = original_settings | {"hysteresis": 1}
        original_save = coordinator._store.async_save
        failed = False

        async def fail_commit(payload):
            nonlocal failed
            if (
                not failed
                and "configuration_restore" not in payload["control"]
                and device.values["anti_legionella_enabled"] is False
            ):
                failed = True
                raise OSError("Final configuration save failed")
            await original_save(payload)

        coordinator._store.async_save = fail_commit
        with self.assertRaisesRegex(HomeAssistantError, "Final configuration save failed"):
            await coordinator.async_configure_controls(
                options, control_changes={"anti_legionella_enabled": False}
            )

        self.assertTrue(failed)
        self.assertTrue(device.values["anti_legionella_enabled"])
        self.assertEqual(coordinator.engine.settings, original_settings)
        self.assertEqual(coordinator.entry.options, {})
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        for key, value in original_policy.items():
            if key != "control_warning":
                self.assertEqual(coordinator.engine.state[key], value, key)
        self.assertEqual(
            device.writes,
            [("anti_legionella_enabled", False), ("anti_legionella_enabled", True)],
        )
        # The snapshot written before touching hardware stays intact after
        # the live policy removes its journal at commit and recovery.
        first_saved = coordinator._store.saved[0]["control"]["configuration_restore"]
        self.assertTrue(first_saved["native"]["anti_legionella_enabled"])
        self.assertEqual(first_saved["settings"], original_settings)

    async def test_failed_commit_and_recovery_save_keep_journal_until_storage_recovers(self):
        coordinator, device, _ = self.with_native_controls()
        original_settings = dict(coordinator.engine.settings)
        original_save = coordinator._store.async_save
        storage_failed = False

        async def fail_commit_and_recovery(payload):
            nonlocal storage_failed
            if (
                "configuration_restore" not in payload["control"]
                and device.values["anti_legionella_enabled"] is False
            ):
                storage_failed = True
            if storage_failed:
                raise OSError("Configuration storage is unavailable")
            await original_save(payload)

        coordinator._store.async_save = fail_commit_and_recovery
        with self.assertRaisesRegex(HomeAssistantError, "Configuration storage is unavailable"):
            await coordinator.async_configure_controls(
                original_settings | {"hysteresis": 1},
                control_changes={"anti_legionella_enabled": False},
            )

        self.assertTrue(device.values["anti_legionella_enabled"])
        self.assertEqual(coordinator.engine.settings, original_settings)
        backup = coordinator.engine.state["configuration_restore"]
        self.assertEqual(backup["settings"], original_settings)
        self.assertTrue(backup["native"]["anti_legionella_enabled"])
        durable_backup = coordinator._store.saved[-1]["control"]["configuration_restore"]
        self.assertEqual(backup, durable_backup)

        # A restart reads the already durable journal, even though neither the
        # failed commit nor the completed hardware rollback could be saved.
        restarted, restarted_device, _ = self.with_native_controls()
        restarted_device.values = deepcopy(device.values)
        restarted._store.async_load = AsyncMock(return_value=deepcopy(coordinator._store.saved[-1]))
        await restarted.async_load()
        restarted._recover_pending = True
        await restarted._async_update_data()
        self.assertEqual(restarted.engine.settings, original_settings)
        self.assertNotIn("configuration_restore", restarted.engine.state)
        self.assertTrue(restarted_device.values["anti_legionella_enabled"])

        coordinator.engine.tick = AsyncMock()
        await coordinator._async_update_data()
        coordinator.engine.tick.assert_not_awaited()
        self.assertIn("configuration_restore", coordinator.engine.state)

        storage_failed = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        coordinator.engine.tick.assert_awaited_once()
        self.assertTrue(device.values["anti_legionella_enabled"])

    async def test_configuration_rollback_restores_comfort_before_space_permissions(self):
        coordinator, device, _ = self.with_native_controls()
        policy = deepcopy(coordinator.engine.state)
        policy["heating_target"] = 23
        coordinator.engine.state["configuration_restore"] = {
            "native": {
                "heating_enabled": True,
                "passive_cooling_enabled": False,
                "comfort_wheel": 23,
            },
            "state": policy,
            "settings": dict(coordinator.engine.settings),
            "thermal_action": "idle",
        }
        device.values.update(heating_enabled=False, passive_cooling_enabled=True, comfort_wheel=22)
        coordinator._values = dict(device.values)
        await coordinator._async_save(coordinator.engine.state)

        self.assertTrue(await coordinator._async_restore_configuration())
        self.assertEqual(device.values["comfort_wheel"], 23)
        self.assertTrue(device.values["heating_enabled"])
        self.assertFalse(device.values["passive_cooling_enabled"])
        self.assertLess(
            device.writes.index(("comfort_wheel", 23)),
            device.writes.index(("heating_enabled", True)),
        )
        self.assertEqual(coordinator.engine.state["heating_target"], 23)
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_refresh_does_not_allow_missing_or_unsupported_comfort_writes(self):
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported):
                coordinator, device, _ = self.with_native_controls()
                coordinator._values["comfort_wheel"] = None
                device.values["comfort_wheel"] = None
                device.async_read_value = AsyncMock(return_value=None)
                if unsupported:
                    device.unsupported.add("comfort_wheel")
                with self.assertRaisesRegex(ValueError, "valid comfort_wheel"):
                    await coordinator._async_write("comfort_wheel", 22)
                if unsupported:
                    device.async_read_value.assert_not_awaited()
                else:
                    device.async_read_value.assert_awaited_once_with("comfort_wheel")
                self.assertEqual(device.writes, [])

    async def test_thermostat_journal_storage_error_reaches_user_as_home_assistant_error(self):
        coordinator, _, _ = self.with_native_controls()
        coordinator.engine.command = AsyncMock(
            side_effect=OSError("Could not save thermostat recovery values")
        )
        coordinator.engine.tick = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()
        with self.assertRaisesRegex(HomeAssistantError, "Could not save thermostat recovery"):
            await coordinator.async_command("heating_target", 22)
        coordinator.engine.tick.assert_not_awaited()
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_stale_external_outdoor_input_switches_and_refreshes_physical_sensor(self):
        coordinator, device, states = self.make_coordinator(
            {"outside_sensor": "sensor.garden", "sensor_timeout": 900, "configuration_version": 3}
        )
        states["sensor.garden"] = self.sensor_state("18", age=901)
        device.values = {
            "outdoor_source": 1,
            "bms_outdoor_temperature": 18,
            "outdoor_temperature": 18,
        }
        coordinator._values = dict(device.values)
        coordinator.engine.state["outdoor_source_snapshot"] = 0
        await coordinator._async_outdoor_feed()
        self.assertEqual(device.writes, [("outdoor_source", 0)])
        self.assertEqual(coordinator.effective_outside, 4.5)
        self.assertEqual(coordinator.outside_source, "Thermia physical outside sensor")

    async def test_poll_decode_failure_cannot_reuse_cached_data_as_success(self):
        coordinator, device, _ = self.make_coordinator()
        coordinator._values = {"main_demand": 4, "outdoor_temperature": 9}
        device.async_update = AsyncMock(
            side_effect=ValueError("Short response for outdoor_temperature")
        )
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()

    async def test_control_failure_after_good_poll_preserves_new_monitoring_data(self):
        coordinator, device, _ = self.make_coordinator()
        device.values = {"main_demand": 99, "outdoor_temperature": 7.25}
        coordinator.engine.tick = AsyncMock(
            side_effect=ValueError("Charging limit is not configured")
        )
        result = await coordinator._async_update_data()
        self.assertEqual(result["outdoor_temperature"], 7.25)
        self.assertEqual(result["main_demand"], 99)
        self.assertEqual(
            coordinator.engine.state["control_warning"], "Charging limit is not configured"
        )

    async def test_genuinely_unsupported_bms_temperature_never_gets_written(self):
        coordinator, device, states = self.make_coordinator({"outside_sensor": "sensor.garden"})
        states["sensor.garden"] = self.sensor_state("10")
        device.values = {"outdoor_source": 0, "bms_outdoor_temperature": None}
        device.unsupported.add("bms_outdoor_temperature")
        coordinator._values = dict(device.values)
        await coordinator._async_outdoor_feed()
        self.assertEqual(device.writes, [])
        self.assertIn("unsupported", coordinator.engine.state["control_warning"])

    async def test_stable_unitless_external_room_reading_is_used_without_expiration(self):
        coordinator, _, states = self.make_coordinator(
            {"inside_sensor": "sensor.room", "sensor_timeout": 0}
        )
        state = self.sensor_state("21.5", age=7200)
        state.attributes = {}
        states["sensor.room"] = state
        coordinator._values = {"indoor_temperature": None, "room_sensor_alarm": True}
        self.assertEqual(coordinator.effective_inside, 21.5)
        self.assertEqual(coordinator.inside_source, "sensor.room")
        info = coordinator.temperature_source_info("inside")
        self.assertEqual(info["external_status"], "valid")
        self.assertEqual(info["freshness_limit_seconds"], 0)
        self.assertEqual(info["fallback_status"], "room_sensor_alarm")

    async def test_previous_automatic_timeout_does_not_expire_a_valid_existing_sensor(self):
        coordinator, _, states = self.make_coordinator(
            {"inside_sensor": "sensor.room", "sensor_timeout": 900}
        )
        states["sensor.room"] = self.sensor_state("21.5", age=7200)
        coordinator._values = {"indoor_temperature": None}
        self.assertEqual(coordinator.effective_inside, 21.5)
        self.assertEqual(
            coordinator.temperature_source_info("inside")["freshness_limit_seconds"], 0
        )

    async def test_invalid_external_temperature_reasons_preserve_native_fallback(self):
        coordinator, _, states = self.make_coordinator({"inside_sensor": "sensor.room"})
        coordinator._values = {"indoor_temperature": 19.25, "room_sensor_alarm": False}
        cases = [(None, "not_found"), ("unknown", "unknown"), ("unavailable", "unavailable")]
        for value, reason in cases:
            states.clear()
            if value is not None:
                states["sensor.room"] = self.sensor_state(value)
            self.assertEqual(coordinator.effective_inside, 19.25)
            self.assertEqual(coordinator.inside_source, "Thermia room sensor")
            self.assertEqual(
                coordinator.temperature_source_info("inside")["external_status"], reason
            )
        states["sensor.room"] = self.sensor_state("22")
        states["sensor.room"].attributes["unit_of_measurement"] = "kWh"
        self.assertEqual(coordinator.effective_inside, 19.25)
        self.assertEqual(
            coordinator.temperature_source_info("inside")["external_status"], "unsupported_unit"
        )

    async def test_explicit_external_timeout_falls_back_and_reports_stale(self):
        coordinator, _, states = self.make_coordinator(
            {"inside_sensor": "sensor.room", "sensor_timeout": 900, "configuration_version": 3}
        )
        states["sensor.room"] = self.sensor_state("22", age=901)
        coordinator._values = {"indoor_temperature": 19.25, "room_sensor_alarm": False}
        self.assertEqual(coordinator.effective_inside, 19.25)
        self.assertEqual(coordinator.temperature_source_info("inside")["external_status"], "stale")
        coordinator._values["indoor_temperature"] = None
        self.assertIsNone(coordinator.effective_inside)
        self.assertEqual(coordinator.inside_source, "Unavailable")

    async def test_external_temperature_units_are_converted_before_outdoor_feed(self):
        coordinator, device, states = self.make_coordinator(
            {"outside_sensor": "sensor.garden", "sensor_timeout": 0}
        )
        states["sensor.garden"] = self.sensor_state("50")
        states["sensor.garden"].attributes["unit_of_measurement"] = " °f "
        device.values = {
            "outdoor_source": 0,
            "bms_outdoor_temperature": None,
            "outdoor_temperature": 4.5,
        }
        coordinator._values = dict(device.values)
        await coordinator._async_outdoor_feed()
        self.assertEqual(device.writes, [("bms_outdoor_temperature", 10), ("outdoor_source", 1)])
        self.assertEqual(coordinator.effective_outside, 10)

    async def test_source_details_explain_selected_external_sensor_without_room_probe(self):
        coordinator, _, states = self.make_coordinator({"inside_sensor": "sensor.room"})
        coordinator._values = {"indoor_temperature": None, "comfort_wheel": 20}
        details = coordinator.temperature_source_details("inside")
        self.assertEqual(details["selected_sensor"], "sensor.room")
        self.assertEqual(details["external_status"], "not_found")
        self.assertIsNone(details["thermia_temperature"])
        self.assertIsNone(coordinator.effective_inside)
        self.assertNotIn("selected_sensor", coordinator.temperature_source_info("inside"))

        states["sensor.room"] = self.sensor_state("68")
        states["sensor.room"].attributes["unit_of_measurement"] = "°F"
        details = coordinator.temperature_source_details("inside")
        self.assertEqual(details["selected_sensor_state"], "68")
        self.assertEqual(details["selected_sensor_unit"], "°F")
        self.assertEqual(details["effective_temperature"], 20)
        self.assertEqual(details["external_status"], "valid")

    async def test_no_selected_room_sensor_does_not_use_a_comfort_or_supply_target(self):
        coordinator, _, _ = self.make_coordinator()
        coordinator._values = {
            "indoor_temperature": None,
            "comfort_wheel": 20,
            "supply_temperature": 35,
            "calculated_supply_target": 36,
        }
        self.assertIsNone(coordinator.effective_inside)
        self.assertEqual(coordinator.inside_source, "Unavailable")
        details = coordinator.temperature_source_details("inside")
        self.assertIsNone(details["selected_sensor"])
        self.assertEqual(details["external_status"], "not_selected")
        self.assertEqual(details["fallback_status"], "unavailable")
        self.assertIsNone(details["thermia_temperature"])

    async def test_source_name_does_not_claim_a_missing_physical_outdoor_reading(self):
        coordinator, _, _ = self.make_coordinator()
        coordinator._values = {"outdoor_source": 0, "outdoor_temperature": None}
        self.assertIsNone(coordinator.effective_outside)
        self.assertEqual(coordinator.outside_source, "Unavailable")

    async def test_listener_handles_changed_and_recovered_unchanged_reports_on_event_loop(self):
        coordinator, _, _ = self.make_coordinator(
            {"inside_sensor": "sensor.room", "sensor_timeout": 900, "configuration_version": 3}
        )
        tasks = []
        coordinator.hass.async_create_task = tasks.append
        coordinator.async_request_refresh = AsyncMock()
        coordinator.async_update_listeners = Mock()
        remove_changed, remove_reported = Mock(), Mock()
        with (
            patch.object(
                coordinator_module, "async_track_state_change_event", return_value=remove_changed
            ) as track_changed,
            patch.object(
                coordinator_module, "async_track_state_report_event", return_value=remove_reported
            ) as track_reported,
        ):
            coordinator.async_listen_sensors()
        changed = track_changed.call_args.args[2]
        reported = track_reported.call_args.args[2]
        self.assertTrue(changed._hass_callback)
        self.assertTrue(reported._hass_callback)
        changed(SimpleNamespace(data={}))
        await tasks.pop()
        coordinator.async_request_refresh.assert_awaited_once()
        coordinator.async_update_listeners.assert_called_once()
        reported(
            SimpleNamespace(
                data={"old_last_reported": datetime.now(timezone.utc) - timedelta(seconds=901)}
            )
        )
        await tasks.pop()
        self.assertEqual(coordinator.async_request_refresh.await_count, 2)
        self.assertEqual(coordinator.async_update_listeners.call_count, 2)
        reported(SimpleNamespace(data={"old_last_reported": datetime.now(timezone.utc)}))
        self.assertEqual(tasks, [])
        coordinator._unsub_sensors()
        remove_changed.assert_called_once()
        remove_reported.assert_called_once()

    async def test_external_source_updates_while_heat_pump_remains_offline(self):
        coordinator, _, states = self.make_coordinator({"inside_sensor": "sensor.room"})
        coordinator.last_update_success = False
        coordinator.async_update_listeners = Mock()
        coordinator.async_request_refresh = AsyncMock(side_effect=UpdateFailed("Offline"))
        tasks = []
        coordinator.hass.async_create_task = tasks.append
        with patch.object(coordinator_module, "async_track_state_change_event") as track_changed:
            coordinator.async_listen_sensors()
        changed = track_changed.call_args.args[2]
        states["sensor.room"] = self.sensor_state("22.5")
        changed(SimpleNamespace(data={}))
        coordinator.async_update_listeners.assert_called_once()
        self.assertEqual(
            coordinator.temperature_source_details("inside")["effective_temperature"], 22.5
        )
        self.assertFalse(coordinator.temperature_source_details("inside")["controller_available"])
        with self.assertRaises(UpdateFailed):
            await tasks.pop()
        coordinator._stopping = True
        changed(SimpleNamespace(data={}))
        coordinator.async_update_listeners.assert_called_once()
        self.assertEqual(tasks, [])

    async def test_configuration_waits_for_restoration_and_ignores_obsolete_checkbox(self):
        coordinator, old_device, _ = self.make_coordinator()
        coordinator.engine.settings["enable_undocumented_controls"] = True
        next_options = {
            **coordinator.engine.settings,
            "enable_undocumented_controls": False,
            "max_hot_water_temperature": 65,
        }
        coordinator._pending_options = next_options
        coordinator.engine.shutdown = AsyncMock(return_value=False)
        self.assertFalse(await coordinator.async_apply_pending_options())
        self.assertIs(coordinator.device, old_device)
        self.assertTrue(coordinator.engine.settings["enable_undocumented_controls"])
        self.assertIn("hot_water_boost", coordinator.device.specs)
        self.assertIs(coordinator._pending_options, next_options)

        coordinator.engine.shutdown.return_value = True
        self.assertTrue(await coordinator.async_apply_pending_options())
        self.assertIsNone(coordinator._pending_options)
        self.assertTrue(coordinator.engine.settings["enable_undocumented_controls"])
        self.assertIs(coordinator.device, old_device)
        self.assertIn("hot_water_boost", coordinator.device.specs)
        self.assertIn("anti_legionella_enabled", coordinator.device.specs)
        self.assertEqual(coordinator.engine.shutdown.await_count, 2)

    async def test_restart_cannot_reintroduce_the_old_automatic_sensor_expiry(self):
        coordinator, _, _ = self.make_coordinator(
            {"inside_sensor": "sensor.room", "sensor_timeout": 900}
        )
        coordinator._store.async_load = AsyncMock(
            return_value={
                "control": {},
                "settings": {
                    "configuration_version": 3,
                    "sensor_timeout": 0,
                    "max_heating_temperature": 25,
                },
            }
        )
        await coordinator.async_load()
        self.assertEqual(coordinator.engine.settings["sensor_timeout"], 0)
        self.assertEqual(coordinator.engine.settings["max_heating_temperature"], 25)
        self.assertEqual(coordinator.engine.settings["inside_sensor"], "sensor.room")
        self.assertEqual(coordinator.engine.settings["configuration_version"], 7)

    async def test_restart_retains_a_newly_explicit_sensor_expiry(self):
        coordinator, _, _ = self.make_coordinator(
            {"sensor_timeout": 900, "configuration_version": 3}
        )
        coordinator._store.async_load = AsyncMock(
            return_value={"control": {}, "settings": {"sensor_timeout": 0}}
        )
        await coordinator.async_load()
        self.assertEqual(coordinator.engine.settings["sensor_timeout"], 900)

    async def test_home_assistant_stop_prevents_following_poll_from_restarting_control(self):
        coordinator, device, _ = self.make_coordinator()
        coordinator._values = {"main_demand": 99}
        coordinator.engine.shutdown = AsyncMock(return_value=True)
        coordinator.engine.tick = AsyncMock()
        device.async_update = AsyncMock(return_value={"main_demand": 4})
        await coordinator.async_stop(None)
        self.assertTrue(coordinator._stopping)
        coordinator.engine.shutdown.assert_awaited_once()
        self.assertEqual(await coordinator._async_update_data(), {"main_demand": 99})
        device.async_update.assert_not_awaited()
        coordinator.engine.tick.assert_not_awaited()

    async def test_configure_none_defaults_do_not_write_native_controls(self):
        coordinator, device, _ = self.with_native_controls()
        options = dict(coordinator.engine.settings)
        options.update(
            hot_water_start=None,
            auxiliary_heater_enabled=None,
            anti_legionella_enabled=None,
        )
        await coordinator.async_configure_controls(options)
        self.assertEqual(device.writes, [])
        self.assertEqual(device.values["hot_water_start"], 45)
        self.assertEqual(device.values["immersion_heater"], 2)
        self.assertTrue(device.values["anti_legionella_enabled"])

    async def test_first_poll_with_none_control_defaults_does_not_change_native_states(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator._recover_pending = True
        coordinator.engine.settings.update(
            hot_water_start=None,
            auxiliary_heater_enabled=None,
            anti_legionella_enabled=None,
        )
        await coordinator._async_update_data()
        self.assertEqual(device.writes, [])
        self.assertEqual(device.values["hot_water_start"], 45)
        self.assertEqual(device.values["immersion_heater"], 2)
        self.assertTrue(device.values["anti_legionella_enabled"])

    async def test_unrelated_options_save_does_not_reapply_previous_native_choices(self):
        coordinator, device, _ = self.with_native_controls()
        options = dict(coordinator.engine.settings)
        # These saved choices differ from current hardware; an empty deliberate
        # change map must not impose them while changing an unrelated setting.
        options.update(
            hot_water_start=43,
            auxiliary_heater_enabled=False,
            anti_legionella_enabled=False,
            sensor_timeout=1200,
        )
        await coordinator.async_configure_controls(options, control_changes={})
        self.assertEqual(device.writes, [])
        self.assertEqual(device.values["hot_water_start"], 45)
        self.assertEqual(device.values["immersion_heater"], 2)
        self.assertTrue(device.values["anti_legionella_enabled"])

    async def test_only_explicit_auxiliary_switch_change_is_applied(self):
        coordinator, device, _ = self.with_native_controls()
        options = dict(coordinator.engine.settings)
        options.update(auxiliary_heater_enabled=False, anti_legionella_enabled=None)
        await coordinator.async_configure_controls(
            options, control_changes={"auxiliary_heater_enabled": False}
        )
        self.assertEqual(device.writes, [("immersion_heater", 0)])
        self.assertEqual(device.values["hot_water_start"], 45)
        self.assertTrue(device.values["anti_legionella_enabled"])

    async def test_configuration_native_boost_changes_only_the_flag(self):
        coordinator, device, _ = self.with_native_controls()
        device.values["hot_water_boost"] = False
        coordinator._values = dict(device.values)
        options = dict(coordinator.engine.settings)
        await coordinator.async_configure_controls(
            options, control_changes={"native_boost_enabled": True}
        )
        self.assertEqual(device.writes, [("hot_water_boost", True)])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")

    async def test_configuration_keeps_heating_and_cooling_permissions_independent(self):
        coordinator, device, _ = self.with_native_controls()
        await coordinator.async_configure_controls(
            dict(coordinator.engine.settings),
            control_changes={"heating_enabled": True, "passive_cooling_enabled": True},
        )
        self.assertEqual(device.writes, [("passive_cooling_enabled", True)])
        self.assertTrue(device.values["heating_enabled"])
        self.assertTrue(device.values["passive_cooling_enabled"])
        self.assertFalse(coordinator.engine.state["managed_heating"])

    async def test_configuration_rejects_invalid_native_boost_before_writes(self):
        coordinator, device, _ = self.with_native_controls()
        device.values["hot_water_boost"] = 20000
        coordinator._values = dict(device.values)
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings), control_changes={"native_boost_enabled": True}
            )
        self.assertEqual(device.writes, [])
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_unrelated_failed_save_never_restores_an_invalid_boost_sentinel(self):
        coordinator, device, _ = self.with_native_controls()
        device.values["hot_water_boost"] = 20000
        coordinator._values = dict(device.values)
        original_write = device.async_write

        async def failed_confirmation(key, value):
            result = await original_write(key, value)
            if key == "immersion_heater" and value == 0:
                raise ValueError("Reply lost after heater change")
            return result

        device.async_write = failed_confirmation
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={"auxiliary_heater_enabled": False},
            )
        self.assertEqual(device.values["immersion_heater"], 2)
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertFalse(any(key == "hot_water_boost" for key, _ in device.writes))

    async def test_configure_pair_crossing_old_stop_uses_new_limits_and_safe_order(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.engine.settings.update(max_start_temperature=50, max_hot_water_temperature=55)
        options = dict(coordinator.engine.settings)
        options.update(max_start_temperature=58, max_hot_water_temperature=60)
        pairs = []
        original_write = device.async_write

        async def record_pair(key, value):
            result = await original_write(key, value)
            pairs.append((device.values["hot_water_start"], device.values["hot_water_stop"]))
            return result

        device.async_write = record_pair
        await coordinator.async_configure_controls(
            options, control_changes={"hot_water_start": 52, "hot_water_target": 57}
        )
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (52, 57)
        )
        self.assertTrue(pairs)
        self.assertTrue(all(start < stop for start, stop in pairs), pairs)
        self.assertEqual(coordinator.engine.settings["max_start_temperature"], 58)
        self.assertEqual(coordinator.engine.settings["max_hot_water_temperature"], 60)
        self.assertEqual(coordinator.entry.options, {})

    async def test_obsolete_manual_profile_does_not_block_native_switch_changes(self):
        coordinator, device, _ = self.with_native_controls()
        original_settings = deepcopy(coordinator.engine.settings)
        options = dict(original_settings)
        options.update(hot_water_boost_start=55, hot_water_boost_stop=50)
        await coordinator.async_configure_controls(
            options, control_changes={"auxiliary_heater_enabled": False}
        )
        self.assertEqual(device.writes, [("immersion_heater", 0)])
        self.assertIsNone(coordinator.engine.settings["hot_water_boost_start"])
        self.assertIsNone(coordinator.engine.settings["hot_water_boost_stop"])
        self.assertEqual(coordinator.entry.options, {})

    async def test_bad_normal_pair_does_not_adjust_hardware_caps_or_stop(self):
        coordinator, device, _ = self.with_native_controls()
        original_settings = deepcopy(coordinator.engine.settings)
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(original_settings), control_changes={"hot_water_start": 52}
            )
        self.assertEqual(device.writes, [])
        self.assertEqual(device.values["hot_water_stop"], 50)
        self.assertEqual(coordinator.engine.settings, original_settings)

    async def test_partial_native_failure_rolls_back_options_and_keeps_warning(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        original_settings = deepcopy(coordinator.engine.settings)
        options = dict(original_settings)
        options.update(
            sensor_timeout=1200, auxiliary_heater_enabled=False, anti_legionella_enabled=False
        )
        original_entry_options = deepcopy(coordinator.entry.options)
        original_write = device.async_write

        async def refusing_write(key, value):
            if key == "anti_legionella_enabled":
                raise ValueError("Controller did not confirm anti legionella write")
            return await original_write(key, value)

        device.async_write = refusing_write
        with self.assertRaisesRegex(HomeAssistantError, "did not confirm"):
            await coordinator.async_configure_controls(
                options,
                control_changes={
                    "auxiliary_heater_enabled": False,
                    "anti_legionella_enabled": False,
                },
            )
        self.assertIn(("immersion_heater", 0), device.writes)
        self.assertEqual(coordinator.engine.settings, original_settings)
        self.assertEqual(coordinator.entry.options, original_entry_options)
        self.assertIn("did not confirm", coordinator.engine.state["control_warning"])

    async def test_failed_readback_restores_physical_values_and_prior_normal_policy(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        original_policy = {
            key: coordinator.engine.state[key]
            for key in (
                "hot_water_start",
                "hot_water_target",
                "hot_water_mode",
                "managed_hot_water",
            )
        }
        options = dict(coordinator.engine.settings)
        original_write = device.async_write

        async def applied_but_unconfirmed(key, value):
            result = await original_write(key, value)
            if key == "anti_legionella_enabled" and value is False:
                # A readback failure can follow an actual hardware change.
                # The coordinator's cached value still says True at this point.
                raise ValueError("Could not confirm applied anti-legionella write")
            return result

        device.async_write = applied_but_unconfirmed
        with self.assertRaisesRegex(HomeAssistantError, "Could not confirm"):
            await coordinator.async_configure_controls(
                options,
                control_changes={
                    "hot_water_start": 44,
                    "hot_water_target": 53,
                    "anti_legionella_enabled": False,
                },
            )
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertTrue(device.values["anti_legionella_enabled"])
        self.assertEqual(
            {key: coordinator.engine.state[key] for key in original_policy}, original_policy
        )
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_incomplete_configuration_rollback_is_retried_before_control_tick(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        original_write = device.async_write
        block_restore = True

        async def write_with_failed_restore(key, value):
            if block_restore and key == "hot_water_start" and value == 45:
                raise ValueError("Connection failed while restoring original start")
            result = await original_write(key, value)
            if block_restore and key == "anti_legionella_enabled" and value is False:
                raise ValueError("Applied flag could not be confirmed")
            return result

        device.async_write = write_with_failed_restore
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={
                    "hot_water_start": 44,
                    "hot_water_target": 53,
                    "anti_legionella_enabled": False,
                },
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        self.assertTrue(coordinator._store.saved)
        self.assertIn("pending", coordinator.engine.state["control_warning"])
        coordinator.engine.tick = AsyncMock()
        await coordinator._async_update_data()
        coordinator.engine.tick.assert_not_awaited()
        self.assertIn("configuration_restore", coordinator.engine.state)

        block_restore = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertTrue(device.values["anti_legionella_enabled"])
        self.assertEqual(coordinator.engine.state["hot_water_start"], 45)
        self.assertEqual(coordinator.engine.state["hot_water_target"], 50)
        coordinator.engine.tick.assert_awaited_once()

    async def test_boost_switch_native_coupling_and_ceiling_through_coordinator(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        device.values["hot_water_boost"] = False
        coordinator._values = dict(device.values)
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("hot_water_boost_enabled", True)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (58, 60)
        )
        self.assertTrue(device.values["hot_water_boost"])
        self.assertTrue(coordinator.engine.state["boost_enabled"])
        self.assertEqual(device.values["immersion_heater"], 2)
        self.assertFalse(any(key == "immersion_heater" for key, _ in device.writes))

        device.values.update(hot_water_top_temperature=60, hot_water_weighted_temperature=59)
        await coordinator._async_update_data()
        self.assertTrue(coordinator.engine.state["boost_enabled"])
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (58, 60)
        )

        await coordinator.async_command("hot_water_boost_enabled", False)
        self.assertFalse(coordinator.engine.state["boost_enabled"])
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertTrue(device.values["hot_water_enabled"])

    async def test_obsolete_profile_is_discarded_without_interrupting_energy_excess(self):
        coordinator, device, _ = self.with_native_controls()
        device.values["hot_water_boost"] = False
        coordinator._values = dict(device.values)
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("hot_water_mode", "energy_excess")
        writes_before = list(device.writes)
        options = dict(coordinator.engine.settings)
        options.update(hot_water_boost_start=55, hot_water_boost_stop=50)
        await coordinator.async_configure_controls(options, control_changes={})
        self.assertEqual(device.writes, writes_before)
        self.assertTrue(coordinator.engine.state["boost_enabled"])
        self.assertIsNone(coordinator.engine.settings["hot_water_boost_start"])
        self.assertIsNone(coordinator.engine.settings["hot_water_boost_stop"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (58, 60)
        )

    async def test_stop_restores_active_boost_and_later_poll_cannot_resume_it(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("hot_water_boost_enabled", True)
        await coordinator.async_stop(None)
        self.assertFalse(coordinator.engine.state["boost_enabled"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        writes_after_stop = list(device.writes)
        await coordinator._async_update_data()
        self.assertEqual(device.writes, writes_after_stop)

    async def test_failed_configuration_save_restores_active_sixty_sixty_boost(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        device.values["hot_water_boost"] = False
        coordinator._values = dict(device.values)
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("hot_water_boost_enabled", True)
        original_write = device.async_write

        async def applied_but_unconfirmed(key, value):
            result = await original_write(key, value)
            if key == "anti_legionella_enabled" and value is False:
                raise ValueError("Could not confirm applied programme change")
            return result

        device.async_write = applied_but_unconfirmed
        with self.assertRaisesRegex(HomeAssistantError, "Could not confirm"):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={
                    "auxiliary_heater_enabled": False,
                    "anti_legionella_enabled": False,
                },
            )
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertTrue(coordinator.engine.state["boost_enabled"])
        self.assertTrue(device.values["anti_legionella_enabled"])
        self.assertEqual(device.values["immersion_heater"], 2)
        self.assertTrue(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (58, 60)
        )

        await coordinator.async_command("hot_water_boost_enabled", False)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertFalse(device.values["hot_water_boost"])

    async def test_native_supply_batch_uses_safe_order_and_only_deliberate_settings(self):
        coordinator, device, _ = self.with_native_controls()
        device.values.update(
            min_supply_temperature=20, max_supply_temperature=35, heating_season_stop=17
        )
        coordinator._values = dict(device.values)
        pairs = []
        original_write = device.async_write

        async def record_pair(key, value):
            result = await original_write(key, value)
            pairs.append(
                (device.values["min_supply_temperature"], device.values["max_supply_temperature"])
            )
            return result

        device.async_write = record_pair
        await coordinator.async_configure_controls(
            dict(coordinator.engine.settings),
            control_changes={"min_supply_temperature": 40, "max_supply_temperature": 50},
        )
        self.assertEqual(
            device.writes, [("max_supply_temperature", 50), ("min_supply_temperature", 40)]
        )
        self.assertTrue(all(low <= high for low, high in pairs))
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_bad_native_slider_or_reversed_pair_fails_before_other_switch_writes(self):
        for changes in (
            {"min_supply_temperature": 51, "max_supply_temperature": 50},
            {"heating_season_stop": 17.5},
            {"heating_season_stop": float("nan")},
            {"max_supply_temperature": True},
        ):
            with self.subTest(changes=changes):
                coordinator, device, _ = self.with_native_controls()
                device.values.update(
                    min_supply_temperature=20, max_supply_temperature=35, heating_season_stop=17
                )
                coordinator._values = dict(device.values)
                with self.assertRaises(HomeAssistantError):
                    await coordinator.async_configure_controls(
                        dict(coordinator.engine.settings),
                        control_changes={"auxiliary_heater_enabled": False, **changes},
                    )
                self.assertEqual(device.writes, [])
                self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_failed_later_configuration_switch_restores_exact_native_limits(self):
        coordinator, device, _ = self.with_native_controls()
        device.values.update(min_supply_temperature=17.25, max_supply_temperature=38.5)
        coordinator._values = dict(device.values)
        original_write = device.async_write
        pairs = []

        async def lose_switch_confirmation(key, value):
            result = await original_write(key, value)
            pairs.append(
                (device.values["min_supply_temperature"], device.values["max_supply_temperature"])
            )
            if key == "anti_legionella_enabled" and value is False:
                raise ValueError("Applied programme flag but lost confirmation")
            return result

        device.async_write = lose_switch_confirmation
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={
                    "min_supply_temperature": 40,
                    "max_supply_temperature": 50,
                    "anti_legionella_enabled": False,
                },
            )
        self.assertEqual(
            (device.values["min_supply_temperature"], device.values["max_supply_temperature"]),
            (17.25, 38.5),
        )
        self.assertTrue(all(low <= high for low, high in pairs))
        self.assertTrue(device.values["anti_legionella_enabled"])
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_invalid_excess_duration_never_changes_hardware_or_options(self):
        for key, value in (
            ("heating_excess_hours", 0),
            ("hot_water_excess_hours", float("inf")),
            ("heating_excess_hours", True),
            ("hot_water_excess_hours", 169),
        ):
            with self.subTest(key=key, value=value):
                coordinator, device, _ = self.with_native_controls()
                original = dict(coordinator.engine.settings)
                with self.assertRaises(HomeAssistantError):
                    await coordinator.async_configure_controls(
                        original | {key: value},
                        control_changes={"auxiliary_heater_enabled": False},
                    )
                self.assertEqual(device.writes, [])
                self.assertEqual(coordinator.engine.settings, original)

    async def test_pending_configuration_rollback_cannot_keep_expired_water_boost_on(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        device.values["hot_water_boost"] = False
        coordinator._values = dict(device.values)
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        coordinator.engine.settings["hot_water_excess_hours"] = 0.1
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("hot_water_mode", "energy_excess")
        self.assertTrue(device.values["hot_water_boost"])
        original_write = device.async_write
        block_restore = True

        async def failed_programme_save(key, value):
            if block_restore and key == "anti_legionella_enabled" and value is True:
                raise ValueError("Unrelated programme restoration remains unavailable")
            result = await original_write(key, value)
            if block_restore and key == "anti_legionella_enabled" and value is False:
                raise ValueError("Applied programme could not be confirmed")
            return result

        device.async_write = failed_programme_save
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={"anti_legionella_enabled": False},
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        clock[0] = 1361.0
        for _ in range(2):
            await coordinator._async_update_data()
            self.assertIn("configuration_restore", coordinator.engine.state)
            self.assertFalse(device.values["hot_water_boost"])
            self.assertEqual(
                (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
            )

        block_restore = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertFalse(coordinator.engine.state["boost_enabled"])
        self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
        self.assertTrue(device.values["anti_legionella_enabled"])
        writes_after_restoration = list(device.writes)
        await coordinator._async_update_data()
        self.assertEqual(device.writes, writes_after_restoration)
        self.assertFalse(device.values["hot_water_boost"])

    async def test_pending_configuration_rollback_cannot_restore_expired_fixed_heating(self):
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        device.values.update(
            fixed_supply_enabled=False,
            fixed_supply_target=30.0,
            max_supply_temperature=40.0,
            smart_grid_request=0,
        )
        coordinator._values = dict(device.values)
        coordinator.engine.settings.update(
            heating_excess_hours=0.1,
            max_heating_temperature=23.0,
            charge_supply_temperature=35.0,
        )
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("heating_preset", "pv_charge")
        self.assertTrue(device.values["fixed_supply_enabled"])
        original_write = device.async_write
        block_restore = True

        async def failed_programme_save(key, value):
            if block_restore and key == "anti_legionella_enabled" and value is True:
                raise ValueError("Unrelated programme restoration remains unavailable")
            result = await original_write(key, value)
            if block_restore and key == "anti_legionella_enabled" and value is False:
                raise ValueError("Applied programme could not be confirmed")
            return result

        device.async_write = failed_programme_save
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={"anti_legionella_enabled": False},
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        clock[0] = 1361.0
        for _ in range(2):
            await coordinator._async_update_data()
            self.assertIn("configuration_restore", coordinator.engine.state)
            self.assertFalse(device.values["fixed_supply_enabled"])
            self.assertEqual(device.values["fixed_supply_target"], 30.0)

        block_restore = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
        self.assertTrue(device.values["anti_legionella_enabled"])
        writes_after_restoration = list(device.writes)
        await coordinator._async_update_data()
        self.assertEqual(device.writes, writes_after_restoration)
        self.assertFalse(device.values["fixed_supply_enabled"])

    async def pending_dual_excess_rollback(self, *, heating_hours=0.1, water_hours=0.1):
        """Start both functions and leave an unrelated Configure rollback pending."""
        coordinator, device, _ = self.with_native_controls({"enable_undocumented_controls": True})
        device.values.update(
            hot_water_boost=False,
            fixed_supply_enabled=False,
            fixed_supply_target=30.0,
            max_supply_temperature=40.0,
            smart_grid_request=0,
        )
        coordinator._values = dict(device.values)
        coordinator.engine.settings.update(
            heating_excess_hours=heating_hours,
            hot_water_excess_hours=water_hours,
            max_heating_temperature=23.0,
            charge_supply_temperature=35.0,
        )
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        coordinator.async_request_refresh = AsyncMock()
        await coordinator.async_command("heating_preset", "pv_charge")
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original_write = device.async_write
        failures = {"programme": True, "boost": False}

        async def failed_configuration_write(key, value):
            if failures["programme"] and key == "anti_legionella_enabled" and value is True:
                raise ValueError("Unrelated programme restoration remains unavailable")
            if failures["boost"] and key == "hot_water_boost" and value is False:
                raise ValueError("Native Boost Off cannot currently be confirmed")
            result = await original_write(key, value)
            if failures["programme"] and key == "anti_legionella_enabled" and value is False:
                raise ValueError("Applied programme could not be confirmed")
            return result

        device.async_write = failed_configuration_write
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                dict(coordinator.engine.settings),
                control_changes={"anti_legionella_enabled": False},
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        return coordinator, device, clock, failures

    async def test_expired_heating_restores_when_independent_boost_off_write_fails(self):
        coordinator, device, clock, failures = await self.pending_dual_excess_rollback()
        failures["boost"] = True
        clock[0] = 1361.0
        for _ in range(2):
            await coordinator._async_update_data()
            self.assertIn("configuration_restore", coordinator.engine.state)
            self.assertFalse(device.values["fixed_supply_enabled"])
            self.assertEqual(device.values["fixed_supply_target"], 30.0)

        # A failed Boost-Off must still remove the independent water permission,
        # rather than leaving the pump heating against its temporary boosted pair.
        self.assertFalse(device.values["hot_water_enabled"])

        failures.update(programme=False, boost=False)
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )

    async def test_second_timer_still_expires_while_configuration_rollback_is_pending(self):
        coordinator, device, clock, failures = await self.pending_dual_excess_rollback(
            heating_hours=0.2, water_hours=0.1
        )
        clock[0] = 1361.0
        await coordinator._async_update_data()
        self.assertIn("configuration_restore", coordinator.engine.state)
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertTrue(device.values["fixed_supply_enabled"])
        self.assertEqual(coordinator.engine.state["heating_excess_deadline"], 1720.0)

        clock[0] = 1721.0
        for _ in range(2):
            await coordinator._async_update_data()
            self.assertIn("configuration_restore", coordinator.engine.state)
            self.assertFalse(device.values["fixed_supply_enabled"])
            self.assertEqual(device.values["fixed_supply_target"], 30.0)
            self.assertFalse(device.values["hot_water_boost"])

        failures["programme"] = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
        self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
        writes_after_restoration = list(device.writes)
        await coordinator._async_update_data()
        self.assertEqual(device.writes, writes_after_restoration)

    async def test_unrelated_restore_read_failure_cannot_defer_both_expired_functions(self):
        coordinator, device, clock, failures = await self.pending_dual_excess_rollback()
        original_read = device.async_read_value

        async def failed_programme_read(key):
            if key == "anti_legionella_enabled":
                raise ValueError("Unrelated programme read remains unavailable")
            return await original_read(key)

        device.async_read_value = failed_programme_read
        clock[0] = 1361.0
        await coordinator._async_update_data()
        self.assertIn("configuration_restore", coordinator.engine.state)
        self.assertFalse(device.values["fixed_supply_enabled"])
        self.assertEqual(device.values["fixed_supply_target"], 30.0)
        self.assertFalse(device.values["hot_water_boost"])
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )

        device.async_read_value = original_read
        failures["programme"] = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")

    async def test_configure_capped_gap_change_preserves_start_originals_and_timer(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        coordinator.engine.now = lambda: 1000.0
        coordinator.engine.settings.update(max_start_temperature=57, hot_water_hysteresis=3)
        device.values["hot_water_boost"] = False
        coordinator._values["hot_water_boost"] = False
        await coordinator.async_command("hot_water_mode", "energy_excess")
        coordinator.entry.options = dict(coordinator.engine.settings)
        original_options = deepcopy(coordinator.entry.options)
        original_settings = deepcopy(coordinator.engine.settings)
        original_state = deepcopy(coordinator.engine.state)
        original_native = deepcopy(device.values)
        device.writes.clear()

        await coordinator.async_configure_controls(
            {**coordinator.engine.settings, "hot_water_hysteresis": 2}, control_changes={}
        )

        self.assertEqual(device.writes, [])
        self.assertEqual(device.values, original_native)
        self.assertEqual(coordinator.entry.options, original_options)
        self.assertEqual(
            coordinator.engine.settings, original_settings | {"hot_water_hysteresis": 2}
        )
        self.assertEqual(coordinator.engine.hot_water_excess_info()["effective_restart_gap"], 3)
        for key in (
            "overrides",
            "charge_active",
            "hot_water_mode",
            "boost_enabled",
            "hot_water_excess_started_at",
            "hot_water_excess_deadline",
        ):
            self.assertEqual(coordinator.engine.state[key], original_state[key], key)
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_configure_gap_updates_native_pair_without_restarting_countdown_or_snapshot(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.async_request_refresh = AsyncMock()
        clock = [1000.0]
        coordinator.engine.now = lambda: clock[0]
        coordinator.engine.settings.update(max_start_temperature=58, hot_water_hysteresis=3)
        device.values["hot_water_boost"] = False
        coordinator._values["hot_water_boost"] = False
        await coordinator.async_command("hot_water_mode", "energy_excess")
        original_snapshot = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
        original_deadline = coordinator.engine.state["hot_water_excess_deadline"]
        clock[0] = 4600.0

        await coordinator.async_configure_controls(
            {**coordinator.engine.settings, "hot_water_hysteresis": 2}, control_changes={}
        )
        await coordinator.engine.tick(coordinator.effective_inside, coordinator.effective_outside)

        self.assertEqual(coordinator.engine.settings["hot_water_hysteresis"], 2)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (58, 60)
        )
        self.assertTrue(device.values["hot_water_boost"])
        self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original_snapshot)
        self.assertEqual(coordinator.engine.state["hot_water_excess_started_at"], 1000.0)
        self.assertEqual(coordinator.engine.state["hot_water_excess_deadline"], original_deadline)

    async def test_inactive_evening_preferences_need_no_verified_caps_or_native_writes(self):
        coordinator, device, _ = self.with_native_controls()
        coordinator.engine.settings.update(
            max_start_temperature=None, max_hot_water_temperature=None
        )
        before = deepcopy(coordinator.engine.state)
        await coordinator.async_configure_controls(
            {
                **coordinator.engine.settings,
                "hot_water_evening_start": 36,
                "hot_water_evening_stop": 42,
            },
            control_changes={},
        )
        self.assertEqual(device.writes, [])
        self.assertEqual(coordinator.engine.state, before)
        self.assertEqual(coordinator.engine.settings["hot_water_evening_start"], 36)
        self.assertEqual(coordinator.engine.settings["hot_water_evening_stop"], 42)

    async def test_invalid_evening_preferences_block_all_configuration_writes(self):
        for start, stop in ((40, 40), (45, 40), (29, 40), (35, 61), (True, 40), (35, float("nan"))):
            with self.subTest(start=start, stop=stop):
                coordinator, device, _ = self.with_native_controls()
                before = deepcopy(coordinator.engine.settings)
                with self.assertRaises(HomeAssistantError):
                    await coordinator.async_configure_controls(
                        {
                            **before,
                            "hot_water_evening_start": start,
                            "hot_water_evening_stop": stop,
                        },
                        control_changes={"auxiliary_heater_enabled": False},
                    )
                self.assertEqual(device.writes, [])
                self.assertEqual(coordinator.engine.settings, before)
                self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_active_evening_edit_is_journaled_and_preserves_original_normal_pair(self):
        coordinator, device, _ = self.with_native_controls()
        device.values["hot_water_boost"] = False
        coordinator._values["hot_water_boost"] = False
        await coordinator.engine.command("hot_water_mode", "evening")
        original = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
        device.writes.clear()
        journals = []
        original_write = device.async_write

        async def record_write(key, value):
            if key in {"hot_water_start", "hot_water_stop"}:
                journals.append(deepcopy(coordinator.engine.state.get("configuration_restore")))
            return await original_write(key, value)

        device.async_write = record_write
        await coordinator.async_configure_controls(
            {
                **coordinator.engine.settings,
                "hot_water_evening_start": 36,
                "hot_water_evening_stop": 42,
            },
            control_changes={},
        )
        self.assertTrue(journals)
        self.assertTrue(all(journal is not None for journal in journals))
        self.assertEqual(journals[0]["native"]["hot_water_start"], 35)
        self.assertEqual(journals[0]["native"]["hot_water_stop"], 40)
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (36, 42)
        )
        self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original)
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "evening")
        self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        await coordinator.engine.command("hot_water_mode", "auto")
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )

    async def test_active_evening_edit_checks_caps_before_hardware_or_settings_change(self):
        coordinator, device, _ = self.with_native_controls()
        await coordinator.engine.command("hot_water_mode", "evening")
        coordinator.engine.settings["max_start_temperature"] = 50
        before = deepcopy(coordinator.engine.state)
        settings = deepcopy(coordinator.engine.settings)
        device.writes.clear()
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                {
                    **settings,
                    "hot_water_evening_start": 52,
                    "hot_water_evening_stop": 55,
                },
                control_changes={},
            )
        self.assertEqual(device.writes, [])
        self.assertEqual(coordinator.engine.settings, settings)
        self.assertEqual(coordinator.engine.state["overrides"], before["overrides"])
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "evening")

    async def test_active_evening_edit_requires_online_confirmation(self):
        coordinator, device, _ = self.with_native_controls()
        await coordinator.engine.command("hot_water_mode", "evening")
        settings = deepcopy(coordinator.engine.settings)
        device.writes.clear()
        coordinator.last_update_success = False
        with self.assertRaisesRegex(HomeAssistantError, "Connect the heat pump"):
            await coordinator.async_configure_controls(
                {
                    **settings,
                    "hot_water_evening_start": 36,
                    "hot_water_evening_stop": 42,
                },
                control_changes={},
            )
        self.assertEqual(device.writes, [])
        self.assertEqual(coordinator.engine.settings, settings)
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_failed_evening_edit_restores_previous_profile_and_normal_snapshot(self):
        coordinator, device, _ = self.with_native_controls()
        await coordinator.engine.command("hot_water_mode", "evening")
        original = deepcopy(coordinator.engine.state["overrides"]["hot_water"])
        settings = deepcopy(coordinator.engine.settings)
        original_write = device.async_write
        failed = False

        async def fail_stop_once(key, value):
            nonlocal failed
            if key == "hot_water_stop" and value == 42 and not failed:
                failed = True
                raise RuntimeError("Evening STOP failed")
            return await original_write(key, value)

        device.async_write = fail_stop_once
        with self.assertRaisesRegex(HomeAssistantError, "Evening STOP failed"):
            await coordinator.async_configure_controls(
                {
                    **settings,
                    "hot_water_evening_start": 36,
                    "hot_water_evening_stop": 42,
                },
                control_changes={},
            )
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (35, 40)
        )
        self.assertEqual(coordinator.engine.settings, settings)
        self.assertEqual(coordinator.engine.state["overrides"]["hot_water"], original)
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "evening")
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_restart_resolves_failed_evening_configuration_to_normal(self):
        coordinator, device, _ = self.with_native_controls()
        await coordinator.engine.command("hot_water_mode", "evening")
        original_write = device.async_write
        failed = False

        async def fail_after_first_stop(key, value):
            nonlocal failed
            if key == "hot_water_stop" and value == 42:
                failed = True
            if failed and key.startswith("hot_water"):
                raise RuntimeError("Pump connection lost")
            return await original_write(key, value)

        device.async_write = fail_after_first_stop
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_configure_controls(
                {
                    **coordinator.engine.settings,
                    "hot_water_evening_start": 36,
                    "hot_water_evening_stop": 42,
                },
                control_changes={},
            )
        self.assertIn("configuration_restore", coordinator.engine.state)
        device.async_write = original_write
        coordinator._recover_pending = True
        await coordinator._async_update_data()
        self.assertEqual(
            (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
        )
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertEqual(coordinator.engine.state["overrides"], {})
        self.assertNotIn("configuration_restore", coordinator.engine.state)

    async def test_restart_cancels_active_excess_before_unrelated_configuration_restore(self):
        coordinator, device, clock, failures = await self.pending_dual_excess_rollback()
        clock[0] = 1100.0  # Both deadlines are still in the future.
        coordinator._recover_pending = True
        for _ in range(2):
            await coordinator._async_update_data()
            self.assertIn("configuration_restore", coordinator.engine.state)
            self.assertFalse(device.values["fixed_supply_enabled"])
            self.assertEqual(device.values["fixed_supply_target"], 30.0)
            self.assertFalse(device.values["hot_water_boost"])
            self.assertEqual(
                (device.values["hot_water_start"], device.values["hot_water_stop"]), (45, 50)
            )

        failures["programme"] = False
        await coordinator._async_update_data()
        self.assertNotIn("configuration_restore", coordinator.engine.state)
        self.assertFalse(coordinator._recover_pending)
        self.assertEqual(coordinator.engine.state["heating_preset"], "normal")
        self.assertEqual(coordinator.engine.state["hot_water_mode"], "auto")
        self.assertIsNone(coordinator.engine.state["heating_excess_deadline"])
        self.assertIsNone(coordinator.engine.state["hot_water_excess_deadline"])


if __name__ == "__main__":
    unittest.main()
