# GC Long-Run Target Notes

## Scope

`f2fs_gc_long_rw.py` is for long-running GC pressure that reuses `rw_test.py`
matrix verification instead of re-implementing data checks in shell.

## Target model

- `plain`
  - auto-provisioned by the runner as a small loop-backed F2FS image
  - the script formats it with `mkfs.f2fs -s 1`
  - the script mounts it and keeps free space low with background churn files

- `inline`
  - must be a non-fs-level target
  - do not route this through `rw_matrix_inline.sh`, because that wrapper assumes
    `fscrypt` directory encryption on an `inlinecrypt` mount
  - acceptable inputs are:
    - `--inline-root <mounted f2fs root>`
    - `--inline-device <block device>` when the caller already prepared a
      block-level inline-encrypted device and wants the runner to `mkfs` and mount it

## Why the existing inline wrapper is wrong for this case

`rw_matrix_inline.sh` validates:

- `inlinecrypt` mount options
- an `fscrypt`-encrypted directory
- `E` attributes on the target directory

That is correct for fs-level encryption coverage, but it is the wrong abstraction
when the user wants block-level inline behavior without fs-level encryption.

## Runner strategy

- keep the small filesystem nearly full by creating and deleting garbage files
- trigger GC continuously with `f2fs_io gc_urgent` or `f2fs_io gc`
- execute structured matrix cases from `rw_test.py`
- use full-file verification so the run aborts immediately on the first mismatch

## Validation note

This runner is root-only and environment-sensitive because it depends on:

- `mkfs.f2fs`
- `mount` / `umount`
- `losetup`
- `drop_caches`
- `f2fs_io gc_urgent` or `f2fs_io gc`
