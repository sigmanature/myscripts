import os
import time
import threading
import random
from .io import ensure_dir, open_rw, ensure_file_size, pwrite_pattern, fsync

class ChurnThread(threading.Thread):
    """
    Create/write/delete files repeatedly to generate segment churn and trigger GC.
    inline_mode=True -> tiny files (likely inline_data)
    inline_mode=False -> larger files
    """
    def __init__(
        self,
        churn_dir: str,
        inline_mode: bool,
        file_size: int,
        files_per_round: int,
        keep_fraction: float,
        interval_s: float,
        seed: int,
        verbose: bool = False,
    ):
        super().__init__(daemon=True)
        self.churn_dir = churn_dir
        self.inline_mode = inline_mode
        self.file_size = file_size
        self.files_per_round = files_per_round
        self.keep_fraction = keep_fraction
        self.interval_s = interval_s
        self.seed = seed
        self.verbose = verbose
        self._stop = threading.Event()
        self._live = []
        self.rounds = 0
        self.created = 0
        self.deleted = 0

    def stop(self) -> None:
        self._stop.set()

    def _mk_one(self, idx: int) -> str:
        name = f"{'inl' if self.inline_mode else 'reg'}_{os.getpid()}_{threading.get_ident()}_{self.rounds}_{idx}.bin"
        return os.path.join(self.churn_dir, name)

    def run(self) -> None:
        ensure_dir(self.churn_dir)
        rnd = random.Random(self.seed)

        if self.verbose:
            print(f"[churn] dir={self.churn_dir} inline={self.inline_mode} size={self.file_size}B", flush=True)

        while not self._stop.is_set():
            self.rounds += 1
            batch = []
            for i in range(self.files_per_round):
                path = self._mk_one(i)
                fd = open_rw(path, create=True)
                try:
                    ensure_file_size(fd, self.file_size)
                    pwrite_pattern(fd, 0, self.file_size, seed=rnd.randint(1, 1_000_000))
                    fsync(fd)
                finally:
                    os.close(fd)
                batch.append(path)
                self.created += 1

            rnd.shuffle(batch)
            keep_n = int(len(batch) * self.keep_fraction)
            keep = batch[:keep_n]
            drop = batch[keep_n:]

            for p in drop:
                try:
                    os.unlink(p)
                    self.deleted += 1
                except FileNotFoundError:
                    pass

            self._live.extend(keep)

            if len(self._live) > self.files_per_round * 4:
                rnd.shuffle(self._live)
                to_del = self._live[: self.files_per_round]
                self._live = self._live[self.files_per_round :]
                for p in to_del:
                    try:
                        os.unlink(p)
                        self.deleted += 1
                    except FileNotFoundError:
                        pass

            time.sleep(self.interval_s)
