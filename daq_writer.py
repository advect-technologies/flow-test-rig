from pathlib import Path
import asyncio
import time
from loguru import logger
from models import TestRigDF, DaqConfig


class DaqJsonlWriter:
    """Buffered JSONL writer for daq-tools.
    
    Accumulates DataPoints in memory and dumps them to a new .jsonl file
    when the buffer hits max_size. This plays nicely with daq-tools' 
    watch-and-delete pattern.
    """

    def __init__(
        self,
        config: DaqConfig
    ):
        self.config = config
        self.watch_dir = Path(config.watch_dir)
        self.watch_dir.mkdir(parents=True, exist_ok=True)

        self.max_buffer_size = config.buffer.max_size
        self.max_age_seconds = config.buffer.max_age_seconds

        self._buffer: list[str] = []
        self._last_dump_time = time.time()
        self._lock = asyncio.Lock()

        logger.info(f"DAQ buffered writer initialized → watch_dir={self.watch_dir}, "
                   f"max_buffer={self.max_buffer_size}, max_age={self.max_age_seconds}s")

    async def write(self, record: TestRigDF) -> None:
        """Add one TestRigDF record (as DataPoint lines) to the buffer.
        Dump to file if buffer is full or too old.
        """
        try:
            points = record.to_data_points(self.config.measurement,self.config.base_tags)

            new_lines = [p.to_json() + "\n" for p in points]

            async with self._lock:
                self._buffer.extend(new_lines)

                now = time.time()
                should_dump = (
                    len(self._buffer) >= self.max_buffer_size or
                    (now - self._last_dump_time) >= self.max_age_seconds
                )

                if should_dump and self._buffer:
                    await self._dump_buffer()
                    self._last_dump_time = now

        except Exception as e:
            logger.exception(f"Error writing record to DAQ buffer: {e}")

    async def _dump_buffer(self) -> None:
        """Atomically write buffer to a new .jsonl file and clear it."""
        if not self._buffer:
            return

        timestamp = int(time.time() * 1000)  # ms precision for uniqueness
        filepath = self.watch_dir / f"rig_{timestamp}.jsonl"

        try:
            content = "".join(self._buffer)
            # Use a temp file + rename for atomic write (safer with watcher)
            temp_path = filepath.with_suffix(".jsonl.tmp")
            temp_path.write_text(content, encoding="utf-8")
            temp_path.rename(filepath)

            logger.debug(f"Dumped {len(self._buffer)} lines to {filepath.name}")
            self._buffer.clear()

        except Exception as e:
            logger.error(f"Failed to dump buffer to {filepath}: {e}")
            # Optionally keep the lines in buffer for retry

    async def close(self) -> None:
        """Force dump any remaining data on shutdown."""
        async with self._lock:
            if self._buffer:
                await self._dump_buffer()
        logger.info("DAQ writer closed")