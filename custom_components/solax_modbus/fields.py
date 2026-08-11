"""SolaX register data types as modbus-connection register fields.

modbus-connection ships the common Modbus scalar types and expects a device
library to define the exotic ones as ``RegisterField`` subclasses. Two are
SolaX's: a string whose words follow the plugin's 32-bit word order, and a plain
list of raw words. The two byte halves that share one 16-bit register are the
library's own ``bits()`` field.

The fields are used unbound (address 0): the hub owns block planning and passes
the already-read words in, so only ``decode``/``encode`` matter here.
"""

from __future__ import annotations

from typing import Any

from modbus_connection import WordOrder
from modbus_connection.decode import decode_string
from modbus_connection.encode import encode_string
from modbus_connection.model import FloatField, NumberField, RegisterField, bits

from .const import (
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


class WordOrderedStringField(RegisterField[str]):
    """A null-padded ASCII string whose registers follow a word order.

    modbus-connection's ``StringField`` has no ``word_order``, unlike every
    numeric field, but plugins declaring ``order32="little"`` do store their
    strings word-reversed - which is what pymodbus's STRING conversion produced
    for them before this migration.
    """

    def __init__(self, address: int, *, word_order: WordOrder = "big", **kwargs: Any) -> None:
        super().__init__(address, **kwargs)
        self.word_order = word_order

    def _ordered(self, words: list[int]) -> list[int]:
        return words if self.word_order == "big" else list(reversed(words))

    def decode(self, words: list[int], scale_exponent: int | None = None) -> str:
        """Decode the registers as ASCII, two characters per word."""
        return decode_string(self._ordered(words))

    def encode(self, value: Any, scale_exponent: int | None = None) -> list[int]:
        """Encode a string into ``count`` null-padded registers."""
        return self._ordered(encode_string(value, length=self.count))


class WordsField(RegisterField[list[int]]):
    """A field that stays a list of raw register words.

    Plugins hand these to a ``value_function`` that picks the words apart
    itself - packed clock registers, and firmware versions split over several
    registers, are the usual shapes.
    """

    def decode(self, words: list[int], scale_exponent: int | None = None) -> list[int]:
        """Return the words themselves, masked to 16 bits."""
        return [word & 0xFFFF for word in words]


def _build(register_data_type: str, word_count: int, word_order: WordOrder) -> RegisterField[Any] | None:
    """Build the field for one SolaX register data type."""
    if register_data_type == REGISTER_U16:
        return NumberField(0, count=1, signed=False)
    if register_data_type == REGISTER_S16:
        return NumberField(0, count=1, signed=True)
    if register_data_type == REGISTER_U32:
        return NumberField(0, count=2, signed=False, word_order=word_order)
    if register_data_type == REGISTER_S32:
        return NumberField(0, count=2, signed=True, word_order=word_order)
    if register_data_type == REGISTER_F32:
        return FloatField(0, count=2, word_order=word_order)
    if register_data_type == REGISTER_ULSB16MSB16:
        # Declared as "LSB word first, MSB word second", which is what an
        # unsigned 32-bit read already produces for either word order - the
        # plugins' comment that this duplicates REGISTER_U32 is correct.
        return NumberField(0, count=2, signed=False, word_order=word_order)
    if register_data_type == REGISTER_STR:
        return WordOrderedStringField(0, count=word_count, word_order=word_order)
    if register_data_type == REGISTER_WORDS:
        return WordsField(0, count=word_count)
    if register_data_type == REGISTER_U8L:
        return bits(0, 0, 8)
    if register_data_type == REGISTER_U8H:
        return bits(0, 8, 8)
    return None


_CACHE: dict[tuple[str, int, str], RegisterField[Any] | None] = {}


def field_for(
    register_data_type: str | None,
    *,
    word_count: int | None = None,
    word_order: WordOrder = "big",
) -> RegisterField[Any] | None:
    """Return the field for a register data type, or None if it has none.

    Fields are stateless once built, and a poll decodes thousands of registers,
    so they are built once per (type, width, word order) and shared.
    """
    if register_data_type is None:
        return None
    key = (register_data_type, word_count or 0, word_order)
    if key not in _CACHE:
        _CACHE[key] = _build(register_data_type, word_count or 0, word_order)
    return _CACHE[key]
