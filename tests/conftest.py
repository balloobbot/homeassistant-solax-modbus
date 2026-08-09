import pytest
from modbus_connection import ModbusError


class MockHub:
    """A hub stand-in speaking the read contract the plugins now use."""

    def __init__(self, name: str = "MockHub", modbus_addr: int = 1) -> None:
        self.name = name
        self._modbus_addr = modbus_addr
        self.seriesnumber: str | None = None
        # Map (unit, address, count) -> registers, or an exception to raise.
        self._read_side_effects: dict[tuple[int, int, int], list[int] | ModbusError] = {}

    def configure_read(self, unit: int, address: int, count: int, response: list[int] | ModbusError) -> None:
        """Configure a specific response for a read operation."""
        self._read_side_effects[(unit, address, count)] = response

    async def async_read_input_registers(self, unit: int, address: int, count: int) -> list[int]:
        """Simulate reading input registers."""
        response = self._read_side_effects.get((unit, address, count))
        if isinstance(response, ModbusError):
            raise response
        if response is not None:
            return response

        # Default empty response if not configured
        return [0] * count

    async def async_read_holding_registers(self, unit: int, address: int, count: int) -> list[int]:
        """Simulate reading holding registers."""
        # For now, treat same as input registers for mocking purposes
        return await self.async_read_input_registers(unit, address, count)


@pytest.fixture
def mock_hub() -> MockHub:
    return MockHub()
