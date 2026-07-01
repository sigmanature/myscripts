#!/usr/bin/env python3
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_ROOT = os.path.dirname(THIS_DIR)
if TEST_ROOT not in sys.path:
    sys.path.insert(0, TEST_ROOT)

from utils.artifact_pressure import (
    ArtifactPressureConfig,
    ArtifactRecipe,
    ArtifactState,
    advance_verity_fsync_batch,
    plan_fsync_batch,
    PatternConfig,
    should_enable_verity,
)


def _cfg() -> ArtifactPressureConfig:
    return ArtifactPressureConfig(
        groups=4,
        app_count=3,
        artifacts_per_app=2,
        artifact_min_bytes=4096,
        artifact_max_bytes=8192,
        prefill_percent=0,
        prefill_file_bytes=4096,
        churn_files_per_round=0,
        churn_file_bytes=4096,
        churn_keep_fraction=0.0,
        churn_interval_s=0.0,
        memory_pressure_bytes=0,
        gc_interval_s=0.0,
        sync_every_groups=0,
        verify_every_groups=0,
        seed=123,
        evidence_dir="/tmp/evidence",
        save_expected_file=False,
        keep_success_files=True,
        fsync_worker_count=3,
        fsync_batch_width=4,
        verity_ratio_percent=100,
        verity_min_fsync_passes=2,
    )


def _recipe() -> ArtifactRecipe:
    return ArtifactRecipe(
        expected_size=4096,
        baseline=PatternConfig(
            mode="mod251",
            token=b"",
            seed=1,
            chunk_size=4096,
            pattern_gen="stream",
            readback="pread",
        ),
        overlays=(),
    )


def run() -> None:
    cfg = _cfg()
    if cfg.fsync_worker_count != 3:
        raise RuntimeError(f"unexpected fsync worker count: {cfg.fsync_worker_count}")
    if cfg.verity_min_fsync_passes != 2:
        raise RuntimeError(f"unexpected verity sync threshold: {cfg.verity_min_fsync_passes}")

    focus = "/tmp/app_0001/artifact_00.bin"
    batch = plan_fsync_batch(
        focus_path=focus,
        candidate_paths=[
            focus,
            "/tmp/app_0001/artifact_01.bin",
            "/tmp/app_0002/artifact_00.bin",
            "/tmp/app_0002/artifact_00.bin",
            "/tmp/app_0003/artifact_00.bin",
        ],
        worker_count=cfg.fsync_worker_count,
        batch_width=cfg.fsync_batch_width,
        round_id=7,
    )
    if batch[0] != focus:
        raise RuntimeError(f"focus path not first: {batch}")
    if len(batch) != 3:
        raise RuntimeError(f"unexpected batch size: {batch}")
    if len(set(batch)) != len(batch):
        raise RuntimeError(f"batch contains duplicates: {batch}")

    if should_enable_verity(
        verity_candidate=True,
        sync_passes=1,
        verity_min_fsync_passes=2,
    ):
        raise RuntimeError("verity should not enable before sync threshold")

    if not should_enable_verity(
        verity_candidate=True,
        sync_passes=2,
        verity_min_fsync_passes=2,
    ):
        raise RuntimeError("verity should enable at threshold with 100% ratio")

    if should_enable_verity(
        verity_candidate=False,
        sync_passes=2,
        verity_min_fsync_passes=2,
    ):
        raise RuntimeError("verity should stay disabled with 0% ratio")

    secondary = "/tmp/app_0002/artifact_00.V.bin"
    state = {
        focus: ArtifactState(recipe=_recipe(), sync_passes=1, verity_candidate=True),
        secondary: ArtifactState(recipe=_recipe(), sync_passes=1, verity_candidate=True),
    }
    eligible = advance_verity_fsync_batch(
        state=state,
        paths=(focus, secondary),
        verity_min_fsync_passes=2,
    )
    if eligible != (focus, secondary):
        raise RuntimeError(f"expected both files to become verity-eligible: {eligible}")
    if state[focus].sync_passes != 2 or state[secondary].sync_passes != 2:
        raise RuntimeError(f"unexpected sync pass accounting: {state}")
    state[focus].verity_enabled = True
    eligible = advance_verity_fsync_batch(
        state=state,
        paths=(focus,),
        verity_min_fsync_passes=2,
    )
    if eligible:
        raise RuntimeError(f"already-enabled file should not be returned again: {eligible}")


def main() -> None:
    run()
    print("[OK] test_artifact_pressure_verity_fsync_plan passed", flush=True)


if __name__ == "__main__":
    main()
