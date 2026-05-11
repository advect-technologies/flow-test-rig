import json
import datetime as dt
import time
import string
from secrets import choice
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal, Union, Any, ClassVar, List, Dict
from periphs import alicat, scale
from enum import StrEnum
from loguru import logger
from pathlib import Path
from daq_tools.models import DataPoint
from socket import gethostname

# Optional: make this configurable later via TestRigConfig
DEFAULT_WATCH_DIR = Path("daq_watch")

def generate_id(length: int = 4) -> str:
    """Generates a random alphanumeric string."""
    alphabet = string.ascii_letters + string.digits
    return ''.join(choice(alphabet) for _ in range(length))

@dataclass(kw_only=True)
class TestStage:
    duration_h: float
    TYPE: ClassVar[str] = 'BASE'

    def __post_init__(self):
        self._min_duration_s = 60
        self._min_flow_step = 0.2   ## add units

        # clamp at minimum duration
        self._duration_s = max(self.duration_h * 3600,self._min_duration_s)

@dataclass(kw_only=True)
class TestStageStable(TestStage):
    target: float
    TYPE: ClassVar[str] = 'STABLE'

@dataclass(kw_only=True)
class TestStageRamp(TestStage):
    target_start: float
    target_stop: float
    targets: List[float] = field(default=None,init=False)
    times: List[float] = field(default=None,init=False)
    time_step_s: float = field(default=None,init=False)
    TYPE: ClassVar[str] = 'RAMP'

    def __post_init__(self):
        super().__post_init__()
    
        n_flow = abs(self.target_stop - self.target_start) / self._min_flow_step 
        n_time = self._duration_s / self._min_duration_s
        
        self._n = int(min(n_flow,n_time))
        if self._n <= 1:
            self.time_step_s = self._duration_s
            flow_step = self.target_stop - self.target_start 
        else:
            self.time_step_s = int(self._duration_s / self._n)
            flow_step = (self.target_stop - self.target_start) / (self._n-1)

        self.targets = [round(self.target_start + i*flow_step,1) for i in range(self._n+1)]
        self.targets[-1] = self.target_stop
        self.times = [round(i*self.time_step_s,1) for i in range(self._n+1)]
        
@dataclass
class TestProtocol:
    stages: list[TestStage]
    name: Optional[str] = ""

    @classmethod
    def from_json(cls,
                  path:Path = Path('protocols/default-protocol.json'),
                  name:str = '') -> TestProtocol:
        path = Path(path)
        if not path.exists(): RuntimeError(f'{path.absolute()} does not exist')
        
        data = json.loads(path.read_text())
        if name == '': name = path.stem

        return cls.loader(data,name=name)

    @classmethod
    def loader(cls,stage_list: List[Dict], name:str) -> TestProtocol:
        stages: List[TestStage] = [cls._resolve_stage(s,i) for i,s in enumerate(stage_list)]
        return cls(stages=stages, name=name)

    @classmethod
    def _resolve_stage(cls,stage: Dict, index: int):
        
        STAGE_MAP = {c.TYPE:c for c in TestStage.__subclasses__()}

        stage_type = stage.pop('type')

        if (stage_type is None) or (not isinstance(stage_type,str)):
            raise RuntimeError(f'No type defined for stage {index}')
        
        c = STAGE_MAP.get(stage_type.upper())
        
        if c is None:
            raise RuntimeError(f'Invalid stage type for stage {index}')
        
        return c(**stage)

@dataclass
class FlowTestReport:
    current_stage: int
    total_stages: int
    run_time: float
    remaining_time: float

@dataclass
class FlowTest:
    protocol: TestProtocol
    station: str
    test_id: Optional[str] = "" 
    create_time: Optional[int] = field(default_factory=time.time)
    
    def __post_init__(self):
        if self.test_id == '':
            self.test_id =  f'{self.station}-{int(self.create_time)}-{generate_id(4)}'
        
@dataclass(kw_only=True)
class TestRigDF:
    time: float = field(
        default_factory=lambda:time.time()
        )    
    mass: Optional[scale.ADEK30KL_DF] = None
    flow: Optional[alicat.AlicatMassFlowDF] = None
    low_dp: Optional[alicat.AlicatBaseDF] = None
    high_dp: Optional[alicat.AlicatBaseDF] = None
    
    def flatten(self):
        mass = self.mass.flatten(prefix='mass',exclude=['time','header','unit']) if self.mass else {}
        flow = self.flow.flatten(prefix='flow',exclude=['time','unit_id']) if self.flow else {}
        low_dp = self.low_dp.flatten(prefix='low_dp',exclude=['time','unit_id']) if self.low_dp else {}
        high_dp = self.high_dp.flatten(prefix='high_dp',exclude=['time','unit_id']) if self.high_dp else {}
        timestamp = dt.datetime.fromtimestamp(self.time).isoformat(timespec='seconds')
        return {'time':timestamp,**mass,**flow,**low_dp,**high_dp}

    def to_data_points(self,
                       measurement:str = 'flow-test_rig',
                       base_tags: dict | None = {}
                       ) -> list[DataPoint]:
            """Convert the full rig snapshot into DataPoint(s) for daq-tools.
            
            For now we dump EVERYTHING into a single measurement/topic.
            Easy to split later if needed.
            """

            # Flatten everything into one big fields dict (this is what you asked for)
            flat = self.flatten()                     # reuses your existing method

            # Optional: clean up the 'time' key if you don't want it duplicated
            flat.pop('time', None)

            if flat is None:
                return []
            
            if isinstance(base_tags,dict):
                tags = base_tags
                tags["id"] = gethostname()
            else:
                tags={"id": gethostname()}

            point = DataPoint(
                time=self.time,
                measurement=measurement,           # ← single measurement name
                tags = tags,
                fields=flat                                # all mass + flow + low_dp + high_dp fields
            )

            return [point]

@dataclass
class SerialConfig:
    """Serial port settings (used for Alicat devices and scale)."""
    port: str = "/dev/ttyUSB0"
    baudrate: int = 19200
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout: float = 1.0           # seconds
    query_timeout: float = 1.5

@dataclass
class ScaleConfig:
    """Scale (load cell) configuration."""
    serial: SerialConfig
    units: str = "g"

@dataclass
class AlicatSharedSerial:
    """Optional shared serial connection for multi-drop / multiplexed Alicat devices.
    If defined, Alicat devices without their own 'serial' block will use this one.
    """
    serial: Optional[SerialConfig] = None

@dataclass
class AlicatConfig:
    """Base for all Alicat instruments."""
    full_scale_min: float
    full_scale_max: float
    unit_id: str = "A"                     # 'A'–'Z' for multi-drop addressing
    pressure_unit: str = "PSI"
    serial: Optional[SerialConfig] = None  # device-specific override

    def __post_init__(self):
        if not isinstance(self.pressure_unit,(int,alicat.AlicatPressureUnits)):
            self.pressure_unit = getattr(alicat.AlicatPressureUnits,self.pressure_unit.upper())
        self.full_scale_min = alicat.convert_to_pa(self.full_scale_min,
                                                   self.pressure_unit)
        self.full_scale_max = alicat.convert_to_pa(self.full_scale_max,
                                                   self.pressure_unit)

@dataclass
class FlowControlConfig(AlicatConfig):
    """Mass flow controller/meter."""
    flow_unit: str = "SLPM"

@dataclass
class DiffPressConfig(AlicatConfig):
    """Differential pressure sensor."""
    pass  # extend later if needed (e.g. damping, averaging)

@dataclass
class DaqBufferConfig:
    """Buffer settings for the JSONL writer."""
    max_size: int = 100                    # number of records before forcing a dump
    max_age_seconds: float = 30.0          # force dump after this many seconds

@dataclass
class DaqConfig:
    watch_dir: Path = field(default_factory=lambda: Path(".daq_watch"))
    measurement: str = "flow_test_rig"
    sample_period_s: int = 10
    base_tags: dict[str,Any] = field(default_factory=dict)
    buffer: DaqBufferConfig = field(default_factory=DaqBufferConfig)

@dataclass
class TestRigConfig:
    """Complete test rig hardware configuration."""
    mock: bool
    mass: ScaleConfig
    flow: FlowControlConfig
    high_dp: DiffPressConfig
    low_dp: DiffPressConfig
    alicat_shared: AlicatSharedSerial = field(default_factory=AlicatSharedSerial)
    daq: DaqConfig = field(default_factory=DaqConfig)

    def __post_init__(self):
        """Basic consistency checks."""
        # Ensure at least one Alicat has access to a serial port
        alicats = [self.flow, self.high_dp, self.low_dp]
        
        has_serial = any([self.alicat_shared.serial is not None, 
                          all([a.serial is not None for a in alicats])])

        if not has_serial:
            raise ValueError(
                "No serial configuration found for any Alicat device. "
                "Define alicat_shared.serial or a per-device serial."
            )

class EventNames(StrEnum):
    CHANGE_SETPOINT = 'change_setpoint'
    TARE_SCALE = 'tare_scale'
    STOP_BUTTON = 'stop_button'
    START_BUTTON = 'start_button'
    NULL_EVENT = 'null_event'
    STATE_CHANGE = 'state_change'
    TEST_FINISH = 'test_finish'
    TEST_CANCEL = 'test_cancel'
    TEST_CRASH = 'test_crash'

class States(StrEnum):
    IDLE = 'idle'
    ACTIVE = 'active'
    FAULT = 'fault'

@dataclass(kw_only=True)
class Event:
    name: EventNames
    timestamp:int = field(default_factory=lambda:int(time.time()))
    retry:bool = False

    def model_dump_json(self):
        return json.dumps(asdict(self))

    @classmethod
    def model_load_json(cls,data):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            logger.warning('Invalid json passed to event parser')
            return None
        
        if cl := [c for c in Event.__subclasses__() if c.name == data.get('name')]:
            return cl[0](**data)
        
@dataclass(kw_only=True)
class StopButtonEvent(Event):
    name: EventNames = EventNames.STOP_BUTTON
    retry:bool = True

@dataclass(kw_only=True)
class TestFinish(Event):
    name: EventNames = EventNames.TEST_FINISH
    retry:bool = True

@dataclass(kw_only=True)
class TestCancel(Event):
    name: EventNames = EventNames.TEST_CANCEL
    retry:bool = True

@dataclass(kw_only=True)
class TestCrash(Event):
    name: EventNames = EventNames.TEST_CRASH
    retry:bool = True

@dataclass(kw_only=True)
class StartButtonEvent(Event):
    name: EventNames = EventNames.START_BUTTON
    retry:bool = True

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