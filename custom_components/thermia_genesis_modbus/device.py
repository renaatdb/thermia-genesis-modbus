"""Read and verify Thermia registers over Home Assistant's shared Modbus unit."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from modbus_connection import IllegalDataAddressError, IllegalFunctionError

from .registers import REGISTERS, RegisterSpec

OPTIONAL_WRITES = {"anti_legionella_enabled", "hot_water_boost"}


def decode(spec: RegisterSpec, words: list[int | bool]) -> float | int | bool | None:
    """Decode signed temperatures and counters without treating counters as sentinels."""
    if len(words) != spec.count:
        raise ValueError(f"Short response for {spec.key}")
    if spec.space in ("coil", "discrete"):
        return bool(words[0])
    if spec.count == 1:
        raw = int(words[0])
        if spec.missing and raw == 0x4E20:
            return None
        if spec.signed and raw >= 0x8000:
            raw -= 0x10000
    else:
        ordered = words if spec.word_order == "big" else words[::-1]
        raw = 0
        for word in ordered:
            raw = (raw << 16) | int(word)
        if spec.signed and raw >= (1 << (16 * spec.count - 1)):
            raw -= 1 << (16 * spec.count)
    value = raw * spec.scale
    return raw if spec.scale == 1 else round(value, 6)


def read_blocks(specs: list[RegisterSpec]) -> list[list[RegisterSpec]]:
    """Use short contiguous reads; never span gaps in the published map."""
    blocks: list[list[RegisterSpec]] = []
    for spec in sorted(specs, key=lambda item: item.address):
        if blocks:
            block = blocks[-1]
            end = block[-1].address + block[-1].count
            if spec.address == end and spec.address + spec.count - block[0].address <= 16:
                block.append(spec)
                continue
        blocks.append([spec])
    return blocks


class ThermiaDevice:
    """Own device data, but leave the connection lifecycle to Home Assistant."""

    def __init__(self, unit, *, undocumented: bool = True) -> None:
        self.unit = unit
        self.specs = {
            spec.key: spec for spec in REGISTERS if undocumented or spec.key not in OPTIONAL_WRITES
        }
        self.values: dict[str, Any] = {}
        self.unsupported: set[str] = set()
        self.errors: dict[str, str] = {}

    async def _read(self, space: str, address: int, count: int):
        method = {
            "input": self.unit.read_input_registers,
            "holding": self.unit.read_holding_registers,
            "coil": self.unit.read_coils,
            "discrete": self.unit.read_discrete_inputs,
        }[space]
        return await method(address, count)

    async def _read_block(self, specs: list[RegisterSpec], output: dict) -> None:
        first, last = specs[0], specs[-1]
        try:
            words = await self._read(
                first.space, first.address, last.address + last.count - first.address
            )
        except (IllegalDataAddressError, IllegalFunctionError):
            if len(specs) == 1:
                self.unsupported.add(first.key)
                output[first.key] = None
            else:
                midpoint = len(specs) // 2
                await self._read_block(specs[:midpoint], output)
                await self._read_block(specs[midpoint:], output)
            return
        for spec in specs:
            offset = spec.address - first.address
            output[spec.key] = decode(spec, words[offset : offset + spec.count])

    async def async_update(self) -> dict[str, Any]:
        output = {key: None for key in self.specs}
        groups: dict[str, list[RegisterSpec]] = defaultdict(list)
        for spec in self.specs.values():
            if spec.key not in self.unsupported:
                groups[spec.space].append(spec)
        self.errors = {}
        for space in ("input", "holding", "coil", "discrete"):
            for block in read_blocks(groups[space]):
                await self._read_block(block, output)
        self.values = output
        return output

    async def async_read_value(self, key: str) -> Any:
        spec = self.specs[key]
        result = decode(spec, await self._read(spec.space, spec.address, spec.count))
        self.values[key] = result
        return result

    async def async_write(self, key: str, value: float | int | bool) -> Any:
        spec = self.specs.get(key)
        if spec is None or not spec.writable or key in self.unsupported:
            raise ValueError(f"{key} is unsupported or read-only on this configuration")
        if not math.isfinite(float(value)):
            raise ValueError("A finite value is required")
        if spec.min_value is not None and value < spec.min_value:
            raise ValueError(f"{key} is below {spec.min_value}")
        if spec.max_value is not None and value > spec.max_value:
            raise ValueError(f"{key} is above {spec.max_value}")
        # A write can take effect even when its reply/readback is lost. Never
        # leave the old cached value looking confirmed in that situation.
        self.values[key] = None
        if spec.space == "coil":
            await self.unit.write_coil(spec.address, bool(value))
        elif spec.space == "holding" and spec.count == 1:
            raw = round(float(value) / spec.scale)
            if raw < -32768 or raw > 65535:
                raise ValueError("Value cannot be represented in a 16-bit register")
            await self.unit.write_register(spec.address, raw & 0xFFFF)
        else:
            raise ValueError("Only documented single registers and coils can be written")
        result = decode(spec, await self._read(spec.space, spec.address, spec.count))
        self.values[key] = result
        tolerance = abs(spec.scale) / 2 + 1e-6
        if result is None or abs(float(result) - float(value)) > tolerance:
            raise ValueError(f"The controller did not confirm {key}={value}; it returned {result}")
        return result
