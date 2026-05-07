import json
from . import utils
from dataclasses import dataclass, fields, asdict
from enum import StrEnum, IntEnum
from loguru import logger
from typing import get_args, Optional
from re import compile
from random import uniform, gauss
from pathlib import Path

class AlicatPressureUnits(IntEnum):
    DEFAULT = 0
    PA = 2
    KPA = 4
    MPA = 5
    MBAR = 6
    BAR = 7
    PSI = 10
    INH2O = 20

class AlicatFlowUnits(IntEnum):
    SLPM = 7

class AlicatCommands(StrEnum):
    POLL_DATA = ''
    REQUEST_DATA = 'DV'
    ACTIVE_GAS = 'GS'
    AVAILABLE_GAS = '??G*'
    QUERY_SETPOINT = 'LS'
    TARE_FLOW = 'V'
    TARE_PRESSURE = 'P'

def convert_to_pa(val:float,input_units:AlicatPressureUnits):
    if val is None: return
    match(input_units):
        case AlicatPressureUnits.KPA:
            return int(val * 1000)
        case AlicatPressureUnits.MPA:
            return int(val * 1e6)
        case AlicatPressureUnits.PSI:
            return int(val * 6894.757)
        case AlicatPressureUnits.INH2O:
            return int(val * 248.84)
        case _:
            return val

@dataclass(kw_only=True)
class AlicatBaseDF(utils.PeriphDF):
    unit_id: str
    pressure: Optional[float] = None

    def __post_init__(self):
        names = [f.name for f in fields(self.__class__)
                  if f.type is float or float in get_args(f.type)]      
        for name in names:
            try:
                val = float(getattr(self,name))
            except ValueError:
                val = None
            setattr(self,name,val)
        
    @classmethod
    def parse_line(cls, raw_line:str, pressure_unit:AlicatPressureUnits) -> AlicatBaseDF:
        parts = raw_line.strip().split()
        if len(parts) < 2:
            raise ValueError(f"Invalid Alicat response (too short): {raw_line!r}")

        unit_id = parts[0]
        pressure = parts[1] if len(parts) > 1 else None

        data = cls(
            unit_id = unit_id,
            pressure = pressure
        )

        if data.pressure is not None:
            data.pressure = convert_to_pa(data.pressure,pressure_unit)        
        return data

    @classmethod
    def generate(cls,unit_id:str):
        return cls(
            unit_id=unit_id,
            pressure = round(gauss(1),2)
        ) 

    def mutate(self,exclude:list=[]):
        exclude += ['time']
        names = [f.name for f in fields(self.__class__)
                  if f.type is float or float in get_args(f.type)]
        names = [n for n in names if n not in exclude]
        for name in names:
            try:
                val = gauss(getattr(self,name),getattr(self,name)*0.05)
            except ValueError:
                val = None
            setattr(self,name,round(val,2))

    def dump_line(self):
        data = [v for k,v in asdict(self).items() if k!='time']
        data = [f'+{d:.2f}'.zfill(6) if isinstance(d,float) else d for d in data]
        return ' '.join(data)

@dataclass(kw_only=True)
class AlicatMassFlowDF(AlicatBaseDF):    
    temp: Optional[float] = None
    vflow: Optional[float] = None
    mflow: Optional[float] = None
    setpoint: Optional[float] = None
    gas: Optional[str] = None
    status: Optional[str] = None

    @classmethod
    def parse_line(cls, raw_line:str,
                   pressure_unit:AlicatPressureUnits) -> AlicatMassFlowDF:
        parts = raw_line.strip().split()
        if len(parts) < 4:
            raise ValueError(f"Invalid Alicat response (too short): {raw_line!r}")
        
        base = AlicatBaseDF.parse_line(raw_line,pressure_unit)

        return cls(
                **asdict(base),  # unit_id, pressure, time
                temp=parts[2] if len(parts) > 2 and parts[2] != "" else None,
                vflow=parts[3] if len(parts) > 3 and parts[3] != "" else None,
                mflow=parts[4] if len(parts) > 4 and parts[4] != "" else None,
                setpoint=parts[5] if len(parts) > 5 and parts[5] != "" else None,
                gas=parts[6] if len(parts) > 7 else None,
                status=parts[7] if len(parts) >= 8 else None,
            )

    @classmethod
    def generate(cls,unit_id:str):
        """
        For use with mock serial device
        
        :param cls: Description
        :param unit_id: Description
        :type unit_id: str
        """
        return cls(
            unit_id=unit_id,
            pressure = round(gauss(1),2),
            temp = round(gauss(20),2),            
            vflow = round(uniform(40,50),2),
            mflow = round(gauss(1000),2),
            setpoint = 1000,
            gas = 'Air',
            status = 'HLD'
            ) 
    
    def mutate(self, exclude = ['setpoint','mflow']):
        super().mutate(exclude)
        if self.setpoint > 0:
            self.mflow = abs(round(gauss(self.setpoint,sigma=0.2),2))
        else:
            self.mflow = 0.0

class AlicatBase:
    
    def __init__(self,
                 serial_handler: utils.SimpleSerialDevice|utils.MockSerialDevice,
                 unit_id: str,
                 pressure_unit: AlicatPressureUnits
                 ):
        self.serial = serial_handler
        self.unit_id = unit_id
        self.pressure_unit = pressure_unit

    async def fetch_data(self) -> str:
        line = await self.serial.query(f'{self.unit_id}{AlicatCommands.POLL_DATA}\r')
        return line
    
    @classmethod
    def parse_command(cls,command:str) -> dict:
        pattern = compile(r'(?P<id>[a-zA-Z])(?P<cmd>.*)')
        
        if result := pattern.search(command):
            return result.groupdict() 
        else:
            return {} 

    @classmethod
    def mock_command_map(cls,command:str):
        pass

    @classmethod
    def _mock_load_data(cls, unit_id:str) -> dict:
        p = Path(f'periphs/.mock/{unit_id}.json')
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            return {}
        else:
            return json.loads(p.read_text())    

    @classmethod
    def _mock_persist_data(cls, unit_id:str, 
                           data:dict[str,str|float]
                           ):
        p = Path(f'periphs/.mock/{unit_id}.json')
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(data))

class AlicatFlowController(AlicatBase):
    
    async def fetch_data(self) -> AlicatMassFlowDF:
        line =  await super().fetch_data()
        logger.debug(f'Received line {line}')
        try:
            data = AlicatMassFlowDF.parse_line(line,self.pressure_unit)
            return data
        except Exception as e:
            logger.error(f'Unable to parse line {line}: "{e}"') 

    async def write_gas(self):
        pass
    
    async def write_setpoint(self, value:float, units:AlicatFlowUnits|None=None) -> bool:
        cmd = f'{self.unit_id}{AlicatCommands.QUERY_SETPOINT} {value} {"" if not units else units}'
        try:
            line = await self.serial.query(cmd.strip()+'\r')
            if line is not None: return True
        except Exception as e:
            logger.error(f'Unable to write setupoint to flow controller: {e}')
        return False

    @classmethod
    def mock_command_map(cls,command:str):
        parsed_line = cls.parse_command(command)
        unit_id = parsed_line.get('id')

        data = cls._mock_load_data(unit_id)
        if not data:
            data = AlicatMassFlowDF.generate(unit_id)
        else:
            data = AlicatMassFlowDF(**data)
            data.mutate()
    
        cmd = '' if not parsed_line.get('cmd').split() else parsed_line.get('cmd').split()[0] 
        line = None

        match cmd:

            case AlicatCommands.POLL_DATA:
                line = data.dump_line()

            case AlicatCommands.QUERY_SETPOINT:
                try:
                    new_setpoint = float(parsed_line.get('cmd').split()[1])
                except TypeError:
                    logger.error('Setpoint must be numeric')
                    return 
                
                line = [unit_id,
                        f'+{data.setpoint:.2f}'.zfill(6),
                        f'+{new_setpoint:.2f}'.zfill(6),
                        str(AlicatFlowUnits.SLPM),
                        'SLPM'
                        ]
                data.setpoint = new_setpoint

                line = ' '.join(line)

            case AlicatCommands.ACTIVE_GAS:
                pass

            case _:
                pass
                
        cls._mock_persist_data(unit_id,asdict(data))
        return line
        
class AlicatDiffPressure(AlicatBase):
    
    async def fetch_data(self) -> AlicatBaseDF:
        line =  await super().fetch_data()
        logger.debug(f'Received line {line}')
        try:
            data = AlicatBaseDF.parse_line(line,self.pressure_unit)
            return data
        except Exception as e:
            logger.error(f'Unable to parse line {line}: "{e}"') 

    @classmethod
    def mock_command_map(cls,command:str):
        parsed_line = cls.parse_command(command)
        cmd = parsed_line.get('cmd')
        unit_id = parsed_line.get('id')

        data = cls._mock_load_data(unit_id)
        if not data:
            data = AlicatBaseDF.generate(unit_id)
        else:
            data = AlicatBaseDF(**data)
            data.mutate()
        line = None

        match cmd:

            case AlicatCommands.POLL_DATA:
                line =  data.dump_line()

            case _:
                pass

        cls._mock_persist_data(unit_id,asdict(data))
        return line
