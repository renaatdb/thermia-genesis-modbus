"""Plain-language explanations of the current control settings."""

from __future__ import annotations

import math
from typing import Any

from .const import DEFAULT_OPTIONS
from .native_settings import NATIVE_SETTINGS
from .temperature import sensor_timeout_seconds


def _number(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and minimum <= number <= maximum else None


def _setting(engine: Any, key: str) -> float:
    try:
        return engine._validate_setting(key, engine.settings.get(key))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_OPTIONS[key]


def _temperature(value: Any, *, room: bool = False) -> str:
    number = _number(value, 10 if room else 0, 35 if room else 90)
    return f"{number:g}°C" if number is not None else "not available"


def control_guide_attributes(coordinator: Any) -> dict[str, str]:
    """Describe selections and preferences without commands or timer changes."""
    engine = coordinator.engine
    state = engine.state
    heating_mode = {
        "off": "Off",
        "heat": "Heat",
        "cool": "Cool",
        "heat_cool": "Auto (Heat/Cool)",
    }.get(state.get("heating_mode"), "Unknown")
    heating_preset = {
        "pv_charge": (
            "Excess Energy (Heating Only)"
            if state.get("heating_mode") == "heat_cool"
            else "Excess Energy"
        ),
        "low": (
            "Low Mode (Heating Only)" if state.get("heating_mode") == "heat_cool" else "Low Mode"
        ),
        "vacation": "Vacation",
    }.get(state.get("heating_preset"), "Normal")
    water_mode = {
        "off": "Off",
        "auto": "Normal",
        "energy_excess": "Excess Energy",
        "evening": "Low Mode",
    }.get(state.get("hot_water_mode"), "Native operation")
    room_ceiling = _setting(engine, "max_heating_temperature")
    heat_stop_offset = _setting(engine, "heating_excess_heat_stop_offset")
    heating_gap = _setting(engine, "hysteresis")
    water_gap = _setting(engine, "hot_water_hysteresis")
    try:
        water_stop, water_start, effective_water_gap = engine._water_limits(once=True)
    except (TypeError, ValueError, OverflowError):
        water_start = None
        water_profile = (
            "Excess Energy temperatures are unavailable with the current limits or restart gap. "
            "A STOP limit of at least 60°C, a valid START limit and a working native Boost control are required. "
        )
        water_restart = (
            "After a ceiling pause, it resumes at the effective START temperature or below. "
        )
    else:
        water_profile = (
            f"Temporarily sets start to {water_start:g}°C and stop to {water_stop:g}°C "
            f"(requested restart gap {water_gap:g}°C; effective restart gap {effective_water_gap:g}°C). "
        )
        water_restart = f"At 60°C it pauses Boost and hot-water demand, then resumes only at {water_start:g}°C or below. "
    heating_hours = _setting(engine, "heating_excess_hours")
    water_hours = _setting(engine, "hot_water_excess_hours")
    water_low_hours = _setting(engine, "hot_water_low_hours")
    heating_low_offset = _setting(engine, "heating_low_offset")
    vacation_temperature = _setting(engine, "heating_vacation_temperature")
    vacation_cooling_offset = _setting(engine, "heating_vacation_cooling_offset")
    humidity_limit = _setting(engine, "cooling_humidity_limit")
    vacation_humidity_limit = _setting(engine, "cooling_vacation_humidity_limit")
    dew_point_margin = _setting(engine, "cooling_dew_point_margin")
    evening_start = _setting(engine, "hot_water_evening_start")
    evening_stop = _setting(engine, "hot_water_evening_stop")
    if evening_start >= evening_stop:
        evening_start = DEFAULT_OPTIONS["hot_water_evening_start"]
        evening_stop = DEFAULT_OPTIONS["hot_water_evening_stop"]
    supply_spec = NATIVE_SETTINGS["max_supply_temperature"]
    native_supply_limit = _number(
        coordinator.value("max_supply_temperature"), supply_spec.min_value, supply_spec.max_value
    )
    supply = (
        f"{native_supply_limit:g}°C (the pump's normal maximum heating supply temperature)"
        if native_supply_limit is not None
        else "not available; a valid native maximum heating supply temperature is required"
    )
    start = _temperature(state.get("hot_water_start"))
    stop = _temperature(state.get("hot_water_target"))
    native_heating_target = _number(coordinator.value("comfort_wheel"), 10, 40)
    timeout = sensor_timeout_seconds(engine.settings.get("sensor_timeout"))
    freshness = (
        f"External readings older than {timeout / 60:g} minutes fall back to Thermia."
        if timeout
        else "Maximum sensor age is off: a valid Home Assistant state does not expire just because it is old."
    )
    return {
        "Control Ownership": (
            "External Control / EMHASS: integration thermostats and presets are disabled. "
            "Native switches and temperature numbers remain available. Solar gains, forecast "
            "preheating and the slow floor response belong to the external automations. "
            "Optional humidity protection and interrupted-write recovery remain active."
            if engine.external_control else
            "Integration Thermostats And Presets: thermostat commands may take ownership. "
            "Select External Control / EMHASS in Configure when external automations decide."
        ),
        "Current Selections": f"Heating: {heating_preset}, {heating_mode}. Hot water: {water_mode}.",
        "Mode And Preset History": (
            "Open the C2.06 Heating — Mode And Preset or C3.08 Hot Water — Mode And Preset sensor to see its history timeline. "
            "Each records the selected mode and preset together, such as Heat · Low Mode. Off has no active preset. "
            "These selections describe the thermostat setting; Operating Status shows what the pump is doing. "
            "The new timelines start when this version is installed and follow Home Assistant's history retention. "
            "The built-in thermostat temperature graph does not add preset timeline lanes."
        ),
        "Heating Normal": (
            "Heat enables native heating and disables cooling. Cool enables native cooling and disables heating. "
            "Normal Heat keeps its permission on regardless of the external room temperature. "
            "Normal Cool does likewise when humidity protection is inactive; active protection can pause cooling. "
            "Thermia still decides whether heating or cooling actually runs. "
            "Auto (Heat/Cool) uses the inside reading to choose heating, cooling or neither between its "
            f"lower and upper targets, with a {heating_gap:g}°C start gap. Off disables both permissions. "
            f"Saved target: {_temperature(state.get('heating_target'), room=True)}; "
            f"Auto range: {_temperature(state.get('heating_low'), room=True)} to "
            f"{_temperature(state.get('heating_high'), room=True)}."
        ),
        "Heating Targets And Thermia": (
            "Heat copies its selected temperature to Thermia's native heating/comfort dial; Auto copies its "
            "lower heating target. The supported range is 10–35°C. "
            f"Thermia currently reports {_temperature(native_heating_target)}. "
            "When Thermia has no native inside sensor, its comfort dial adjusts the heating curve rather than "
            "measuring the room. The external inside reading remains in Home Assistant for Auto and Excess Energy. "
            "There is no documented writable cooling room target in Genesis 17.1: the cooling room target stays "
            "in Home Assistant, and Auto uses its upper target. The cooling supply slider controls water temperature. "
            "Changing the thermostat target during Excess Energy ends charging and applies the new Normal target. "
            "The separate Heating — Excess Energy Ceiling slider changes its charging ceiling without ending the preset. "
            "Changing the native Heating — Heat Stop slider ends Excess Energy, restores its temporary settings, "
            "then applies the new ordinary Heat stop value."
        ),
        "Heating Excess Energy": (
            "Requests extra heating regardless of the Normal room target, using a valid inside reading. "
            "While actively charging, copies the charging ceiling to Thermia's comfort dial. "
            f"Temporarily raises the original Heat stop by {heat_stop_offset:g}°C without stacking across updates. "
            "If the outside temperature remains warmer than that raised threshold, Thermia can still block heating. "
            "The charging ceiling must be 10–35°C. Cooling is off; charging water uses the pump's normal maximum heating supply temperature. "
            f"Pauses at {room_ceiling:g}°C and can resume at {room_ceiling - heating_gap:g}°C. "
            f"Charging supply temperature: {supply}. "
            "At the ceiling, heating pauses and restores the original Heat stop and fixed-supply settings; the comfort dial keeps the charging target. "
            "Leaving the preset also restores the original comfort dial. "
            f"After {heating_hours:g} hours it returns to Normal and restores the previous mode and pump settings. "
            "Without a valid inside reading, this charging run is cancelled and its original settings are restored. "
            "Thermia retains compressor startup timing, demand priorities and safety restrictions."
        ),
        "Heating Low Mode And Vacation": (
            f"Low Mode lowers the saved Normal heating target by {heating_low_offset:g}°C, with a minimum of 10°C. "
            f"Vacation uses a heating target of {vacation_temperature:g}°C. "
            "Low Mode keeps the Normal cooling target. "
            f"Vacation reduces the saved Normal cooling target by {vacation_cooling_offset:g}°C, with a minimum of 10°C. "
            "A 0°C reduction keeps Normal cooling. In Heat/Cool the heating target must remain below the cooling target. "
            "Off ends the active preset and restores saved Normal settings. Presets are hidden until the thermostat is on. "
            "Cool offers Normal and Vacation; Low Mode and Excess Energy are hidden there. "
            "Heat/Cool labels those two heating presets '(Heating Only)'. "
            "Vacation in Cool uses the inside reading to regulate around its derived cooling target. "
            "Selecting Normal restores the saved Normal temperatures. Editing a preset temperature returns to Normal "
            "and applies the changed side while restoring any untouched Normal side."
        ),
        "Cooling Humidity Protection": (
            "Optionally select an inside relative-humidity sensor in Configure. Without one, existing cooling limits apply. "
            "Enable Humidity Protection In Normal Cooling for Cool and Heat/Cool, including Low Mode's cooling. "
            f"Normal protection is currently {'on' if engine.settings.get('cooling_humidity_enabled') else 'off'}. "
            "Vacation uses protection with a selected sensor regardless of that switch. "
            f"Normal and Low Mode cooling pause at {humidity_limit:g}% relative humidity and can resume at {humidity_limit - 2:g}% or below. "
            f"Vacation cooling uses its separate limit of {vacation_humidity_limit:g}% and can resume at {vacation_humidity_limit - 2:g}% or below. "
            "The guard uses the effective inside temperature and humidity to calculate dew point. "
            f"It keeps the cooling-water target at least {dew_point_margin:g}°C above that point, rounded up to whole degrees, "
            "and never colder than the original cooling-water target. If the required water target cannot be verified, "
            "or the selected humidity/inside-temperature reading is invalid or stale, cooling pauses. "
            "The Inside Humidity, Inside Dew Point and Cooling Humidity Protection sensors show its inputs and status. "
            "Floor cooling does not dehumidify the air. This software guard does not measure actual floor-surface temperature."
        ),
        "Hot Water Normal": (
            f"Saved Normal start: {start}; stop: {stop}. Thermia applies this pair using its own tank-control logic. "
            "Keep Thermia's own hot-water mode on Normal and native Boost off to use the configured start/stop pair. "
            "Native Boost on can override ordinary programme behaviour even though these settings remain unchanged. "
            "Adjust this pair on the Hot Water thermostat. Off disables the hot-water request."
        ),
        "Hot Water Temperature Readings": (
            "The Hot Water thermostat displays the weighted tank temperature when valid, with the top reading "
            "as fallback. Its attributes also show the top, lower and weighted readings. "
            "Measured temperatures retain their source precision; target adjustments use whole degrees. "
            "In Normal and Low Mode, Thermia decides which readings trigger a cycle; the displayed temperature "
            "alone does not identify that decision. "
            "For Excess Energy the integration uses the hotter valid top/weighted reading both to pause at "
            "the ceiling and to decide when it can restart."
        ),
        "Thermostat Temperature Changes": (
            "Changing any thermostat temperature returns every active preset to Normal in every active HVAC mode. "
            "Thermostat temperature actions in automations follow the same rule. "
            "Only the end you change replaces its saved Normal value; the other end returns to its saved Normal value unless the pair would cross. "
            "For Normal 45/50°C and Excess Energy 55/60°C, changing STOP to 59°C gives Normal 45/59°C, "
            "while changing START to 46°C gives 46/50°C. From Low Mode 35/40°C, START 36°C gives 36/50°C. "
            "Changing both ends applies both changes; submitting the unchanged preset pair does nothing. "
            "A requested START above its configured maximum is capped before applying the edit: "
            "with the default maximum of 55°C, a 57/60°C request becomes 55/60°C without an error. "
            "The thermostat confirms the accepted value even when the pump already has that value. "
            "If the resulting Normal START is equal to or above STOP, it automatically creates a 5°C gap. "
            "Editing STOP keeps that value and lowers START; editing only START raises STOP. If both moved, STOP takes priority. "
            "The pair shifts together when necessary to stay within 30–60°C and the configured pump limits. "
            "For example, a STOP edit to 54°C with saved Normal START 54°C gives 49/54°C. "
            "A Low Mode STOP edit to 41°C with saved Normal START 45°C gives 36/41°C. "
            "Valid existing pairs, including gaps below 5°C, are retained. If no legal 5°C pair fits, the preset, settings and timer remain unchanged. "
            "Home Assistant keeps the displayed handles from crossing; a lower STOP may need another adjustment after Normal returns. "
            "A heating edit preserves the displayed mode, applies its target and clears its charging timer. "
            "While Off, target adjustment is disabled and saved Normal temperatures are retained. "
            "Turn the thermostat on before editing, or include an active HVAC mode in the same temperature action. "
            "Temperature adjustments use whole degrees. Invalid input temperatures, STOP above its maximum "
            "or limits that cannot fit a valid pair are rejected before a preset or timer is changed."
        ),
        "Hot Water Excess Energy": (
            water_profile
            + "The requested restart gap is configurable from 1 to 30°C; its default is 2°C. "
            "START is the lower of 60°C minus that gap and the configured maximum START. "
            "The default limits are START 55°C and STOP 60°C, so this controller uses an effective 5°C gap even when 1°C or 2°C is selected. "
            "Below the 60°C ceiling, selecting the preset immediately requests native Thermia Boost, "
            "even above the restart temperature. The hotter valid tank reading enforces the ceiling. "
            + water_restart
            + f"The preset stays selected during this pause. After {water_hours:g} hours, or when you select Normal "
            "or Off, Boost is turned off and the saved Normal temperatures are restored. "
            "Confirmed pump limits and a working native Boost control are required."
        ),
        "Hot Water Low Mode": (
            f"Uses ordinary Thermia automatic hot water with start {evening_start:g}°C and stop {evening_stop:g}°C; "
            "native Boost is off. Water can cool naturally to the start temperature before reheating. "
            "Edit the Low Mode profile using the device sliders. A thermostat temperature change returns to Normal and applies the edited Normal range. "
            "Select Normal or Off to restore the saved Normal pair. "
            "Select Low Mode yourself or through an automation; it has no automatic schedule. "
            f"Low Mode needs no inside reading and returns to Normal after {water_low_hours:g} hours. "
            "Selecting it again explicitly restarts that timer. Polling or editing its configured START/STOP does not. "
            "Thermia's own hygiene cycles remain separate."
        ),
        "Timers And Reset": (
            f"Heating Excess Energy is limited to {heating_hours:g} hours; hot-water Excess Energy to {water_hours:g} hours. "
            f"Hot-water Low Mode is limited to {water_low_hours:g} hours before restoring Normal START/STOP. "
            "Edit these durations using the device sliders. Explicitly selecting Excess Energy again restarts only that "
            "thermostat's timer; selecting Normal and then Excess Energy also starts a fresh timer. "
            "Regular updates and temperature pauses do not restart either timer. "
            "Restarting Home Assistant restores temporary overrides instead of resuming a charging run."
        ),
        "Time Remaining": (
            "The two Excess Energy and the Hot-water Low Mode time remaining sensors show hours left and update every 30 seconds, "
            "including while the heat pump is offline. Zero means the timer has finished or the preset is inactive; "
            "it does not confirm that the pump settings have been restored. "
            "If the pump is offline at expiry, reset remains pending until the connection allows restoration."
        ),
        "Inside And Outside Sensors": (
            "Valid sensors selected in Configure take priority over Thermia's own inside and outside readings. "
            "Missing, unknown, unavailable or invalid external values fall back to a valid Thermia reading. "
            f"{freshness} Auto, extra heating and Vacation cooling need a valid inside reading. "
            "Normal Heat and unguarded Normal Cool do not. Active humidity protection also requires valid inside temperature and humidity. "
            "The heating thermostat also shows current inside humidity when a selected sensor has a valid reading. "
            "Without a selected humidity sensor, that value is omitted."
        ),
        "Native Switches": (
            "Heating and Cooling (Passive) change the pump's native permissions; they do not force the compressor "
            "to run. The Hot Water Enabled switch controls the hot-water function. "
            "The Thermia Boost switch changes only native Boost, leaving the temperature pair unchanged; "
            "it cannot oppose the Excess Energy or Low Mode preset. "
            "Electric Auxiliary Heater Allowed permits automatic electric assistance; it does not force heating. "
            "Anti-Legionella Programme enables or disables the pump's own hygiene programme. "
            "The pump retains demand priorities and startup restrictions. Operating Status shows what it is actually doing."
        ),
        "Where To Change Settings": (
            "Configure page 1: model, external sensors, verified hot-water limits, sensor freshness and room restart margin. "
            "Configure page 2: heat curve, heating-water limits, cooling-water target and heat-pump power-control mode. "
            "Device Configuration: all A1 switches first; then B1 Heating, B2 Hot water and B3 Cooling sliders. "
            "The hot-water Excess Energy restart gap is a device slider, configurable from 1 to 30°C with a 2°C default. "
            "The default pump limits are START 55°C and STOP 60°C; Excess Energy therefore uses 55/60°C on this controller. "
            "New duration edits use whole hours from 1 to 168; existing fractional durations keep their saved clocks until changed. "
            "Thermostats: modes, presets and saved Normal target temperatures."
        ),
    }
