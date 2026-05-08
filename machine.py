import periphs
import models
import asyncio
import time
from dataclasses import asdict
from loguru import logger
from typing import Optional
from daq_writer import DaqJsonlWriter
from pathlib import Path
from daq_tools import DAQIngestor

class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""

async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()

async def event_handler(test_rig:TestRig,
                        event_q:asyncio.Queue,
                        ):
    while True:

        try:
            event:models.Event =  await event_q.get()        
            status = None

            match(event.name):

                case models.EventNames.STATE_CHANGE:
                    logger.debug(f'Entering state "{event.new_state}"')
                    if event.new_state == models.States.FAULT: 
                        # status = await abort_test(event.reason)
                        pass
                    elif event.new_state == models.States.IDLE:
                        pass
                        # await status = test_rig.change_setpoint(0)
                    elif event.new_state == models.States.ACTIVE:
                        status = True     
                    else:
                        status = True                      
                    
                    if status:
                        pass
                        # test_rig.state_manager.state = event.new_state
                        
                case models.EventNames.STOP_BUTTON:
                    logger.debug('Handling stop button event')
                    await event_q.put(models.StateChangeEvent(new_state=models.States.IDLE))

                case models.EventNames.CHANGE_SETPOINT:
                    logger.debug(f'Changing setpoint to {event.value}')
                    status = await test_rig.change_setpoint(event.value)

                case models.EventNames.TARE_SCALE:
                    logger.debug(f'Zeroing the Scale')
                    status = await test_rig.zero_scale()

                case _:
                    logger.warning('Unhandled event')
                
            if status == False:
                if event.retry:
                    logger.warning('Event not handled correctly, retrying')
                    await event_q.put(event)
                else:
                    logger.warning('Event not handled corectly, not resubmitting')
                       
        except Exception as ex:
            logger.exception('Exception in event handler')

async def flow_tasks(test_rig: TestRig,
                     stop_flag: asyncio.Event,
                     test_rig_event_q: asyncio.Queue,
                     on_metrics_update = None,
                     ):
    print("Hello from st-test-rig!")
    metrics_updated_flag = asyncio.Event()
    daq_writer = DaqJsonlWriter(test_rig.config.daq)

    async def update_metrics_loop():
        while True:
            await test_rig.update_metrics()
            metrics_updated_flag.set()            
            if on_metrics_update is not None:
                on_metrics_update(test_rig.fetch_flat_metrics())
            await asyncio.sleep(1)
    
    async def report_metrics_loop():
        last_write = 0
        while True:
            await metrics_updated_flag.wait()
            metrics_updated_flag.clear()
            if time.time() - last_write > test_rig.config.daq.sample_period_s:
                last_write = time.time()
                await daq_writer.write(test_rig._metrics)                        

    async def start_daq_ingestor():
        if Path('daq_config.toml').exists():
            config_path = Path('daq_config.toml')
        else:
            config_path = Path('default_daq_config.toml')        
        try:
            async with DAQIngestor.from_config_file(config_path) as ingestor:
                logger.info("DAQIngestor started — watching for JSONL files")
                await asyncio.Event().wait()  # run forever until cancelled
        except Exception as e:
            logger.error(f"DAQIngestor failed: {e}")

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(update_metrics_loop())
            tg.create_task(report_metrics_loop())
            tg.create_task(event_handler(test_rig,test_rig_event_q))
            tg.create_task(test_rig.do_supervisory_control(test_rig_event_q))
            tg.create_task(start_daq_ingestor())
            await stop_flag.wait()
            await daq_writer.close()
            tg.create_task(force_terminate_task_group())

    except* TerminateTaskGroup:

        logger.warning('All tasks stopped, shutting down')

    except* Exception as eg:
            # Optional: catch real errors from children
            logger.error(f"Background tasks failed: {eg.exceptions}")

class TestRig:

    def __init__(self,
                 config:models.TestRigConfig
                 ):
        self.config = config
    
        self.mass: periphs.scale.ADEK30KL = None
        self.flow: periphs.alicat.AlicatFlowController = None
        self.high_dp: periphs.alicat.AlicatDiffPressure = None
        self.low_dp: periphs.alicat.AlicatDiffPressure = None
        
        self._metrics: Optional[models.TestRigDF] = None

        try:
            if config.mock:
                self._load_mock_devices()
            else: 
                self._load_real_devices()
        except Exception as e:
            raise RuntimeError(f'Error in initializing serial deivces: {e}')

    def _load_mock_devices(self):
        mass_serial = periphs.utils.MockSerialDevice(
            response_mapper=periphs.scale.ADEK30KL.mock_command_map,
            name='MockScale'
        )
        alicat_flow_serial = periphs.utils.MockSerialDevice(
            response_mapper=periphs.alicat.AlicatFlowController.mock_command_map,
            name='FlowMockSerial'
        )
        alicat_pressure_serial = periphs.utils.MockSerialDevice(
            response_mapper=periphs.alicat.AlicatDiffPressure.mock_command_map,
            name='PressureMockSerial'
        )

        self.mass = periphs.scale.ADEK30KL(mass_serial)
        self.flow = periphs.alicat.AlicatFlowController(alicat_flow_serial,
                                                                self.config.flow.unit_id,
                                                                self.config.flow.pressure_unit)
        self.high_dp = periphs.alicat.AlicatDiffPressure(alicat_pressure_serial,
                                                               self.config.high_dp.unit_id,
                                                               self.config.high_dp.pressure_unit)
        self.low_dp = periphs.alicat.AlicatDiffPressure(alicat_pressure_serial,
                                                              self.config.low_dp.unit_id,
                                                              self.config.low_dp.pressure_unit)

    def _load_real_devices(self):

        # Scale always uses it's own serial device
        mass_serial = periphs.utils.SimpleSerialDevice(
            **asdict(self.config.mass.serial),
            name='ScaleSerial'
        )
        self.mass = periphs.scale.ADEK30KL(mass_serial)

        # A shared alicat serial config may be defined
        if self.config.alicat_shared.serial:
            shared_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.alicat_shared.serial),
                name='AlicatSharedSerial'
            )
        else:
            shared_alicat_serial = None

        # Each instrument may also have it's own serial device defined,
        # if so, use it; otherwise, default to shared serial
        if self.config.flow.serial:   
            flow_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.flow.serial),
                name='AlicatFlowSerial'
            )
            self.flow = periphs.alicat.AlicatFlowController(flow_alicat_serial,
                                                                    self.config.flow.unit_id,
                                                                    self.config.flow.pressure_unit)
        elif shared_alicat_serial:
            self.flow = periphs.alicat.AlicatFlowController(shared_alicat_serial,
                                                                    self.config.flow.unit_id,
                                                                    self.config.flow.pressure_unit)
        else:
            raise RuntimeError('No serial device available for flow controller')

        if self.config.high_dp.serial:
            high_dp_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.high_dp.serial),
                name='AlicatHighDPressureSerial'
            )
            self.high_dp = periphs.alicat.AlicatDiffPressure(high_dp_alicat_serial,
                                                                   self.config.high_dp.unit_id,
                                                                   self.config.high_dp.pressure_unit)
        elif shared_alicat_serial:
            self.high_dp = periphs.alicat.AlicatDiffPressure(shared_alicat_serial,
                                                                   self.config.high_dp.unit_id,
                                                                   self.config.high_dp.pressure_unit)
        else:
            raise RuntimeError('No serial device available for high range pressure sensor')

        if self.config.low_dp.serial:
            low_dp_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.low_dp.serial),
                name='AlicatLowDPressureSerial'
            )
            self.low_dp = periphs.alicat.AlicatDiffPressure(low_dp_alicat_serial,
                                                                  self.config.low_dp.unit_id,
                                                                  self.config.low_dp.pressure_unit)
        elif shared_alicat_serial:
            self.low_dp = periphs.alicat.AlicatDiffPressure(shared_alicat_serial,
                                                                  self.config.low_dp.unit_id,
                                                                  self.config.low_dp.pressure_unit)
        else:
            raise RuntimeError('No serial device available for low range pressure sensor')

    async def update_metrics(self):
        try:
            mass_data = await self.mass.fetch_data()
            flow_data = await self.flow.fetch_data()
            low_dp_data = await self.low_dp.fetch_data()
            high_dp_data = await self.high_dp.fetch_data()
            self._metrics = models.TestRigDF(
                mass=mass_data,
                flow=flow_data,
                low_dp=low_dp_data,
                high_dp=high_dp_data
            )
        except Exception as e:
            logger.error(f'Error in update data task: {e}')

    async def report_metrics(self):
        print(self._metrics.flatten())
        pass
    
    def fetch_flat_metrics(self) -> dict:
        return self._metrics.flatten()

    async def change_setpoint(self, val: float):
        await self.flow.write_setpoint(val)

    async def zero_scale(self):
        await self.mass.tare()        

    async def do_supervisory_control(self,event_q:asyncio.Queue):
        shutoff_point = self.config.low_dp.full_scale_max - self.config.low_dp.full_scale_max*0.05 
        stop_requested = 0
        while True:
            if self._metrics is not None:
                try:
                    if self._metrics.low_dp.pressure >= shutoff_point:
                        if time.time() - stop_requested > 30:
                            logger.warning('Full scale range exceeded on pressure sensor, stopping flow')
                            event = models.SetpointEvent(retry=True,value=0)
                            stop_requested = event.timestamp
                            await event_q.put(event)
                        else:
                            logger.debug('Full scale range exceeded on pressure sensor,stop request already sent')                    
                except Exception as e:
                    logger.warning('Issue when doing supervisory control')
            
            await asyncio.sleep(1)
            