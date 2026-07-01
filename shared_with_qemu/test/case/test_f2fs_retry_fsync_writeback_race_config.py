#!/usr/bin/env python3
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

import test_f2fs_retry_fsync_writeback_race as case_mod


def run() -> None:
    if case_mod.FSYNC_WORKERS < 2:
        raise RuntimeError(f"need >=2 fsync workers, got {case_mod.FSYNC_WORKERS}")
    if case_mod.BACKGROUND_WRITERS < 1:
        raise RuntimeError(
            f"need background writers to overlap writeback, got {case_mod.BACKGROUND_WRITERS}"
        )
    if tuple(case_mod.SYSRQ_SEQUENCE) != ("w", "t", "l"):
        raise RuntimeError(f"unexpected sysrq sequence: {case_mod.SYSRQ_SEQUENCE}")
    if case_mod.RETRY_FORCE_LOOPS < 1:
        raise RuntimeError(
            f"retry injection should default on for deterministic repro, got {case_mod.RETRY_FORCE_LOOPS}"
        )


def main() -> None:
    run()
    print("[OK] test_f2fs_retry_fsync_writeback_race_config passed", flush=True)


if __name__ == "__main__":
    main()
