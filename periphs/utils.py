import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import aioserial
from loguru import logger
from serial.serialutil import EIGHTBITS, PARITY_NONE, STOPBITS_ONE, SerialException


@dataclass(kw_only=True)
class PeriphDF:
    time: float = field(default_factory=dt.datetime.now(dt.UTC).timestamp)

    @classmethod
    def parse_line(cls, raw_line: str) -> "PeriphDF":
        return cls()

    def flatten(self, prefix: str | None = None, exclude: list[str] | None = None):

        if exclude is None:
            exclude = []
        return {f"{prefix}_{k}": v for k, v in asdict(self).items() if k not in exclude}


class SimpleSerialDevice:
    """
    Minimal async serial device handler - no background task, lazy open, basic retries.

    Opens the port only when first used or after failure.
    Re-open happens automatically on next call if the previous one failed.
    Designed for simplicity; higher-level code should handle retry timing if needed.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        bytesize: int = EIGHTBITS,
        parity: str = PARITY_NONE,
        stopbits: float = STOPBITS_ONE,
        timeout: float = 1,
        query_timeout: float = 1.5,
        max_retries: int = 2,
        retry_backoff: float = 0.3,
        encoding: str = "ascii",
        name: str = "SerialDevice",
    ):
        self.port = port
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self.query_timeout = query_timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.encoding = encoding
        self.name = name

        self._ser: aioserial.AioSerial | None = None
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Explicitly close the port if open."""
        if self._ser is not None:
            try:
                await self._ser.close()
            except Exception:
                logger.debug(f"{self.name}: Failed to close port")
            self._ser = None

    async def _ensure_open(self) -> bool:
        if self._ser is None or not self._ser.is_open:
            try:
                self._ser = aioserial.AioSerial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=self.bytesize,
                    parity=self.parity,
                    stopbits=self.stopbits,
                    timeout=0.8,  # short read timeout for individual reads
                )
                logger.info(f"{self.name}: Opened {self.port}")
                return True
            except Exception as e:
                logger.warning(f"{self.name}: Failed to open port: {e}")
                return False
        return True

    async def query(
        self,
        command: str,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> str | None:
        """
        Send command and wait for one line response.
        Retries on timeout or serial errors.
        Returns response or None if all attempts fail.
        """
        effective_retries = retries if retries is not None else self.max_retries
        effective_timeout = timeout if timeout is not None else self.query_timeout

        if not await self._ensure_open():
            return None

        payload = command.encode(self.encoding)

        for attempt in range(effective_retries + 1):
            async with self._lock:
                try:
                    await self._ser.write_async(payload)

                    async with asyncio.timeout(effective_timeout):
                        raw = await self._ser.readline_async()
                        if raw:
                            return raw.decode(self.encoding, errors="replace").strip()

                except TimeoutError:
                    logger.debug(
                        f"{self.name}: Timeout on attempt {attempt + 1}/{effective_retries + 1}: {command}"
                    )
                except (SerialException, OSError, ConnectionError) as e:
                    logger.warning(
                        f"{self.name}: Serial error on attempt {attempt + 1}: {e}"
                    )
                    self._ser = None  # force reopen next attempt
                except Exception:
                    logger.exception(f"{self.name}: Unexpected error in query")
                    return None

            if attempt < effective_retries:
                await asyncio.sleep(self.retry_backoff)

        logger.warning(
            f"{self.name}: All {effective_retries + 1} attempts failed for: {command}"
        )
        return None

    async def write_only(self, command: str) -> bool:
        """Send command without waiting for response."""
        if not await self._ensure_open():
            return False

        payload = command.encode(self.encoding)

        async with self._lock:
            try:
                await self._ser.write_async(payload)
                return True
            except Exception as e:
                logger.error(f"{self.name}: Write failed: {e}")
                self._ser = None  # force reopen next time
                return False


class MockSerialDevice:
    """
    Mock serial device for testing.

    Instead of talking to real hardware, it uses a user-provided mapping
    function (or dict) to simulate device responses.

    Example usage:
        def my_response_mapper(cmd: str) -> str:
            if cmd == "A\r":
                return "A   +012.34 +025.67 +0100.0 +0095.2 +0100.0"
            if cmd.startswith("A S"):
                return "A"  # simple ack
            return "ERR"

        mock = MockSerialDevice(response_mapper=my_response_mapper)
    """

    def __init__(
        self,
        response_mapper: Callable[[str], str | None]
        | dict[str, str | None]
        | None = None,
        name: str = "MockSerial",
        always_connected: bool = True,
        delay: float = 0.0,  # simulated network/device delay
    ):
        self.name = name
        self._delay = delay
        self._always_connected = always_connected

        if callable(response_mapper):
            self._response_mapper = response_mapper
        elif isinstance(response_mapper, dict):
            self._response_mapper = lambda cmd: response_mapper.get(cmd.strip(), None)
        else:
            # Default: echo command or return a generic response
            self._response_mapper = lambda cmd: f"MOCK: {cmd.strip()}"

        # For write_only tracking (optional debugging)
        self._last_written: str | None = None

    async def close(self) -> None:
        """Mock close - does nothing."""

    async def query(
        self,
        command: str,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> str | None:
        """
        Simulate sending a command and receiving a response.
        Ignores timeout/retries (always "succeeds" instantly or with delay).
        """
        logger.debug(f'MOCK {self.name} - sending command "{command.strip()}"')

        if self._delay > 0:
            await asyncio.sleep(self._delay)

        if not self._always_connected:
            # Simulate occasional disconnects for testing robustness
            import random

            if random.random() < 0.1:  # 10% chance
                logger.debug("Simulating disconnect, no data returned")
                return None

        response = self._response_mapper(command.rstrip())
        logger.debug(f"Responding with mock line: {response}")
        return response

    async def write_only(self, command: str) -> bool:
        """Simulate a write - always succeeds, remembers last command."""
        if self._delay > 0:
            await asyncio.sleep(self._delay)

        self._last_written = command
        return True

    # Optional helpers for test assertions
    def get_last_written(self) -> str | None:
        return self._last_written

    def reset_last_written(self) -> None:
        self._last_written = None


def safe_float(value: Any) -> float:
    """
    Safely convert a value to float.

    Args:
        value: Value to convert (string, float, int, etc.)

    Returns:
        float: Converted value

    Raises:
        ValueError: If value cannot be converted to float
        TypeError: If value is of unsupported type
    """
    # If already a float, return unaltered
    if isinstance(value, float):
        return value

    # Handle integers and other numeric types
    if isinstance(value, (int, bool)):
        return float(value)

    # Handle strings
    if isinstance(value, str):
        # Strip whitespace
        value = value.strip()

        # Handle empty string
        if not value:
            raise ValueError("Cannot convert empty string to float")

        # Handle scientific notation and special cases
        try:
            return float(value)
        except ValueError as e:
            raise ValueError(f"Cannot convert '{value}' to float: {e!s}") from e

    # Unsupported type
    raise TypeError(f"Unsupported type for float conversion: {type(value).__name__}")
