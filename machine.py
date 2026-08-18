import asyncio
import time
from dataclasses import asdict
from pathlib import Path

from daq_tools import DAQIngestor
from loguru import logger

import config as cfg
import models
import periphs
from daq_writer import DaqJsonlWriter


class TestIncompleteError(Exception):
    """Raise Exception for incomplete test"""


class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""


async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()


async def event_handler(
    test_rig: "TestRig",
    event_q: asyncio.Queue,
):
    while True:
        try:
            event: models.Event = await event_q.get()
            status = None

            match event.name:
                case models.EventNames.STATE_CHANGE:
                    if event.new_state == test_rig.state:
                        logger.info(f"Already in state {event.new_state}, ignore")
                        continue

                    logger.info(f'Entering state "{event.new_state}"')
                    if event.new_state == models.States.FAULT:
                        test_rig.state = models.States.FAULT
                        status = True
                    elif event.new_state == models.States.IDLE:
                        test_rig.state = models.States.IDLE
                        status = True
                    elif event.new_state == models.States.ACTIVE:
                        test_rig.state = models.States.ACTIVE
                        status = True
                    else:
                        status = True

                    if status:
                        pass

                case models.EventNames.START_BUTTON:
                    logger.info("Handling Start button event")
                    await event_q.put(
                        models.StateChangeEvent(new_state=models.States.ACTIVE)
                    )

                case models.EventNames.STOP_BUTTON:
                    logger.info("Handling stop button event")
                    await event_q.put(
                        models.StateChangeEvent(new_state=models.States.IDLE)
                    )

                case models.EventNames.TEST_FINISH:
                    logger.success("Test complete")
                    await event_q.put(
                        models.StateChangeEvent(new_state=models.States.IDLE)
                    )

                case models.EventNames.TEST_CANCEL:
                    logger.warning("Test Cancelled")
                    await event_q.put(
                        models.StateChangeEvent(new_state=models.States.IDLE)
                    )

                case models.EventNames.TEST_CRASH:
                    logger.error("Test Crashed")
                    await event_q.put(
                        models.StateChangeEvent(new_state=models.States.FAULT)
                    )

                case models.EventNames.METADATA_UPDATE:
                    logger.debug(f"Metadata Update received {event.meta}")
                    test_rig.user_meta = event.meta

                case models.EventNames.CHANGE_SETPOINT:
                    logger.info(f"Changing setpoint to {event.value}")
                    status = await test_rig.change_setpoint(event.value)

                case models.EventNames.TARE_SCALE:
                    logger.info("Zeroing the Scale")
                    status = await test_rig.zero_scale()

                case models.EventNames.PROTOCOL_CHANGE:
                    if event.file and event.file.exists():
                        test_rig.test_protocol_filename = event.file
                        logger.success(f"✅ Loaded protocol: {event.file.name}")
                        # Optional: auto-reload or notify
                    else:
                        logger.error(f"Protocol file not found: {event.file}")

                case _:
                    logger.warning("Unhandled event")

            if status == False:
                if event.retry:
                    logger.warning("Event not handled correctly, retrying")
                    await event_q.put(event)
                else:
                    logger.warning("Event not handled corectly, not resubmitting")

        except Exception:
            logger.exception("Exception in event handler")


async def flow_tasks(
    test_rig: "TestRig",
    stop_flag: asyncio.Event,
    test_rig_event_q: asyncio.Queue,
    on_metrics_update=lambda: None,
):
    print("Hello from st-test-rig!")
    metrics_updated_flag = asyncio.Event()
    daq_writer = DaqJsonlWriter(test_rig.config.daq)

    async def test_runner_loop():
        while True:
            await test_rig.state_change.wait()
            test_rig.state_change.clear()

            if test_rig.state == models.States.ACTIVE:
                logger.info("Starting Test")
                await test_rig.run_test(event_q=test_rig_event_q, daq_writer=daq_writer)

    async def update_metrics_loop():
        while True:
            await test_rig.update_metrics()
            metrics_updated_flag.set()
            on_metrics_update()
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
        if Path("daq_config.toml").exists():
            config_path = Path("daq_config.toml")
        else:
            config_path = Path("default_daq_config.toml")
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
            tg.create_task(test_runner_loop())
            tg.create_task(event_handler(test_rig, test_rig_event_q))
            tg.create_task(test_rig.do_supervisory_control(test_rig_event_q))
            tg.create_task(start_daq_ingestor())
            await stop_flag.wait()
            await daq_writer.close()
            tg.create_task(force_terminate_task_group())

    except* TerminateTaskGroup:
        logger.warning("All tasks stopped, shutting down")

    except* Exception as eg:
        # Optional: catch real errors from children
        logger.error(f"Background tasks failed: {eg.exceptions}")


class TestHelper:
    def __init__(self, test: models.FlowTest | None = None):
        self.test = test
        self.start_time: float = time.time()
        self.stage_num = 0
        self.stage_total = len(test.protocol.stages) if test else 0
        self.total_duration = self.resolve_total_duration()

    def resolve_total_duration(self) -> int | None:
        if self.test is None:
            return None
        try:
            return int(sum([stage._duration_s for stage in self.test.protocol.stages]))
        except Exception:
            logger.warning("unable to calculate total test duration")
            return None

    def flatten(self) -> dict:
        if self.test is None:
            return {}
        return {
            "name": self.test.protocol.name,
            "current_stage": self.stage_num,
            "stage_total": self.stage_total,
            "run_time": int(time.time() - self.start_time),
            "total_duration": self.total_duration,
        }


class TestRig:
    def __init__(self, config: cfg.TestRigConfig):
        self.config = config

        self.mass: periphs.scale.ADEK30KL | None = None
        self.flow: periphs.alicat.AlicatFlowController | None = None
        self.high_dp: periphs.alicat.AlicatDiffPressure | None = None
        self.low_dp: periphs.alicat.AlicatDiffPressure | None = None

        self._metrics: models.TestRigDF | None = None
        self._state = models.States.IDLE

        self.test_q = asyncio.Queue()
        self.state_change: asyncio.Event = asyncio.Event()
        self.test_helper: TestHelper = TestHelper()
        self.test_protocol_filename: Path = Path("protocols", "example-protocol.json")
        self.user_meta = models.UserMetadata.load_user_data()

        try:
            if config.mock:
                self._load_mock_devices()
            else:
                self._load_real_devices()
        except Exception as e:
            raise RuntimeError(f"Error in initializing serial deivces: {e}")

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, val: models.States):
        if val not in models.States:
            return
        self._state = val
        self.state_change.set()

    def _load_mock_devices(self):
        mass_serial = periphs.utils.MockSerialDevice(
            response_mapper=periphs.scale.ADEK30KL.mock_command_map, name="MockScale"
        )
        alicat_flow_serial = periphs.utils.MockSerialDevice(
            response_mapper=periphs.alicat.AlicatFlowController.mock_command_map,
            name="FlowMockSerial",
        )
        alicat_pressure_serial = periphs.utils.MockSerialDevice(
            response_mapper=periphs.alicat.AlicatDiffPressure.mock_command_map,
            name="PressureMockSerial",
        )

        self.mass = periphs.scale.ADEK30KL(mass_serial)
        self.flow = periphs.alicat.AlicatFlowController(
            alicat_flow_serial,
            self.config.flow.unit_id,
            self.config.flow.pressure_unit_code,
        )
        self.high_dp = periphs.alicat.AlicatDiffPressure(
            alicat_pressure_serial,
            self.config.high_dp.unit_id,
            self.config.high_dp.pressure_unit_code,
        )
        self.low_dp = periphs.alicat.AlicatDiffPressure(
            alicat_pressure_serial,
            self.config.low_dp.unit_id,
            self.config.low_dp.pressure_unit_code,
        )

    def _load_real_devices(self):

        # Scale always uses it's own serial device
        mass_serial = periphs.utils.SimpleSerialDevice(
            **asdict(self.config.mass.serial), name="ScaleSerial"
        )
        self.mass = periphs.scale.ADEK30KL(mass_serial)

        # A shared alicat serial config may be defined
        if self.config.alicat_shared.serial:
            shared_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.alicat_shared.serial), name="AlicatSharedSerial"
            )
        else:
            shared_alicat_serial = None

        # Each instrument may also have it's own serial device defined,
        # if so, use it; otherwise, default to shared serial
        if self.config.flow.serial:
            flow_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.flow.serial), name="AlicatFlowSerial"
            )
            self.flow = periphs.alicat.AlicatFlowController(
                flow_alicat_serial,
                self.config.flow.unit_id,
                self.config.flow.pressure_unit_code,
            )
        elif shared_alicat_serial:
            self.flow = periphs.alicat.AlicatFlowController(
                shared_alicat_serial,
                self.config.flow.unit_id,
                self.config.flow.pressure_unit_code,
            )
        else:
            raise RuntimeError("No serial device available for flow controller")

        if self.config.high_dp.serial:
            high_dp_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.high_dp.serial), name="AlicatHighDPressureSerial"
            )
            self.high_dp = periphs.alicat.AlicatDiffPressure(
                high_dp_alicat_serial,
                self.config.high_dp.unit_id,
                self.config.high_dp.pressure_unit_code,
            )
        elif shared_alicat_serial:
            self.high_dp = periphs.alicat.AlicatDiffPressure(
                shared_alicat_serial,
                self.config.high_dp.unit_id,
                self.config.high_dp.pressure_unit_code,
            )
        else:
            raise RuntimeError(
                "No serial device available for high range pressure sensor"
            )

        if self.config.low_dp.serial:
            low_dp_alicat_serial = periphs.utils.SimpleSerialDevice(
                **asdict(self.config.low_dp.serial), name="AlicatLowDPressureSerial"
            )
            self.low_dp = periphs.alicat.AlicatDiffPressure(
                low_dp_alicat_serial,
                self.config.low_dp.unit_id,
                self.config.low_dp.pressure_unit_code,
            )
        elif shared_alicat_serial:
            self.low_dp = periphs.alicat.AlicatDiffPressure(
                shared_alicat_serial,
                self.config.low_dp.unit_id,
                self.config.low_dp.pressure_unit_code,
            )
        else:
            raise RuntimeError(
                "No serial device available for low range pressure sensor"
            )

    async def update_metrics(self):
        try:
            mass_data = await self.mass.fetch_data()
            flow_data = await self.flow.fetch_data()
            low_dp_data = await self.low_dp.fetch_data()
            high_dp_data = await self.high_dp.fetch_data()

            if self.test_helper.test:
                test_id = self.test_helper.test.metadata.test_id
            else:
                test_id = None
            self._metrics = models.TestRigDF(
                test_id=test_id,
                station=self.config.station,
                mass=mass_data,
                flow=flow_data,
                low_dp=low_dp_data,
                high_dp=high_dp_data,
            )
        except Exception as e:
            logger.error(f"Error in update data task: {e}")

    def fetch_flat_test_status(self) -> dict:
        if self.state != models.States.ACTIVE:
            return {}

        test_data = self.test_helper.flatten()
        return {"station": self.config.station, **test_data}

    def fetch_flat_metrics(self) -> dict:
        if self._metrics is None:
            return {}
        return self._metrics.flatten()

    async def change_setpoint(self, val: float):
        await self.flow.write_setpoint(val)

    async def zero_scale(self):
        await self.mass.tare()

    async def do_supervisory_control(self, event_q: asyncio.Queue):
        shutoff_point = (
            self.config.low_dp.full_scale_max - self.config.low_dp.full_scale_max * 0.05
        )
        stop_requested = 0
        while True:
            if self._metrics is not None:
                try:
                    if self._metrics.low_dp.pressure >= shutoff_point:
                        if time.time() - stop_requested > 30:
                            logger.warning(
                                "Full scale range exceeded on pressure sensor, stopping flow"
                            )
                            event = models.SetpointEvent(retry=True, value=0)
                            stop_requested = event.timestamp
                            await event_q.put(event)
                        else:
                            logger.debug(
                                "Full scale range exceeded on pressure sensor,stop request already sent"
                            )
                except Exception:
                    logger.warning("Issue when doing supervisory control")

            await asyncio.sleep(1)

    async def load_test(self) -> models.FlowTest:
        protocol = models.TestProtocol.from_json(self.test_protocol_filename)
        metadata = models.TestMetadata(
            station=self.config.station,
            protocol_file=protocol.name,
            **self.user_meta.to_dict(),
        )

        return models.FlowTest(protocol=protocol, metadata=metadata)

    async def run_test(self, event_q: asyncio.Queue, daq_writer: DaqJsonlWriter):
        test = await self.load_test()
        self.test_helper = TestHelper(test=test)

        await daq_writer.write(test.metadata)
        try:
            for i, stage in enumerate(test.protocol.stages):
                self.test_helper.stage_num = i + 1

                targets = []
                time_step = stage._duration_s

                if stage.TYPE == "RAMP":
                    targets = stage.targets
                    time_step = stage.time_step_s
                elif stage.TYPE == "STABLE":
                    targets = [stage.target]

                for target in targets:
                    event = models.SetpointEvent(retry=True, value=target)
                    await event_q.put(event)

                    target_start_time = time.time()
                    while self.state == models.States.ACTIVE:
                        if time.time() - target_start_time >= time_step:
                            break
                        else:
                            # logger.debug(f'{int(time.time()-target_start_time)}s of {time_step}s spent at target')
                            # logger.info(self.test_helper.flatten())
                            await asyncio.sleep(1)

                    if self.state != models.States.ACTIVE:
                        raise TestIncompleteError()

            event = models.TestFinish()
            await event_q.put(event)
        except TestIncompleteError:
            logger.warning("Test, leaving test loop")
            event = models.TestCancel()
            await event_q.put(event)
        except asyncio.CancelledError:
            event = models.TestCancel()
            await event_q.put(event)
        except Exception:
            event = models.TestCrash()
            await event_q.put(event)
        finally:
            self.test_helper = TestHelper()
