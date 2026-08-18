import asyncio
import sys

from loguru import logger

import machine
from config_loader import load_test_rig_config

# Windows asyncio fix — must be very early
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger.remove()
logger.add(sys.stderr, level="INFO")


class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""


async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()


test_rig = machine.TestRig(load_test_rig_config())
test_rig_event_q = asyncio.Queue()


def main():
    stop_flag = asyncio.Event()
    # asyncio.run(flow_tasks(stop_flag))
    asyncio.run(
        machine.flow_tasks(
            test_rig=test_rig, stop_flag=stop_flag, test_rig_event_q=test_rig_event_q
        )
    )


if __name__ == "__main__":
    main()
