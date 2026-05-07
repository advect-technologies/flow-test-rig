import asyncio
import sys
import machine
import models
import time
from daq_tools import DAQIngestor
from daq_writer import DaqJsonlWriter
from pathlib import Path

# Windows asyncio fix — must be very early
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from textual.app        import App, ComposeResult, on
from textual.widgets    import Header, Footer, DataTable, Input, RichLog
from textual.reactive   import reactive
from textual.message    import Message

from config_loader import load_test_rig_config
from loguru import logger


logger.remove()
logger.add(sys.stderr,level='INFO')

# Add file sink with rotation, retention, and compression
logger.add(
    "logs/st-test-rig_{time:YYYY-MM-DD}.log",   # daily files: st-test-rig_2026-02-04.log etc.
    rotation="00:00",                           # rotate at midnight (daily)
    retention="30 days",                        # keep last 30 days of logs
    compression="zip",                          # compress old files to .zip
    level="INFO",                              # capture everything to file (even debug)
    enqueue=True,                               # async-safe, good for background tasks
    backtrace=True,
    diagnose=True,
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message} | {extra}",
)

class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""

async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()

test_rig = machine.TestRig(load_test_rig_config())
test_rig_event_q = asyncio.Queue()

async def flow_tasks(stop_flag: asyncio.Event,
                     on_metrics_update = None):
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
            tg.create_task(machine.event_handler(test_rig,test_rig_event_q))
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

###################################################################

class MetricsUpdated(Message):
    """Posted whenever fresh metrics are available."""

    def __init__(self, metrics: dict) -> None:
        super().__init__()
        self.metrics = metrics

class SensorMonitor(App):
    
    # Reactive attribute — changing it triggers watch_metrics_table_data
    metrics_table_data = reactive({}, layout=True, repaint=True)    

    def compose(self) -> ComposeResult:
        yield Header()
        yield RichLog(id="log", markup=True)
        yield DataTable(id='metrics-table')
        yield Input(placeholder="Type commands (help, quit, status…)")
        yield Footer()

    def on_mount(self) -> None:
        self.log_widget = self.query_one(RichLog)
        self.stop_flag = asyncio.Event()

        # Initialize table
        table = self.query_one(DataTable)
        table.add_columns("Parameter", "Value", "Unit")
        table.zebra_stripes = True
        table.cursor_type = "row"

        self.run_worker(self.run_flow_app, exclusive=True, thread=False)

    def watch_metrics_table_data(self, new_data: dict) -> None:
        """Called automatically whenever metrics_table_data changes"""
        table = self.query_one("#metrics-table", DataTable)
        table.clear(columns=False)  # keep columns, only clear rows

        if not new_data:
            table.add_row("No data yet", "", "")
            return

        for param, info in sorted(new_data.items()):
            # Flexible handling of different dict shapes
            if isinstance(info, dict):
                value = info.get("value")
                unit  = info.get("unit", "")
            else:
                # fallback if flat dict like {"Inlet Pressure": 101.3}
                value = info
                unit  = ""

            value_str = f"{value:.4g}" if isinstance(value, (int, float)) else str(value)
            table.add_row(param, value_str, unit)

    @on(MetricsUpdated)
    def handle_metrics_update(self, event: MetricsUpdated) -> None:
        """Where the callback posts arrive"""
        self.metrics_table_data = event.metrics   # ← this triggers the watcher

    def post_metrics_update(self, metrics: dict):
        """Helper to post the message — keeps flow_tasks agnostic."""
        self.post_message(MetricsUpdated(metrics))

    async def run_flow_app(self):
        await flow_tasks(self.stop_flag,on_metrics_update=self.post_metrics_update)

    @on(Input.Submitted)
    def handle_command(self, event: Input.Submitted):
        cmd = event.value.strip().lower()
        log = self.log_widget
        log.write(f"> {event.value}")

    
        if cmd in ("q", "quit", "exit"):
            log.write("[bold red]Shutting down…[/]")
            self.stop_flag.set()
            self.exit()

        elif cmd == "help":
            log.write("[dim]Commands: setpoint <value>, tare, help, quit[/]")

        elif 'setpoint' in cmd:
            if len(cmd.split()) == 2:
                new_setpoint = cmd.split()[-1]
                log.write(f'Setting setpoint to {new_setpoint}')
                test_rig_event_q.put_nowait(models.SetpointEvent(retry=True,value=new_setpoint))

        elif cmd == 'tare':
            log.write(f'Zeroing Scale')
            test_rig_event_q.put_nowait(models.TareScaleEvent(retry=True))

        event.input.clear()

if __name__ == "__main__":
    SensorMonitor().run()