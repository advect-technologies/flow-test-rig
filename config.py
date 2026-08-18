import tomllib
from dataclasses import InitVar, asdict, dataclass, field
from pathlib import Path

from loguru import logger
from rich import print_json

from periphs.alicat import AlicatPressureUnits, convert_to_pa


@dataclass
class SerialConfig:
    """Serial port settings (used for Alicat devices and scale)."""

    port: str = "/dev/ttyUSB0"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout: float = 1.0  # seconds
    query_timeout: float = 1.5


@dataclass
class ScaleConfig:
    """Scale (load cell) configuration."""

    serial: SerialConfig
    units: str = "g"

    def __post_init__(self):
        if isinstance(self.serial, dict):
            self.serial = SerialConfig(**self.serial)


@dataclass
class AlicatSharedSerial:
    """Optional shared serial connection for multi-drop / multiplexed Alicat devices.
    If defined, Alicat devices without their own 'serial' block will use this one.
    """

    serial: SerialConfig | None = None

    def __post_init__(self):
        if isinstance(self.serial, dict):
            self.serial = SerialConfig(**self.serial)


@dataclass
class AlicatConfig:
    """Base for all Alicat instruments."""

    full_scale_min: float
    full_scale_max: float
    unit_id: str = "A"  # 'A'–'Z' for multi-drop addressing
    pressure_unit: InitVar[str] = "PSI"
    serial: SerialConfig | None = None  # device-specific override
    pressure_unit_code: AlicatPressureUnits = field(init=False)

    def __post_init__(self, pressure_unit):
        if isinstance(self.serial, dict):
            self.serial = SerialConfig(**self.serial)

        self.pressure_unit_code = getattr(AlicatPressureUnits, pressure_unit.upper())
        self.full_scale_min = convert_to_pa(
            self.full_scale_min, self.pressure_unit_code
        )
        self.full_scale_max = convert_to_pa(
            self.full_scale_max, self.pressure_unit_code
        )


@dataclass
class FlowControlConfig(AlicatConfig):
    """Mass flow controller/meter."""

    flow_unit: str = "SLPM"


@dataclass
class DiffPressConfig(AlicatConfig):
    """Differential pressure sensor."""


@dataclass
class DAQConfig:
    watch_dir: Path = field(default_factory=lambda: Path(".daq_watch"))
    measurement: str = "flow_test_rig"
    metadata_measurement: str = "flow_test_rig_meta"
    sample_period_s: int = 10
    base_tags: dict[str, str | int | float | bool] = field(default_factory=dict)
    buffer_max_size: int = 100  # number of records before forcing a dump
    buffer_max_age_seconds: float = 30.0  # force dump after this many seconds


@dataclass
class TestRigConfig:
    """Complete test rig hardware configuration."""

    mock: bool
    station: str
    mass: ScaleConfig
    flow: FlowControlConfig
    high_dp: DiffPressConfig
    low_dp: DiffPressConfig
    alicat_shared: AlicatSharedSerial = field(default_factory=AlicatSharedSerial)
    daq: DAQConfig = field(default_factory=DAQConfig)

    def __post_init__(self):
        """Basic consistency checks."""
        # Ensure at least one Alicat has access to a serial port
        alicats = [self.flow, self.high_dp, self.low_dp]

        has_serial = any(
            [
                self.alicat_shared.serial is not None,
                all(a.serial is not None for a in alicats),
            ]
        )

        if not has_serial:
            raise ValueError(
                "No serial configuration found for any Alicat device. "
                "Define alicat_shared.serial or a per-device serial."
            )


def load_test_rig_config(
    path: str | Path = "config.toml",
) -> TestRigConfig:
    """
    Simple, robust TOML -> TestRigConfig loader with schema validation.
    """
    path = Path(path)

    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path.resolve()}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    try:
        mass = ScaleConfig(**raw.pop("mass", {}))
        flow = FlowControlConfig(**raw.pop("flow", {}))
        high_dp = DiffPressConfig(**raw.pop("high_dp", {}))
        low_dp = DiffPressConfig(**raw.pop("low_dp", {}))
        alicat_shared = AlicatSharedSerial(**raw.pop("alicat_shared", {}))
        daq = DAQConfig(**raw.pop("daq", {}))

        config = TestRigConfig(
            **raw,
            mass=mass,
            flow=flow,
            high_dp=high_dp,
            low_dp=low_dp,
            alicat_shared=alicat_shared,
            daq=daq,
        )

        logger.success("✅ Config loaded successfully (custom loader + validation)")
        return config

    except Exception as e:
        logger.error(f"❌ Config load failed: {path.name}")
        raise RuntimeError(f"Config error: {e}") from e


# Quick test
if __name__ == "__main__":
    try:
        cfg = load_test_rig_config()
        logger.info(f"Mock mode: {cfg.mock}")
        logger.info(f"Scale port: {cfg.mass.serial.port}")
        logger.info(f"Flow FS: {cfg.flow.full_scale_max}")
        print_json(data=asdict(cfg))
    except Exception as e:
        logger.exception(e)
