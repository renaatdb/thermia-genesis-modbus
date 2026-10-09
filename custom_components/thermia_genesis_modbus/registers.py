"""Thermia domestic Genesis 17.1 Modbus register catalogue.

Addresses are zero based, as in the manufacturer's Address column. Scales
multiply the wire value. Source: Thermia ACMBDH01UG0402, Genesis 17.1,
Atlas / Calibra / Calibra E / Calibra Cool / Calibra RXT / Diplomat Inverter.
https://odoo.geotherma.be/shop/th-206943-thermia-calibra-e-cool-8-3x400v-n-13581/document/1097

Optional accessories and advanced settings are represented, but their entities
start disabled. A missing accessory or unsupported register must be isolated by
the transport rather than making the whole device unavailable.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegisterSpec:
    """Description shared by polling, decoding and entity platforms."""

    key: str
    name: str
    space: str
    address: int
    scale: float = 1
    signed: bool = False
    count: int = 1
    word_order: str = "big"
    unit: str | None = None
    kind: str = "number"
    enabled: bool = True
    diagnostic: bool = False
    writable: bool = False
    min_value: float | None = None
    max_value: float | None = None
    missing: bool = True


STATUS_BY_CODE = {
    1: "Manual operation",
    2: "Defrost",
    3: "Hot water",
    4: "Heating",
    5: "Active cooling",
    6: "Pool",
    7: "Anti legionella",
    8: "Passive cooling",
    98: "Standby",
    99: "Idle",
    100: "Off",
}
ACTIVE_DEMAND_BITS = {
    0: "Manual operation",
    1: "Defrost",
    2: "Hot water",
    3: "Heating",
    4: "Active cooling",
    5: "Pool",
    6: "Anti legionella",
    7: "Passive cooling",
    9: "Standby",
    10: "Idle",
    11: "Off",
}
SMART_GRID_STATUS_BY_CODE = {1: "EVU", 4: "Normal", 5: "Comfort", 6: "Boost"}

# These two controls exist in the supplied Calibra integration, but are absent
# from the domestic Genesis 17.1 public protocol. Probe model support and
# require a valid binary readback before accepting either native control.
UNVERIFIED_KEYS = frozenset({"anti_legionella_enabled", "hot_water_boost"})

# These are separate physical measurement points, not interchangeable fallback
# readings. The manufacturer marks input 12 as an expansion-module point.
TEMPERATURE_POINTS = {
    "condenser_in_temperature": {
        "measurement_location": "Heat-pump condenser water inlet",
        "description": "Water returning to the heat pump; separate from the system return probe.",
    },
    "condenser_out_temperature": {
        "measurement_location": "Heat-pump condenser water outlet",
        "description": "Water leaving the heat pump; separate from the system supply probe.",
    },
    "supply_temperature": {
        "measurement_location": "Heating system supply line",
        "description": "The published register is marked EM; availability depends on the configured system probe.",
    },
    "return_temperature": {
        "measurement_location": "Heating system return line",
        "description": "A separate system return measurement; the controller must supply a valid reading.",
    },
    "indoor_temperature": {
        "measurement_location": "Thermia room sensor",
        "description": "Requires a Thermia room-temperature reading; a selected external sensor can supply Effective inside instead.",
    },
}


def _input(
    key,
    name,
    address,
    *,
    scale=1,
    signed=False,
    unit=None,
    enabled=True,
    diagnostic=False,
    kind="number",
    missing=True,
    count=1,
    word_order="big",
):
    return RegisterSpec(
        key,
        name,
        "input",
        address,
        scale,
        signed,
        count,
        word_order,
        unit,
        kind,
        enabled,
        diagnostic,
        missing=missing,
    )


def _holding(
    key,
    name,
    address,
    *,
    scale=1,
    signed=False,
    unit=None,
    min_value=None,
    max_value=None,
    kind="number",
    missing=True,
):
    return RegisterSpec(
        key,
        name,
        "holding",
        address,
        scale=scale,
        signed=signed,
        unit=unit,
        kind=kind,
        enabled=False,
        diagnostic=True,
        writable=True,
        min_value=min_value,
        max_value=max_value,
        missing=missing,
    )


def _binary(
    key, name, space, address, *, enabled=False, diagnostic=True, writable=False, kind="binary"
):
    return RegisterSpec(
        key,
        name,
        space,
        address,
        kind=kind,
        enabled=enabled,
        diagnostic=diagnostic,
        writable=writable,
        missing=False,
    )


_INPUTS = (
    _input("main_demand", "Operating status", 1, kind="enum", missing=False),
    _input(
        "active_demand_flags",
        "Active demand flags",
        2,
        kind="bitfield",
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input(
        "compressor_available_gears",
        "Compressor available gears",
        4,
        scale=0.01,
        enabled=False,
        diagnostic=True,
    ),
    _input("compressor_speed_rpm", "Compressor speed", 5, unit="rpm"),
    _input(
        "additional_heater_demand",
        "External additional heater demand",
        6,
        scale=0.01,
        unit="%",
        enabled=False,
    ),
    _input(
        "discharge_temperature", "Discharge pipe temperature", 7, scale=0.01, signed=True, unit="°C"
    ),
    _input(
        "condenser_in_temperature",
        "Heat-pump return temperature (condenser inlet)",
        8,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _input(
        "condenser_out_temperature",
        "Heat-pump supply temperature (condenser outlet)",
        9,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _input(
        "brine_in_temperature", "Brine inlet temperature", 10, scale=0.01, signed=True, unit="°C"
    ),
    _input(
        "brine_out_temperature", "Brine outlet temperature", 11, scale=0.01, signed=True, unit="°C"
    ),
    _input(
        "supply_temperature", "System supply temperature", 12, scale=0.01, signed=True, unit="°C"
    ),
    _input(
        "outdoor_temperature", "Thermia outside temperature", 13, scale=0.01, signed=True, unit="°C"
    ),
    _input(
        "hot_water_top_temperature",
        "Hot water top temperature",
        15,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _input(
        "hot_water_lower_temperature",
        "Hot water lower temperature",
        16,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _input(
        "hot_water_weighted_temperature",
        "Hot water weighted temperature",
        17,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _input(
        "calculated_supply_target",
        "Calculated supply target",
        18,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _input(
        "selected_heat_curve",
        "Selected heat curve",
        19,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
    ),
    *(
        _input(
            f"heat_curve_outdoor_{i + 1}",
            f"Heat curve outside point {i + 1}",
            20 + i,
            scale=0.01,
            signed=True,
            unit="°C",
            enabled=False,
            diagnostic=True,
        )
        for i in range(7)
    ),
    _input(
        "return_temperature", "System return temperature", 27, scale=0.01, signed=True, unit="°C"
    ),
    _input(
        "calculated_heat_demand",
        "Calculated heat demand",
        30,
        scale=0.01,
        signed=True,
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "cooling_integral",
        "Cooling season integral",
        36,
        signed=True,
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input("condenser_pump_speed", "Condenser circulation pump speed", 39, scale=0.01, unit="%"),
    _input(
        "mix_valve_temperature",
        "Mix valve 1 supply temperature",
        40,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
    ),
    _input(
        "buffer_temperature",
        "Buffer tank temperature",
        41,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
    ),
    _input("mix_valve_position", "Mix valve 1 position", 43, scale=0.01, unit="%", enabled=False),
    _input("brine_pump_speed", "Brine circulation pump speed", 44, scale=0.01, unit="%"),
    _input("hot_water_valve_position", "Hot water valve position", 47, unit="%", enabled=False),
    _input(
        "compressor_operating_hours",
        "Compressor operating hours",
        48,
        count=2,
        unit="h",
        missing=False,
    ),
    _input(
        "hot_water_operating_hours",
        "Hot water operating hours",
        50,
        count=2,
        unit="h",
        missing=False,
    ),
    _input(
        "additional_heater_operating_hours",
        "External additional heater operating hours",
        52,
        count=2,
        unit="h",
        enabled=False,
        missing=False,
    ),
    _input("compressor_speed_percent", "Compressor speed percentage", 54, scale=0.01, unit="%"),
    _input(
        "second_demand",
        "Second running demand",
        55,
        kind="enum",
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input(
        "third_demand",
        "Third running demand",
        56,
        kind="enum",
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input(
        "start_restriction_timer",
        "Compressor start restriction",
        60,
        unit="s",
        diagnostic=True,
        missing=False,
    ),
    _input("compressor_current_gear", "Compressor current gear", 61, scale=0.01),
    *(
        _input(
            f"queued_demand_{i + 1}",
            f"Queued demand {i + 1}",
            62 + i,
            kind="enum",
            enabled=False,
            diagnostic=True,
            missing=False,
        )
        for i in range(5)
    ),
    _input("immersion_heater_step", "Internal immersion heater active step", 67, missing=False),
    _input(
        "buffer_charge_target",
        "Buffer tank charge target",
        68,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
    ),
    *(
        _input(
            f"meter_current_l{i + 1}",
            f"Electric meter L{i + 1} current",
            69 + i,
            scale=0.01,
            unit="A",
            enabled=False,
        )
        for i in range(3)
    ),
    *(
        _input(
            f"meter_voltage_l{i + 1}_neutral",
            f"Electric meter L{i + 1} to neutral voltage",
            72 + i,
            scale=0.01,
            unit="V",
            enabled=False,
        )
        for i in range(3)
    ),
    *(
        _input(key, name, address, scale=0.1, unit="V", enabled=False)
        for key, name, address in (
            ("meter_voltage_l1_l2", "Electric meter L1 to L2 voltage", 75),
            ("meter_voltage_l2_l3", "Electric meter L2 to L3 voltage", 76),
            ("meter_voltage_l3_l1", "Electric meter L3 to L1 voltage", 77),
        )
    ),
    *(
        _input(
            f"meter_power_l{i + 1}",
            f"Electric meter L{i + 1} power",
            78 + i,
            unit="W",
            enabled=False,
        )
        for i in range(3)
    ),
    _input(
        "meter_energy_integer",
        "Electric meter energy whole kWh",
        81,
        unit="kWh",
        enabled=False,
        missing=False,
    ),
    _input(
        "smart_grid_status",
        "Current Smart Grid mode",
        82,
        kind="enum",
        diagnostic=True,
        missing=False,
    ),
    _input(
        "meter_energy",
        "Electric meter total energy",
        83,
        scale=0.1,
        count=2,
        word_order="little",
        unit="kWh",
        enabled=False,
        missing=False,
    ),
    _input(
        "cooling_supply_temperature",
        "Cooling circuit supply temperature",
        106,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
    ),
    _input(
        "indoor_temperature", "Thermia inside temperature", 121, scale=0.1, signed=True, unit="°C"
    ),
    _input(
        "high_pressure_bubble_temperature",
        "High pressure bubble point",
        122,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "high_pressure_dew_temperature",
        "High pressure dew point",
        123,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "low_pressure_dew_temperature",
        "Low pressure dew point",
        124,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "superheat",
        "Superheat temperature difference",
        125,
        scale=0.01,
        signed=True,
        unit="K",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "subcooling",
        "Subcooling temperature difference",
        126,
        scale=0.01,
        signed=True,
        unit="K",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "low_pressure",
        "Low side pressure",
        127,
        scale=0.01,
        signed=True,
        unit="bar",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "high_pressure",
        "High side pressure",
        128,
        scale=0.01,
        signed=True,
        unit="bar",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "liquid_line_temperature",
        "Liquid line temperature",
        129,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "suction_gas_temperature",
        "Suction gas temperature",
        130,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "heating_integral",
        "Heating season integral",
        131,
        signed=True,
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input("cooling_mix_valve_position", "Cooling mix valve opening", 137, unit="%", enabled=False),
    *(
        _input(
            f"desired_gear_{key}",
            f"Desired gear for {label}",
            address,
            enabled=False,
            diagnostic=True,
        )
        for key, label, address in (
            ("hot_water", "hot water", 139),
            ("heating", "heating", 140),
            ("cooling", "cooling", 141),
            ("pool", "pool", 142),
        )
    ),
    _input(
        "available_secondaries",
        "Available Genesis secondary units",
        143,
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input(
        "distributed_gears",
        "Total distributed gears",
        145,
        enabled=False,
        diagnostic=True,
        missing=False,
    ),
    _input("maximum_requested_gear", "Maximum requested gear", 146, enabled=False, diagnostic=True),
    _input(
        "mix_valve_target",
        "Mix valve 1 distribution target",
        147,
        scale=0.01,
        signed=True,
        unit="°C",
        enabled=False,
    ),
    _input("thermal_power", "Heat pump thermal power", 157, scale=0.01, unit="kW"),
    _input("electric_power", "Heat pump electrical power", 158, scale=0.01, unit="kW"),
    *(
        _input(key, name, address, kind="bitfield", enabled=False, diagnostic=True, missing=False)
        for key, name, address in (
            ("secondary_class_d_flags", "Secondary class D alarm flags", 160),
            ("secondary_communication_flags", "Secondary communication alarm flags", 161),
            ("secondary_class_a_flags", "Secondary class A alarm flags", 162),
            ("secondary_class_b_flags", "Secondary class B alarm flags", 163),
        )
    ),
    *(
        _input(
            f"software_{key}",
            f"Control software {key} version",
            address,
            enabled=False,
            diagnostic=True,
            missing=False,
        )
        for key, address in (("major", 311), ("minor", 312), ("micro", 313))
    ),
    _input(
        "expansion_valve_opening",
        "Expansion valve opening",
        315,
        scale=0.01,
        unit="%",
        enabled=False,
        diagnostic=True,
    ),
    _input(
        "inverter_temperature",
        "Inverter temperature",
        319,
        signed=True,
        unit="°C",
        enabled=False,
        diagnostic=True,
    ),
)

_HOLDINGS = (
    _holding(
        "operational_mode",
        "Operational mode setting",
        0,
        kind="enum",
        missing=False,
        min_value=1,
        max_value=3,
    ),
    _holding(
        "max_supply_temperature",
        "Maximum heating curve supply temperature",
        3,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _holding(
        "min_supply_temperature",
        "Minimum heating curve supply temperature",
        4,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    # Domestic Genesis 17.1 uses a direct temperature here. A live 17.01
    # comparison confirmed raw 23 degrees matches the Thermia comfort dial.
    # Other Genesis families can use an offset; the engine checks the original
    # reading before synchronizing this domestic room target.
    _holding(
        "comfort_wheel",
        "Comfort wheel raw temperature setting",
        5,
        scale=0.01,
        signed=True,
        unit="°C",
        min_value=10,
        max_value=40,
    ),
    *(
        _holding(
            f"heat_curve_supply_{i + 1}",
            f"Heat curve supply point {i + 1}",
            6 + i,
            scale=0.01,
            signed=True,
            unit="°C",
        )
        for i in range(7)
    ),
    _holding(
        "heating_season_stop",
        "Heating season stop temperature",
        16,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _holding(
        "hot_water_start", "Hot water start temperature", 22, scale=0.01, signed=True, unit="°C"
    ),
    _holding(
        "hot_water_stop", "Hot water stop temperature", 23, scale=0.01, signed=True, unit="°C"
    ),
    *(
        _holding(key, label, address)
        for key, label, address in (
            ("min_heating_gear", "Minimum heating gear", 26),
            ("max_heating_gear", "Maximum heating gear", 27),
            ("max_hot_water_gear", "Maximum hot water gear", 28),
            ("min_hot_water_gear", "Minimum hot water gear", 29),
        )
    ),
    _holding("cooling_mix_valve_setting", "Cooling mix valve set point", 30, scale=0.01, unit="%"),
    _holding(
        "cooling_mix_valve_min", "Cooling mix valve minimum opening", 49, scale=0.01, unit="%"
    ),
    _holding(
        "cooling_mix_valve_max", "Cooling mix valve maximum opening", 50, scale=0.01, unit="%"
    ),
    _holding("pool_charge_setting", "Pool charge set point", 58, scale=0.01, unit="%"),
    *(
        _holding(f"gear_shift_delay_{key}", f"Gear shift delay {key}", address, unit="min")
        for key, address in (("heating", 61), ("pool", 62), ("cooling", 63))
    ),
    *(
        _holding(key, name, address, scale=0.01, signed=True, unit=unit)
        for key, name, address, unit in (
            ("brine_in_high_limit", "Brine inlet high alarm limit", 67, "°C"),
            ("brine_in_low_limit", "Brine inlet low alarm limit", 68, "°C"),
            ("brine_out_low_limit", "Brine outlet low alarm limit", 69, "°C"),
            ("brine_max_delta", "Brine maximum temperature difference", 70, "K"),
        )
    ),
    _holding(
        "additional_heater_start", "External additional heater start PID sum", 75, signed=True
    ),
    _holding(
        "additional_heater_stop",
        "External additional heater stop PID sum",
        78,
        scale=0.01,
        signed=True,
    ),
    *(
        _holding(key, name, address, scale=0.01, unit="%")
        for key, name, address in (
            ("condenser_pump_min_speed", "Condenser pump minimum speed", 76),
            ("brine_pump_min_speed", "Brine pump minimum speed", 77),
            ("condenser_pump_max_speed", "Condenser pump maximum speed", 79),
            ("brine_pump_max_speed", "Brine pump maximum speed", 80),
            ("condenser_pump_standby_speed", "Condenser pump standby speed", 81),
            ("brine_pump_standby_speed", "Brine pump standby speed", 82),
        )
    ),
    *(
        _holding(key, name, address)
        for key, name, address in (
            ("min_pool_gear", "Minimum pool gear", 85),
            ("max_pool_gear", "Maximum pool gear", 86),
            ("min_cooling_gear", "Minimum cooling gear", 87),
            ("max_cooling_gear", "Maximum cooling gear", 88),
        )
    ),
    _holding("cooling_start", "Cooling start temperature", 105, scale=0.01, signed=True, unit="°C"),
    _holding("cooling_stop", "Cooling stop temperature", 106, scale=0.01, signed=True, unit="°C"),
    _holding(
        "mix_valve_min_supply",
        "Mix valve 1 minimum supply",
        107,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _holding(
        "mix_valve_max_supply",
        "Mix valve 1 maximum supply",
        108,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    *(
        _holding(
            f"mix_valve_curve_supply_{i + 1}",
            f"Mix valve 1 heat curve point {i + 1}",
            109 + i,
            scale=0.01,
            signed=True,
            unit="°C",
        )
        for i in range(7)
    ),
    _holding("fixed_supply_target", "Fixed supply target", 116, scale=0.01, signed=True, unit="°C"),
    _holding(
        "outdoor_source",
        "Thermia outside temperature source",
        117,
        kind="enum",
        min_value=0,
        max_value=1,
        missing=False,
    ),
    _holding(
        "bms_outdoor_temperature",
        "BMS outside temperature",
        118,
        scale=0.01,
        signed=True,
        unit="°C",
        min_value=-50,
        max_value=200,
    ),
    _holding("max_phase_current", "Maximum phase current", 119, unit="A"),
    _holding("compressor_current_hysteresis", "Compressor current hysteresis", 120, unit="A"),
    _holding(
        "smart_grid_request",
        "Desired power consumption control",
        124,
        kind="enum",
        min_value=0,
        max_value=3,
        missing=False,
    ),
    _holding("input_power_limit", "Input power limit", 125, scale=0.1, unit="kW"),
    _holding(
        "mix_valve_mode",
        "Mix valve 1 selected mode",
        298,
        kind="enum",
        min_value=0,
        max_value=2,
        missing=False,
    ),
    _holding(
        "pool_return_target",
        "Pool return temperature target",
        299,
        scale=0.1,
        signed=True,
        unit="°C",
    ),
    _holding("pool_hysteresis", "Pool hysteresis", 300, scale=0.1, unit="K"),
    _holding(
        "passive_cooling_supply_target",
        "Passive cooling supply target",
        302,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _holding(
        "minimum_outdoor_for_cooling",
        "Minimum outside temperature for cooling",
        303,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _holding(
        "additional_heater_outdoor_limit",
        "External heater outside temperature limit",
        304,
        scale=0.01,
        signed=True,
        unit="°C",
    ),
    _holding(
        "immersion_heater",
        "Internal immersion heater allowed",
        321,
        kind="enum",
        min_value=0,
        max_value=2,
        missing=False,
    ),
    _holding(
        "hot_water_boost",
        "Unverified hot water boost request",
        6257,
        kind="enum",
        min_value=0,
        max_value=1,
        missing=False,
    ),
)

_COIL_ROWS = (
    ("alarm_reset", "Alarm reset control", 3),
    ("additional_heater_enabled", "External additional heater enabled", 5),
    ("flow_switch_enabled", "Flow or pressure switch enabled", 7),
    ("hot_water_enabled", "Hot water enabled", 8),
    ("heating_enabled", "Heating enabled", 9),
    ("active_cooling_enabled", "Active cooling enabled", 10),
    ("mix_valve_enabled", "Mix valve 1 enabled", 11),
    ("brine_out_monitoring_enabled", "Brine outlet monitoring enabled", 20),
    ("brine_pump_continuous", "Brine pump continuous operation", 21),
    ("system_pump_enabled", "System circulation pump enabled", 22),
    ("anti_legionella_enabled", "Unverified anti legionella enabled", 24),
    ("additional_heater_only", "Additional heater only operation", 25),
    ("current_limitation_enabled", "Current limitation enabled", 26),
    ("pool_enabled", "Pool enabled", 28),
    ("pool_additional_heater_enabled", "External heater for pool enabled", 31),
    ("passive_cooling_enabled", "Passive cooling enabled", 33),
    ("condenser_pump_variable_speed", "Condenser pump variable speed enabled", 34),
    ("brine_pump_variable_speed", "Brine pump variable speed enabled", 35),
    ("mix_valve_cooling_outdoor_dependent", "Mix valve cooling outside dependence", 37),
    ("brine_pump_during_cooling", "Brine pump during mix valve cooling", 38),
    ("additional_heater_outdoor_dependent", "External heater outside dependence", 39),
    ("brine_in_monitoring_enabled", "Brine inlet monitoring enabled", 40),
    ("fixed_supply_enabled", "Fixed supply target enabled", 41),
    ("evaporator_freeze_protection", "Evaporator freeze protection enabled", 42),
    ("condenser_pump_continuous", "Condenser pump continuous operation", 59),
    ("current_limit_external_heater", "Current limiter restricts external heater", 60),
    ("current_limit_secondaries", "Current limiter restricts secondary units", 61),
)

_DISCRETE_ROWS = (
    ("alarm_class_a", "Class A alarm", 0),
    ("alarm_class_b", "Class B alarm", 1),
    ("alarm_class_c", "Class C alarm", 2),
    ("alarm_class_d", "Class D secondary alarm", 3),
    ("high_pressure_alarm", "High pressure switch alarm", 9),
    ("low_pressure_alarm", "Low pressure level alarm", 10),
    ("discharge_high_alarm", "High discharge temperature alarm", 11),
    ("pressure_limit_active", "Operating pressure limit active", 12),
    ("discharge_sensor_alarm", "Discharge pipe sensor alarm", 13),
    ("liquid_sensor_alarm", "Liquid line sensor alarm", 14),
    ("suction_sensor_alarm", "Suction gas sensor alarm", 15),
    ("flow_switch_alarm", "Flow or pressure switch alarm", 16),
    ("phase_detection_alarm", "Power phase detection alarm", 22),
    ("inverter_alarm", "Inverter alarm", 23),
    ("supply_low_alarm", "System supply low temperature alarm", 24),
    ("compressor_low_speed_alarm", "Compressor low speed alarm", 25),
    ("low_superheat_alarm", "Low superheat alarm", 26),
    ("pressure_ratio_alarm", "Pressure ratio out of range alarm", 27),
    ("compressor_envelope_alarm", "Compressor pressure envelope alarm", 28),
    ("brine_temperature_alarm", "Brine temperature out of range alarm", 29),
    ("brine_in_sensor_alarm", "Brine inlet sensor alarm", 30),
    ("brine_out_sensor_alarm", "Brine outlet sensor alarm", 31),
    ("condenser_in_sensor_alarm", "Condenser inlet sensor alarm", 32),
    ("condenser_out_sensor_alarm", "Condenser outlet sensor alarm", 33),
    ("outdoor_sensor_alarm", "Outside sensor alarm", 34),
    ("supply_sensor_alarm", "System supply sensor alarm", 35),
    ("mix_valve_sensor_alarm", "Mix valve 1 supply sensor alarm", 36),
    ("cooling_supply_sensor_alarm", "Cooling supply sensor alarm", 47),
    ("brine_delta_alarm", "Brine temperature difference alarm", 49),
    ("hot_water_mid_sensor_alarm", "Hot water mid sensor alarm", 50),
    ("brine_in_high_alarm", "Brine inlet high temperature alarm", 55),
    ("brine_in_low_alarm", "Brine inlet low temperature alarm", 56),
    ("brine_out_low_alarm", "Brine outlet low temperature alarm", 57),
    ("mix_valve_deviation_alarm", "Mix valve 1 supply deviation alarm", 60),
    ("sum_alarm", "Sum alarm", 66),
    ("cooling_supply_deviation_alarm", "Cooling supply deviation alarm", 67),
    ("room_sensor_alarm", "Room temperature sensor alarm", 74),
    ("inverter_communication_alarm", "Inverter communication alarm", 75),
    ("pool_sensor_alarm", "Pool return sensor alarm", 76),
    ("pool_external_stop", "Pool external stop", 77),
    ("brine_external_start", "External brine pump start input", 78),
    ("ground_water_relay_active", "Brine or ground water external relay", 79),
    ("secondary_unit_alarm", "Secondary unit communication alarm", 83),
    ("primary_conflict_alarm", "Primary unit network conflict alarm", 84),
    ("primary_missing_secondary_alarm", "Primary unit missing secondary alarm", 85),
    ("oil_boost_active", "Oil boost in progress", 86),
    ("hot_water_top_sensor_alarm", "Hot water top sensor alarm", 87),
    ("compressor_signal", "Compressor control signal", 199),
    ("smart_grid_input_1", "Smart Grid input 1", 201),
    ("external_alarm", "External alarm input", 202),
    ("smart_grid_input_2", "Smart Grid input 2", 204),
    ("additional_heater_signal", "External additional heater control signal", 206),
    ("mix_valve_pump_signal", "Mix valve 1 pump control signal", 209),
    ("condenser_pump_signal", "Condenser pump control signal", 210),
    ("system_pump_signal", "System circulation pump control signal", 211),
    ("brine_pump_signal", "Brine pump control signal", 218),
    ("external_heater_pump_signal", "External heater pump control signal", 219),
    ("heating_season_active", "Heating season active", 220),
    ("additional_heater_active", "External additional heater active", 221),
    ("heat_pump_stopping", "Heat pump stopping", 224),
    ("heat_pump_ok_to_start", "Heat pump ready to start", 225),
    ("pool_directional_valve", "Pool directional valve position", 235),
    ("cooling_pump_signal", "Cooling circulation pump control signal", 236),
    ("surplus_heat_directional_valve", "Surplus heat directional valve position", 238),
    ("cooling_regulation_signal", "Cooling regulation control signal", 240),
    ("active_cooling_directional_valve", "Active cooling directional valve position", 242),
    ("mix_valve_passive_cooling_active", "Mix valve 1 passive cooling active", 245),
    ("compressor_speed_limited", "Compressor unable to increase speed", 246),
)

REGISTERS = (
    *_INPUTS,
    *_HOLDINGS,
    *(_binary(key, name, "coil", address, writable=True) for key, name, address in _COIL_ROWS),
    *(
        _binary(
            key,
            name,
            "discrete",
            address,
            enabled=key in {"alarm_class_a", "alarm_class_b", "alarm_class_c"},
            kind="alarm" if "alarm" in key else "binary",
        )
        for key, name, address in _DISCRETE_ROWS
    ),
)
REGISTERS_BY_KEY = {spec.key: spec for spec in REGISTERS}
