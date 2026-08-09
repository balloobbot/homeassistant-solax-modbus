"""Tests for the SolaX register data types built on modbus-connection fields."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.solax_modbus import SolaXModbusHub
from custom_components.solax_modbus.const import (
    REGISTER_F32,
    REGISTER_S16,
    REGISTER_S32,
    REGISTER_STR,
    REGISTER_U8H,
    REGISTER_U8L,
    REGISTER_U16,
    REGISTER_U32,
    REGISTER_ULSB16MSB16,
    REGISTER_WORDS,
)
from custom_components.solax_modbus.fields import field_for


@pytest.mark.parametrize(
    ("register_data_type", "words", "expected"),
    [
        (REGISTER_U16, [0xFFFE], 65534),
        (REGISTER_S16, [0xFFFE], -2),
        (REGISTER_U32, [0x1234, 0x5678], 0x12345678),
        (REGISTER_S32, [0xFFFF, 0xFFFE], -2),
        (REGISTER_F32, [0x4048, 0xF5C3], pytest.approx(3.14, abs=1e-6)),
        (REGISTER_U8L, [0xAB12], 0x12),
        (REGISTER_U8H, [0xAB12], 0xAB),
        (REGISTER_WORDS, [1, 2, 3], [1, 2, 3]),
        (REGISTER_STR, [0x4D31, 0x3233], "M123"),
        # Declared "LSB word first"; identical to a plain 32-bit read either way.
        (REGISTER_ULSB16MSB16, [0x1234, 0x5678], 0x12345678),
    ],
)
def test_big_endian_types_decode_as_they_did_under_pymodbus(register_data_type: str, words: list[int], expected: Any) -> None:
    field = field_for(register_data_type, word_count=len(words))
    assert field is not None
    assert field.decode(words) == expected


@pytest.mark.parametrize(
    ("register_data_type", "words", "expected"),
    [
        (REGISTER_U32, [0x1234, 0x5678], 0x56781234),
        (REGISTER_S32, [0xFFFE, 0xFFFF], -2),
        (REGISTER_ULSB16MSB16, [0x1234, 0x5678], 0x56781234),
        # pymodbus applied word order to strings too, so plugins declaring
        # order32="little" read their strings word-reversed.
        (REGISTER_STR, [0x4D31, 0x3233], "23M1"),
    ],
)
def test_little_word_order_is_carried_through(register_data_type: str, words: list[int], expected: Any) -> None:
    field = field_for(register_data_type, word_count=len(words), word_order="little")
    assert field is not None
    assert field.decode(words) == expected


def test_unknown_register_data_type_has_no_field() -> None:
    assert field_for("_nonsense") is None
    assert field_for(None) is None


def test_fields_are_shared_per_type_width_and_word_order() -> None:
    assert field_for(REGISTER_U32) is field_for(REGISTER_U32)
    assert field_for(REGISTER_U32) is not field_for(REGISTER_U32, word_order="little")
    assert field_for(REGISTER_STR, word_count=4) is not field_for(REGISTER_STR, word_count=8)


@pytest.mark.parametrize(
    ("register_data_type", "value", "expected"),
    [
        (REGISTER_U16, 65534, [65534]),
        (REGISTER_S16, -2, [65534]),
        (REGISTER_U32, 0x12345678, [0x1234, 0x5678]),
        (REGISTER_S32, -2, [0xFFFF, 0xFFFE]),
    ],
)
def test_write_encoding_round_trips_the_decoded_value(register_data_type: str, value: int, expected: list[int]) -> None:
    field = field_for(register_data_type, word_count=len(expected))
    assert field is not None
    assert field.encode(value) == expected
    assert field.decode(expected) == value


def make_hub(order32: str = "big") -> Any:
    """A hub with only the state treat_address touches."""
    hub = cast(Any, object.__new__(SolaXModbusHub))
    hub._name = "test"
    hub.plugin = SimpleNamespace(order32=order32, isAwake=lambda data: True)
    hub.cyclecount = 100
    hub.tmpdata_expiry = {}
    hub.localsLoaded = True
    hub.inverterPowerKw = 100
    hub._validate_register_func = None
    return hub


def description(**kwargs: Any) -> Any:
    base = {
        "key": "test",
        "register": 0x10,
        "register_data_type": REGISTER_U16,
        "read_scale": 1,
        "scale": 1,
        "rounding": 1,
        "wordcount": None,
        "sleepmode": None,
        "native_unit_of_measurement": None,
        "min_value": None,
        "max_value": None,
        "read_scale_exceptions": None,
    }
    return SimpleNamespace(**{**base, **kwargs})


def test_byte_halves_share_one_register_without_advancing_twice() -> None:
    """The two halves of a packed register both decode from the same word."""
    hub = make_hub()
    data: dict[str, Any] = {}
    regs = [0xAB12, 0x0001]

    low = description(key="low", register_data_type=REGISTER_U8L)
    high = description(key="high", register_data_type=REGISTER_U8H)

    # advance=False is how the block reader feeds the second and later entities
    # sharing an address: it passes the word the first one already read.
    assert hub.treat_address(data, regs, 0, low, initval=regs[0], advance=False) == 0
    assert hub.treat_address(data, regs, 0, high, initval=regs[0], advance=False) == 0
    assert data == {"low": 0x12, "high": 0xAB}

    # Read on its own, a byte half still consumes its register.
    assert hub.treat_address(data, regs, 0, low, advance=True) == 1


def test_per_sensor_word_order_overrides_the_plugin_default() -> None:
    hub = make_hub(order32="big")
    data: dict[str, Any] = {}
    descr = description(key="energy", register_data_type=REGISTER_U32, order32="little")

    hub.treat_address(data, [0x1234, 0x5678], 0, descr)

    assert data["energy"] == 0x56781234
