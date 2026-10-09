"""Thermia alarms, native control signals and actual operating states."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory

from .entity import ThermiaEntity
from .registers import REGISTERS
from .sensor_names import sensor_name


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    entities = [
        ThermiaRegisterBinarySensor(coordinator, spec)
        for spec in REGISTERS
        if spec.space in ("coil", "discrete")
    ]
    entities.extend(
        (
            ThermiaOperatingSensor(coordinator, "heating", {"Heating"}),
            ThermiaOperatingSensor(coordinator, "hot_water", {"Hot water"}),
            ThermiaOperatingSensor(
                coordinator, "cooling", {"Active cooling", "Passive cooling"}
            ),
            ThermiaOperatingSensor(
                coordinator, "anti_legionella", {"Anti legionella"}
            ),
            ThermiaAuxiliaryRunning(coordinator),
        )
    )
    async_add_entities(entities)


class ThermiaRegisterBinarySensor(ThermiaEntity, BinarySensorEntity):
    """A read-only boolean; a missing register is unavailable, never off."""

    def __init__(self, coordinator, spec) -> None:
        super().__init__(coordinator, f"{spec.key}_readback", sensor_name(spec.key))
        self.spec = spec
        self._attr_entity_registry_enabled_default = spec.enabled
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if spec.kind == "alarm":
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM

    @property
    def is_on(self):
        value = self.coordinator.value(self.spec.key)
        return None if value is None else bool(value)

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None


class ThermiaOperatingSensor(ThermiaEntity, BinarySensorEntity):
    """Use all active demand bits, rather than just the highest priority."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, key, demands) -> None:
        entity_key = f"{key}_running"
        super().__init__(coordinator, entity_key, sensor_name(entity_key))
        self.demands = demands

    @property
    def is_on(self):
        if (
            self.coordinator.value("active_demand_flags") is None
            and self.coordinator.value("main_demand") is None
        ):
            return None
        return bool(self.demands.intersection(self.coordinator.active_demands))

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None


class ThermiaAuxiliaryRunning(ThermiaEntity, BinarySensorEntity):
    """Auxiliary heat activity is separate from its permission setting."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator) -> None:
        super().__init__(
            coordinator, "auxiliary_heater_running", sensor_name("auxiliary_heater_running")
        )

    @property
    def is_on(self):
        step = self.coordinator.value("immersion_heater_step")
        external = self.coordinator.value("additional_heater_active")
        if step is not None and step > 0 or external is True:
            return True
        if step is None or external is None:
            return None
        return False

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None
