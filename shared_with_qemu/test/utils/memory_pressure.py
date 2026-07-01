import gc
import os
import random
import threading
import time


PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


class MemoryPressureThread(threading.Thread):
    def __init__(
        self,
        bytes_target: int,
        *,
        seed: int,
        chunk_bytes: int = 8 * 1024 * 1024,
        interval_s: float = 0.02,
    ) -> None:
        super().__init__(daemon=True)
        self.bytes_target = max(0, int(bytes_target))
        self.seed = seed
        self.chunk_bytes = max(PAGE_SIZE, int(chunk_bytes))
        self.interval_s = interval_s
        self._stop_event = threading.Event()
        self._chunks: list[bytearray] = []

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if self.bytes_target <= 0:
            return

        rng = random.Random(self.seed)
        touched = 0
        try:
            while not self._stop_event.is_set():
                while touched < self.bytes_target and not self._stop_event.is_set():
                    buf = bytearray(self.chunk_bytes)
                    for off in range(0, len(buf), PAGE_SIZE):
                        buf[off] = (rng.randint(0, 255) + off // PAGE_SIZE) & 0xFF
                    self._chunks.append(buf)
                    touched += len(buf)

                rng.shuffle(self._chunks)
                hot = max(1, len(self._chunks) // 4)
                for buf in self._chunks[:hot]:
                    for off in range(0, len(buf), PAGE_SIZE * 16):
                        buf[off] = (buf[off] + 1) & 0xFF

                if len(self._chunks) > 4:
                    drop = max(1, len(self._chunks) // 3)
                    del self._chunks[:drop]
                    gc.collect()
                    touched = sum(len(chunk) for chunk in self._chunks)

                time.sleep(self.interval_s)
        finally:
            self._chunks.clear()
            gc.collect()
