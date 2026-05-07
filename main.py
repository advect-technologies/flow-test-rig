import asyncio
import sys
import machine
import sys
import time
from rich import print
from pathlib import Path
from config_loader import load_test_rig_config
from loguru import logger
from daq_writer import DaqJsonlWriter
from daq_tools import DAQIngestor

# Windows asyncio fix — must be very early
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger.remove()
logger.add(sys.stderr,level='INFO')

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

def main():
    stop_flag = asyncio.Event()
    asyncio.run(flow_tasks(stop_flag))

if __name__ == "__main__":
   main()