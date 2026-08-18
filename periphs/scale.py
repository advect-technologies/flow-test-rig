from dataclasses import dataclass
from enum import StrEnum
from random import randint
from re import compile

from loguru import logger

from . import utils


class ADEK30KL_DATA_HEADERS(StrEnum):
    STABLE_WEIGHT = "st"
    STABLE_COUNT = "qt"
    UNSTABLE_WEIGHT = "us"
    OVER_LIMIT = "ol"


class ADEK30KL_DATA_UNITS(StrEnum):
    GRAM = "g"
    KILOGRAM = "kg"
    PIECES = "pc"
    PERCENTAGE = "%"
    DECIMAL_OUNCE = "oz"
    DECIMAL_POUND = "lb"
    TROY_OUNCE = "ozt"
    TAEL = "tl"


class ADEK30KL_COMMANDS(StrEnum):
    DATA_REQUEST = "Q"
    TARE = "Z"
    UNITS = "U"


@dataclass(kw_only=True)
class ADEK30KL_DF(utils.PeriphDF):
    header: ADEK30KL_DATA_HEADERS | None
    value: float | None
    unit: ADEK30KL_DATA_UNITS | None

    def __post_init__(self):

        if not self.header in ADEK30KL_DATA_HEADERS:
            raise TypeError(f"{self.header} is invalid header")

        if not self.unit in ADEK30KL_DATA_UNITS:
            raise TypeError(f"{self.unit} is invalid unit")

    @classmethod
    def parse_line(
        cls,
        line: str,
    ) -> ADEK30KL_DF:
        if not line:
            raise TypeError(f"line must be string not {type(line)}")

        if m := ADEK30KL.data_packet_pattern.search(line.lower()):
            if len(m.group()) != 15:
                raise ValueError(f"{m.group()} is of unexpected length")

            header, rhs = m.group().split(",")
            data = float(rhs[:9])
            unit = rhs[9:].strip()

            return cls(header=header, value=data, unit=unit)
        else:
            raise ValueError(f'Failed to parse packet: "{line}"')


class ADEK30KL:
    """
    Docstring for AD_EK30KL
    Data messages are received in the format: AA,+DDDDDDDDUUU\r\f
    """

    data_packet_pattern = compile(r"[a-zA-Z]{2},[+-][0-9\.]{8}[a-zA-Z\s%]{3}")

    def __init__(
        self, serial_handler: utils.SimpleSerialDevice | utils.MockSerialDevice
    ):
        self.serial = serial_handler

    async def fetch_data(self) -> ADEK30KL_DF:
        line = await self.serial.query(ADEK30KL_COMMANDS.DATA_REQUEST + "\r\n")

        if line is None:
            logger.warning("No data received")
            return

        try:
            data = ADEK30KL_DF.parse_line(line)
            return data
        except (ValueError, TypeError) as e:
            logger.error(f'Unable to parse line: "{e}"')
        except Exception:
            pass

    async def tare(self) -> bool:
        line = await self.serial.query(ADEK30KL_COMMANDS.TARE + "\r\n")
        logger.info("Zeroing Scale")
        if line is None:
            logger.warning("No data received")
            return
        try:
            if line.strip() == ADEK30KL_COMMANDS.TARE:
                return True
        except (ValueError, TypeError) as e:
            logger.error(f'Unable to parse line: "{e}"')
        except Exception:
            pass

    @classmethod
    def mock_command_map(cls, command: str) -> str | None:
        command = command.strip()
        match command:
            case ADEK30KL_COMMANDS.DATA_REQUEST:
                val = str(float(randint(10, 10000)) + randint(0, 10) / 10).zfill(8)
                return f"ST,+{val}  g"

            case ADEK30KL_COMMANDS.TARE:
                return "Z"

            case ADEK30KL_COMMANDS.UNITS:
                return "U"

            case _:
                pass
