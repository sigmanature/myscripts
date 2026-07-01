#!/usr/bin/env python3
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.f2fs_retry_race import assigned_worker_slots


def run() -> None:
    slots = 16
    workers = 4
    covered = set()

    for worker_id in range(workers):
        assigned = assigned_worker_slots(slots, worker_id, workers)
        if not assigned:
            raise RuntimeError(f"worker {worker_id} has no assigned slots")
        for slot in assigned:
            if slot in covered:
                raise RuntimeError(f"slot {slot} assigned more than once")
            covered.add(slot)

    if covered != set(range(slots)):
        raise RuntimeError(f"slot coverage mismatch: got={sorted(covered)}")

    try:
        assigned_worker_slots(2, 2, 3)
    except ValueError:
        pass
    else:
        raise RuntimeError("expected ValueError when workers exceed slots")


def main() -> None:
    run()
    print("[OK] test_f2fs_retry_fsync_writeback_race_slot_plan passed", flush=True)


if __name__ == "__main__":
    main()
