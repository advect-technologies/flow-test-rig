import datetime as dt
import json
import string
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from secrets import choice
from socket import gethostname
from typing import ClassVar

from daq_tools.models import DataPoint
from loguru import logger

from periphs import alicat, scale

# Optional: make this configurable later via TestRigConfig
DEFAULT_WATCH_DIR = Path("daq_watch")


def generate_id(length: int = 4) -> str:
    """Generates a random alphanumeric string."""
    alphabet = string.ascii_letters + string.digits
    return "".join(choice(alphabet) for _ in range(length))


@dataclass(kw_only=True)
class TestStage:
    duration_h: float | None = None
    duration_m: float | None = None
    TYPE: ClassVar[str] = "BASE"

    def __post_init__(self):
        self._min_duration_s = 60
        self._min_flow_step = 0.2  ## add units

        if (self.duration_h is not None) and (self.duration_m is not None):
            raise RuntimeError(
                "At most one of duration_h or duration_m must be defined"
            )
        elif self.duration_h is not None:
            # clamp at minimum duration
            self._duration_s = max(self.duration_h * 3600, self._min_duration_s)
        elif self.duration_m is not None:
            self._duration_s = max(self.duration_m * 60, self._min_duration_s)
        else:
            raise RuntimeError("One of duration_h or duration_m must be defined")


@dataclass(kw_only=True)
class TestStageStable(TestStage):
    target: float
    TYPE: ClassVar[str] = "STABLE"


@dataclass(kw_only=True)
class TestStageRamp(TestStage):
    target_start: float
    target_stop: float
    targets: list[float] = field(default_factory=list, init=False)
    times: list[float] = field(default_factory=list, init=False)
    time_step_s: float = field(init=False)
    TYPE: ClassVar[str] = "RAMP"

    def __post_init__(self):
        super().__post_init__()

        n_flow = abs(self.target_stop - self.target_start) / self._min_flow_step
        n_time = self._duration_s / self._min_duration_s

        self._n = int(min(n_flow, n_time))
        if self._n <= 1:
            self.time_step_s = self._duration_s
            flow_step = self.target_stop - self.target_start
        else:
            self.time_step_s = int(self._duration_s / self._n)
            flow_step = (self.target_stop - self.target_start) / (self._n - 1)

        self.targets = [
            round(self.target_start + i * flow_step, 1) for i in range(self._n + 1)
        ]
        self.targets[-1] = self.target_stop
        self.times = [round(i * self.time_step_s, 1) for i in range(self._n + 1)]


@dataclass
class TestProtocol:
    stages: list[TestStage]
    name: str | None = ""

    @classmethod
    def from_json(
        cls, path: Path = Path("protocols/example-protocol.json"), name: str = ""
    ) -> "TestProtocol":
        path = Path(path)
        if not path.exists():
            raise RuntimeError(f"{path.absolute()} does not exist")

        data = json.loads(path.read_text())
        if name == "":
            name = path.stem

        return cls.loader(data, name=name)

    @classmethod
    def loader(cls, stage_list: list[dict], name: str) -> "TestProtocol":
        stages: list[TestStage] = [
            cls._resolve_stage(s, i) for i, s in enumerate(stage_list)
        ]
        return cls(stages=stages, name=name)

    @classmethod
    def _resolve_stage(cls, stage: dict, index: int):

        STAGE_MAP = {c.TYPE: c for c in TestStage.__subclasses__()}

        stage_type = stage.pop("type")

        if (stage_type is None) or (not isinstance(stage_type, str)):
            raise RuntimeError(f"No type defined for stage {index}")

        c = STAGE_MAP.get(stage_type.upper())

        if c is None:
            raise RuntimeError(f"Invalid stage type for stage {index}")

        return c(**stage)


@dataclass(kw_only=True)
class TestMetadata:
    test_id: str = ""
    station: str = ""
    height: float | None = None
    diameter: float | None = None
    gas: str = "Air"
    notes: str = ""
    start_time: float = field(default_factory=time.time)
    protocol_file: Path | str | None = None

    def __post_init__(self):
        if self.test_id == "":
            self.test_id = f"{self.station}-{int(self.start_time)}-{generate_id(4)}"

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.protocol_file:
            d["protocol_file"] = str(self.protocol_file)
        return d

    def to_data_points(
        self, measurement: str = "flow_test_rig_meta", base_tags: dict | None = None
    ) -> list[DataPoint]:

        flat = self.to_dict()
        tags = {"test_id": self.test_id, "station": self.station}
        if isinstance(base_tags, dict):
            tags.update(base_tags)

        # Ensure no extra tags are present in flat
        for k in tags:
            flat.pop(k, None)

        point = DataPoint(
            measurement=measurement,  # ← single measurement name
            tags=tags,
            fields=flat,  # all mass + flow + low_dp + high_dp fields
        )
        return [point]


@dataclass(kw_only=True)
class UserMetadata:
    height: float = 0.0
    diameter: float = 0.0
    gas: str = "Air"
    notes: str = ""
    _persist_path: ClassVar[Path] = Path(".local", "user-meta.json")

    def __post_init__(self):
        Path(".local").mkdir(exist_ok=True)
        self._save_user_data()

    def to_dict(self) -> dict:
        return asdict(self)

    def _save_user_data(self):
        data = self.to_dict()
        self._persist_path.write_text(json.dumps(data))

    @classmethod
    def load_user_data(cls):
        if not cls._persist_path.exists():
            return cls()
        try:
            data = json.loads(cls._persist_path.read_text())
            return cls(**data)
        except Exception:
            logger.warning("Unable load user metadata")
            return cls()


@dataclass
class FlowTest:
    protocol: TestProtocol
    create_time: int | None = field(default_factory=lambda: int(time.time()))
    metadata: TestMetadata = field(default_factory=TestMetadata)


@dataclass(kw_only=True)
class TestRigDF:
    test_id: str | None = None
    station: str
    time: float = field(default_factory=lambda: time.time())
    mass: scale.ADEK30KL_DF | None = None
    flow: alicat.AlicatMassFlowDF | None = None
    low_dp: alicat.AlicatBaseDF | None = None
    high_dp: alicat.AlicatBaseDF | None = None

    def flatten(self):
        mass = (
            self.mass.flatten(prefix="mass", exclude=["time", "header", "unit"])
            if self.mass
            else {}
        )
        flow = (
            self.flow.flatten(prefix="flow", exclude=["time", "unit_id"])
            if self.flow
            else {}
        )
        low_dp = (
            self.low_dp.flatten(prefix="low_dp", exclude=["time", "unit_id"])
            if self.low_dp
            else {}
        )
        high_dp = (
            self.high_dp.flatten(prefix="high_dp", exclude=["time", "unit_id"])
            if self.high_dp
            else {}
        )
        timestamp = dt.datetime.fromtimestamp(self.time).isoformat(timespec="seconds")
        return {
            "station": self.station.title(),
            "time": timestamp,
            **mass,
            **flow,
            **low_dp,
            **high_dp,
        }

    def to_data_points(
        self, measurement: str = "flow-test_rig", base_tags: dict | None = None
    ) -> list[DataPoint]:
        """Convert the full rig snapshot into DataPoint(s) for daq-tools.

        For now we dump EVERYTHING into a single measurement/topic.
        Easy to split later if needed.
        """

        # Flatten everything into one big fields dict
        flat = self.flatten()

        # Optional: clean up the 'time' key if you don't want it duplicated
        flat.pop("time", None)
        flat.pop("station", None)

        if flat is None:
            return []

        if isinstance(base_tags, dict):
            tags = base_tags
            tags["id"] = gethostname()
        else:
            tags = {"id": gethostname()}

        tags["station"] = self.station

        if self.test_id is not None:
            tags["test_id"] = self.test_id

        point = DataPoint(
            time=self.time,
            measurement=measurement,  # ← single measurement name
            tags=tags,
            fields=flat,  # all mass + flow + low_dp + high_dp fields
        )

        return [point]


####################
# App Models
####################


class EventNames(StrEnum):
    CHANGE_SETPOINT = "change_setpoint"
    TARE_SCALE = "tare_scale"
    STOP_BUTTON = "stop_button"
    START_BUTTON = "start_button"
    NULL_EVENT = "null_event"
    STATE_CHANGE = "state_change"
    TEST_FINISH = "test_finish"
    TEST_CANCEL = "test_cancel"
    TEST_CRASH = "test_crash"
    PROTOCOL_CHANGE = "protocol_change"
    METADATA_UPDATE = "metadata_update"


class States(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    FAULT = "fault"


@dataclass(kw_only=True)
class Event:
    name: EventNames
    timestamp: int = field(default_factory=lambda: int(time.time()))
    retry: bool = False

    def model_dump_json(self):
        return json.dumps(asdict(self))

    @classmethod
    def model_load_json(cls, data):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Invalid json passed to event parser")
            return None

        if cl := [c for c in Event.__subclasses__() if c.name == data.get("name")]:
            return cl[0](**data)


@dataclass(kw_only=True)
class ProtocolChangedEvent(Event):
    name: EventNames = EventNames.PROTOCOL_CHANGE
    file: Path


@dataclass(kw_only=True)
class StopButtonEvent(Event):
    name: EventNames = EventNames.STOP_BUTTON
    retry: bool = True


@dataclass(kw_only=True)
class TestFinish(Event):
    name: EventNames = EventNames.TEST_FINISH
    retry: bool = True


@dataclass(kw_only=True)
class TestCancel(Event):
    name: EventNames = EventNames.TEST_CANCEL
    retry: bool = True


@dataclass(kw_only=True)
class TestCrash(Event):
    name: EventNames = EventNames.TEST_CRASH
    retry: bool = True


@dataclass(kw_only=True)
class StartButtonEvent(Event):
    name: EventNames = EventNames.START_BUTTON
    retry: bool = True


@dataclass(kw_only=True)
class SetpointEvent(Event):
    name: EventNames = EventNames.CHANGE_SETPOINT
    value: float


@dataclass(kw_only=True)
class TareScaleEvent(Event):
    name: EventNames = EventNames.TARE_SCALE


@dataclass(kw_only=True)
class NullEvent(Event):
    name: EventNames = EventNames.NULL_EVENT


@dataclass(kw_only=True)
class StateChangeEvent(Event):
    new_state: States
    name: EventNames = EventNames.STATE_CHANGE


@dataclass(kw_only=True)
class MetadataUpdateEvent(Event):
    name: EventNames = EventNames.METADATA_UPDATE
    meta: UserMetadata
