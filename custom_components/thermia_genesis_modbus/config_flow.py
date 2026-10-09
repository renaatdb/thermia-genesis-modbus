"""UI setup and temperature-source options for Thermia Genesis Modbus."""

from __future__ import annotations

import math

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from modbus_connection import ModbusError, ModbusTcpParams

from .const import CONF_UNIT_ID, DEFAULT_OPTIONS, DOMAIN, NAME, normalize_options
from .native_settings import NATIVE_SETTINGS, validate_native_setting
from .temperature import temperature_source_status

PREFERENCE_KEYS = (
    "control_mode",
    "heat_pump_model",
    "inside_sensor",
    "inside_humidity_sensor",
    "outside_sensor",
    "max_start_temperature",
    "max_hot_water_temperature",
    "sensor_timeout",
    "hysteresis",
)


def _finite_reading(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class NativeSettingSelector(selector.NumberSelector):
    """Validate edits while allowing an untouched native value outside our bounds."""

    def __init__(self, setting, baseline):
        super().__init__(
            selector.NumberSelectorConfig(
                min=setting.min_value,
                max=setting.max_value,
                step=setting.step,
                mode=selector.NumberSelectorMode.SLIDER,
                unit_of_measurement="°C",
            )
        )
        self._setting = setting
        self._baseline = baseline

    def __call__(self, data):
        if isinstance(data, bool):
            raise vol.Invalid("A native temperature setting needs a numeric value")
        value = vol.Coerce(float)(data)
        if math.isfinite(value) and value == self._baseline:
            return value
        try:
            return validate_native_setting(self._setting.key, value)
        except ValueError as error:
            raise vol.Invalid(str(error)) from error


class WholeDegreeTemperatureSelector(selector.NumberSelector):
    """Require whole-degree edits without rounding an existing preference."""

    def __init__(self, config, baseline):
        super().__init__(config)
        self._baseline = baseline

    def __call__(self, data):
        if isinstance(data, bool):
            raise vol.Invalid("A temperature setting needs a numeric value")
        value = float(super().__call__(data))
        if not math.isfinite(value):
            raise vol.Invalid("A temperature setting needs a finite value")
        if value != self._baseline and not value.is_integer():
            raise vol.Invalid("Temperature settings must use 1°C steps")
        return value


class ThermiaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Collect connection details and probe without writing anything."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            host = user_input["host"].strip()
            # A renamed domain creates a separate controller entry. Keep the
            # old entry's recovery journal in charge until it is removed.
            for entry in self.hass.config_entries.async_entries("thermia_pv"):
                if (
                    entry.data.get("host", "").strip().lower() == host.lower()
                    and entry.data.get("port", 502) == user_input["port"]
                    and entry.data.get(CONF_UNIT_ID, 1) == user_input[CONF_UNIT_ID]
                ):
                    return self.async_abort(reason="legacy_entry_exists")
            try:
                async with async_get_temporary_unit(
                    self.hass,
                    ModbusTcpParams(host=host, port=user_input["port"]),
                    user_input[CONF_UNIT_ID],
                ) as unit:
                    reply = await unit.read_input_registers(1, 1)
                    if len(reply) != 1 or reply[0] == 0x4E20:
                        raise ValueError("Invalid Thermia operating status")
            except (ModbusError, HomeAssistantError, ValueError, OSError):
                errors["base"] = "cannot_connect"
            else:
                unique_id = f"{host.lower()}:{user_input['port']}:{user_input[CONF_UNIT_ID]}"
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=NAME,
                    data=user_input | {"host": host},
                    options={"control_mode": "external"},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("host"): str,
                    vol.Required("port", default=502): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                    vol.Required(CONF_UNIT_ID, default=1): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=247)
                    ),
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return ThermiaOptionsFlow()


class ThermiaOptionsFlow(config_entries.OptionsFlow):
    """Keep sensor preferences separate from native curve and supply settings."""

    def __init__(self):
        self._pending_options = None
        self._displayed_controls = None
        self._control_input = None

    @property
    def _coordinator(self):
        return getattr(self.config_entry, "runtime_data", None)

    async def async_step_init(self, user_input=None):
        current = normalize_options(self.config_entry.options)
        if user_input is not None:
            # Device-page controls remain in the saved options. Only fields
            # offered on this page can be cleared by an omitted selection.
            options = dict(current)
            options.update(
                {key: value for key, value in user_input.items() if key in PREFERENCE_KEYS}
            )
            for key in (
                "inside_sensor",
                "inside_humidity_sensor",
                "outside_sensor",
                "max_hot_water_temperature",
                "max_start_temperature",
            ):
                options[key] = user_input.get(key)
                if options[key] in (None, ""):
                    # Clearing a sensor removes its selection; clearing a
                    # hot-water capability resets its integration default.
                    options[key] = DEFAULT_OPTIONS[key]
            self._pending_options = options
            return await self.async_step_controls()
        schema = {}
        schema[vol.Required("control_mode", default=current["control_mode"])] = (
            selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "internal", "label": "Integration Thermostats And Presets"},
                        {"value": "external", "label": "External Control / EMHASS"},
                    ]
                )
            )
        )
        schema[vol.Required("heat_pump_model", default=current["heat_pump_model"])] = str
        for key in ("inside_sensor", "inside_humidity_sensor", "outside_sensor"):
            kwargs = {"description": {"suggested_value": current[key]}} if current[key] else {}
            schema[vol.Optional(key, **kwargs)] = selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor",
                    **({"device_class": "humidity"} if key == "inside_humidity_sensor" else {}),
                )
            )
        for key, lower, upper in (
            ("max_start_temperature", 20, 79),
            ("max_hot_water_temperature", 30, 80),
        ):
            kwargs = (
                {"description": {"suggested_value": current[key]}}
                if current[key] is not None
                else {}
            )
            schema[vol.Optional(key, **kwargs)] = WholeDegreeTemperatureSelector(
                selector.NumberSelectorConfig(
                    min=lower,
                    max=upper,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="°C",
                ),
                current[key],
            )
        schema[vol.Required("sensor_timeout", default=current["sensor_timeout"])] = (
            selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=86400,
                    step=60,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            )
        )
        schema[vol.Required("hysteresis", default=current["hysteresis"])] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0.1,
                max=2,
                step=0.1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="°C",
            )
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema),
            errors={},
            description_placeholders={"temperature_sources": self._temperature_sources()},
            last_step=False,
        )

    def _temperature_sources(self):
        coordinator = self._coordinator
        if coordinator is None:
            return "Current temperature sources are unavailable while the integration is unloaded."
        summaries = []
        for location, label in (("inside", "Inside"), ("outside", "Outside")):
            details = coordinator.temperature_source_details(location)
            external_status = details.get("external_status", "not_selected")
            fallback_status = details.get("fallback_status", "unavailable")
            status = temperature_source_status(
                external_status, fallback_status, inside=location == "inside"
            )
            value = _finite_reading(details.get("effective_temperature"))
            reading = f" ({value:g}°C)" if value is not None else ""
            summary = f"{label}: {status}{reading}."
            if details.get("selected_sensor") and external_status != "valid":
                issue = temperature_source_status(
                    external_status, "unavailable", inside=location == "inside"
                )
                summary += f" {issue}."
            summaries.append(summary)
        return " ".join(summaries)

    def _control_defaults(self):
        """Capture the native settings offered here from actual pump readings."""
        coordinator = self._coordinator
        if coordinator is None:
            return {}
        values = {}
        for key in (
            *(f"heat_curve_supply_{index}" for index in range(1, 8)),
            "min_supply_temperature",
            "max_supply_temperature",
            "passive_cooling_supply_target",
        ):
            value = _finite_reading(coordinator.value(key))
            if value is not None:
                values[key] = value
        return values

    def _native_setting_status(self):
        coordinator = self._coordinator
        curve = (
            _finite_reading(coordinator.value("selected_heat_curve"))
            if coordinator is not None
            else None
        )
        selection = (
            f"Selected heat curve: {curve:g} (read only)."
            if curve is not None
            else "The current heat-curve selection is unavailable."
        )
        unsupported = [
            setting.label
            for key, setting in NATIVE_SETTINGS.items()
            if key != "heating_season_stop" and key not in self._displayed_controls
        ]
        status = (
            selection
            + " The seven heat-curve point sliders edit the controller's native supply temperatures."
            + " Desired cooling supply controls Mixing Valve 1; the app's main cooling target may"
            " use a different setting."
        )
        if unsupported:
            status += " Settings without a valid controller reading are omitted: "
            status += ", ".join(unsupported) + "."
        return status

    async def async_step_controls(self, user_input=None):
        """Apply only deliberate changes, with controller readback before saving."""
        if self._pending_options is None:
            self._pending_options = normalize_options(self.config_entry.options)
        if self._displayed_controls is None:
            self._displayed_controls = self._control_defaults()
        errors = {}
        if user_input is not None:
            self._control_input = user_input
            # Device controls may have changed while this two-page form was
            # open. Retain their latest values, rather than an old snapshot.
            options = normalize_options(self.config_entry.options)
            options.update({key: self._pending_options[key] for key in PREFERENCE_KEYS})
            if "smart_grid_mode" in user_input:
                options["smart_grid_mode"] = user_input["smart_grid_mode"]
            changes = {}
            for key, displayed in self._displayed_controls.items():
                if key in user_input and user_input[key] != displayed:
                    options[key] = user_input[key]
                    changes[key] = user_input[key]
            if not errors:
                coordinator = self._coordinator
                if coordinator is None:
                    errors["base"] = "cannot_apply"
                else:
                    try:
                        await coordinator.async_configure_controls(options, control_changes=changes)
                    except HomeAssistantError as error:
                        errors["base"] = "cannot_apply"
                        self._apply_error = str(error)
                    else:
                        return self.async_create_entry(title="", data=options)
        schema = {}
        defaults = self._displayed_controls | (self._control_input or {})
        for key in (
            *(f"heat_curve_supply_{index}" for index in range(1, 8)),
            "min_supply_temperature",
            "max_supply_temperature",
            "passive_cooling_supply_target",
        ):
            if key in self._displayed_controls:
                schema[vol.Required(key, default=defaults[key])] = NativeSettingSelector(
                    NATIVE_SETTINGS[key], self._displayed_controls[key]
                )
        smart_grid = (self._control_input or {}).get(
            "smart_grid_mode", self._pending_options["smart_grid_mode"]
        )
        schema[vol.Required("smart_grid_mode", default=smart_grid)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": "disabled", "label": "Do Not Send Smart Grid Requests"},
                    {"value": "sg_ready", "label": "Controller Is Configured For SG-Ready"},
                    {
                        "value": "power_limit",
                        "label": "Controller Uses Power Limitation / Load-Up",
                    },
                ]
            )
        )
        unavailable = []
        if not self._displayed_controls:
            unavailable.append(
                "Native temperature sliders are unavailable; only the power-control preference is shown."
            )
        if errors.get("base") == "cannot_apply" and getattr(self, "_apply_error", None):
            unavailable.append(self._apply_error)
        return self.async_show_form(
            step_id="controls",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "unavailable_controls": " ".join(unavailable),
                "native_setting_status": self._native_setting_status(),
            },
            last_step=True,
        )
