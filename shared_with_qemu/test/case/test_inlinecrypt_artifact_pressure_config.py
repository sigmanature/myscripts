#!/usr/bin/env python3
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

import test_inlinecrypt_artifact_pressure as case_mod


def run() -> None:
    if tuple(case_mod.lm.mkfs_features or ()) != ("encrypt", "verity"):
        raise RuntimeError(
            f"inline artifact pressure mkfs features mismatch: {case_mod.lm.mkfs_features}"
        )


def main() -> None:
    run()
    print("[OK] test_inlinecrypt_artifact_pressure_config passed", flush=True)


if __name__ == "__main__":
    main()
