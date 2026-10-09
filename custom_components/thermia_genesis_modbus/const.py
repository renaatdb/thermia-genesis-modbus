"""Constants for the Thermia Genesis Modbus integration."""

from datetime import timedelta

DOMAIN = "thermia_genesis_modbus"
NAME = "Thermia Genesis Modbus"
MODEL = "Genesis Heat Pump"
CONF_UNIT_ID = "unit_id"
PLATFORMS = ["climate", "sensor", "binary_sensor", "switch", "number"]
SCAN_INTERVAL = timedelta(seconds=30)
DEFAULT_OPTIONS = {
    "control_mode": "internal",
    "heat_pump_model": MODEL,
    "inside_sensor": "",
    "inside_humidity_sensor": "",
    "outside_sensor": "",
    "sensor_timeout": 0,
    "heating_low_offset": 2.0,
    "heating_vacation_temperature": 17.0,
    "heating_vacation_cooling_offset": 0.0,
    "cooling_humidity_enabled": False,
    "cooling_humidity_limit": 80.0,
    "cooling_vacation_humidity_limit": 65.0,
    "cooling_dew_point_margin": 2.0,
    "heating_excess_hours": 12.0,
    "hot_water_excess_hours": 6.0,
    "hot_water_low_hours": 24.0,
    "max_hot_water_temperature": 60.0,
    "max_start_temperature": 55.0,
    "max_heating_temperature": 23.0,
    "heating_excess_heat_stop_offset": 2.0,
    "hysteresis": 0.3,
    "hot_water_hysteresis": 2.0,
    "hot_water_evening_start": 35.0,
    "hot_water_evening_stop": 40.0,
    "smart_grid_mode": "disabled",
    "enable_undocumented_controls": True,
    "hot_water_start": None,
    "hot_water_target": None,
    "hot_water_boost_start": None,
    "hot_water_boost_stop": None,
    "heating_enabled": None,
    "hot_water_enabled": None,
    "passive_cooling_enabled": None,
    "auxiliary_heater_enabled": None,
    "anti_legionella_enabled": None,
    "native_boost_enabled": None,
    "configuration_version": 7,
}


def heat_pump_model_label(value):
    """A user-supplied display label, without inferring controller capabilities."""
    return (value.strip() or MODEL) if isinstance(value, str) else MODEL


def normalize_options(options):
    """Upgrade defaults without changing physical controller settings."""
    current = DEFAULT_OPTIONS | options
    current["heat_pump_model"] = heat_pump_model_label(current["heat_pump_model"])
    if current["control_mode"] not in ("internal", "external"):
        raise ValueError("Control mode must be internal or external")
    # Previously every Configure save recorded the automatic 15-minute limit.
    # Use existing valid HA states by default; retain other chosen age limits.
    if options.get("configuration_version", 0) < 3 and current["sensor_timeout"] == 900:
        current["sensor_timeout"] = 0
    # Preserve valid custom restart margins while raising old smaller values
    # to the current minimum gap for hot-water Excess Energy.
    if options.get("configuration_version", 0) < 4:
        margin = current["hot_water_hysteresis"]
        if not isinstance(margin, bool) and isinstance(margin, (int, float)) and 0.5 <= margin < 1:
            current["hot_water_hysteresis"] = 1.0
    # The previous shared 65% default now belongs to Vacation. Retain chosen
    # custom Normal limits; new-version explicit 65% is also a chosen value.
    if options.get("configuration_version", 0) < 5:
        limit = current["cooling_humidity_limit"]
        if not isinstance(limit, bool) and isinstance(limit, (int, float)) and limit == 65:
            current["cooling_humidity_limit"] = 80.0
    if options.get("configuration_version", 0) < 6:
        margin = current["hot_water_hysteresis"]
        if not isinstance(margin, bool) and isinstance(margin, (int, float)) and margin == 3:
            current["hot_water_hysteresis"] = 2.0
    for key in ("max_start_temperature", "max_hot_water_temperature"):
        if current[key] is None:
            current[key] = DEFAULT_OPTIONS[key]
    current["configuration_version"] = 7
    current.pop("charge_supply_temperature", None)
    # Support is detected from valid model-specific register replies. The old
    # checkbox and adjustable Manual on profile are no longer user controls.
    current["enable_undocumented_controls"] = True
    current["hot_water_boost_start"] = None
    current["hot_water_boost_stop"] = None
    return current
