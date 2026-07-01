#!/usr/bin/env python3
import json
import os
import shutil
import sys
import tempfile

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.artifact_pressure import ArtifactPressureConfig, _save_exception_evidence


# === CONFIG ===
WORKDIR = tempfile.mkdtemp(prefix="test_artifact_exception_evidence.")
EVIDENCE_DIR = os.path.join(WORKDIR, "failures")


def _cfg() -> ArtifactPressureConfig:
    return ArtifactPressureConfig(
        groups=1,
        app_count=1,
        artifacts_per_app=1,
        artifact_min_bytes=4096,
        artifact_max_bytes=4096,
        prefill_percent=0,
        prefill_file_bytes=4096,
        churn_files_per_round=0,
        churn_file_bytes=4096,
        churn_keep_fraction=0.0,
        churn_interval_s=1.0,
        memory_pressure_bytes=0,
        gc_interval_s=1.0,
        sync_every_groups=0,
        verify_every_groups=0,
        seed=1,
        evidence_dir=EVIDENCE_DIR,
        save_expected_file=False,
        keep_success_files=True,
    )


def run() -> None:
    tmp_path = os.path.join(WORKDIR, ".artifact.tmp")
    final_path = os.path.join(WORKDIR, "artifact.bin")
    backup_path = os.path.join(WORKDIR, "artifact.backup")
    for path, data in (
        (tmp_path, b"tmp-sample"),
        (final_path, b"final-sample"),
        (backup_path, b"backup-sample"),
    ):
        with open(path, "wb") as fp:
            fp.write(data)

    label = "group_000001.app_0000.artifact_00.write_exception"
    _save_exception_evidence(
        label,
        _cfg(),
        BlockingIOError(11, "Resource temporarily unavailable"),
        {"tmp": tmp_path, "final": final_path, "backup": backup_path},
    )

    case_dir = os.path.join(EVIDENCE_DIR, label)
    expected = ["exception_meta.json", "tmp.bin", "final.bin", "backup.bin"]
    missing = [name for name in expected if not os.path.exists(os.path.join(case_dir, name))]
    if missing:
        raise RuntimeError(f"missing exception evidence files: {missing}")

    with open(os.path.join(case_dir, "exception_meta.json"), "r", encoding="utf-8") as fp:
        meta = json.load(fp)
    if meta.get("exception_type") != "BlockingIOError":
        raise RuntimeError(f"unexpected exception type: {meta.get('exception_type')}")
    if meta.get("paths", {}).get("tmp") != tmp_path:
        raise RuntimeError("tmp path not recorded")


def cleanup() -> None:
    shutil.rmtree(WORKDIR, ignore_errors=True)


def main() -> None:
    try:
        run()
    finally:
        cleanup()
    print("[OK] test_artifact_exception_evidence passed", flush=True)


if __name__ == "__main__":
    main()
