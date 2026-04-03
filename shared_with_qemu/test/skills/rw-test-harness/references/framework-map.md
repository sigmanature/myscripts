# Framework Map

## Workspace

- Root for this framework:
  - `/home/nzzhao/learn_os/myscripts/shared_with_qemu/test`

## Core Files

- `rw_test.py`
  - canonical provider for buffered-I/O and mmap test execution
  - owns:
    - byte parsing
    - pattern generation
    - baseline preparation
    - full-file overlay verification
    - builtin buffered matrix
    - builtin mmap case registry
    - `case`, `matrix`, `mmap-case`, `mmap-matrix` subcommands

- `rw_matrix.sh`
  - plain + encrypted buffered-I/O wrapper

- `rw_matrix_inline.sh`
  - inlinecrypt-oriented buffered-I/O wrapper

- `f2fs_gc_long_rw.py`
  - long-running GC + writeback pressure runner that reuses `run_matrix_case()`
  - auto-provisions a small plain F2FS image
  - accepts a non-fs-level inline target as an existing root or block device

- `mkwrite_test.py`
  - thin trace-aware frontend over builtin mmap cases

- `mmap_wp_fault_test.py`
  - thin wp-fault-focused frontend over builtin mmap case

## Buffered-I/O Extension Points

When adding buffered cases, inspect:

- `MatrixCase`
- `build_builtin_matrix_cases()`
- `run_matrix_case()`
- `prepare_baseline()`
- `verify_full_overlay()`

Prefer:

- data-driven case registration
- provider helper reuse
- read-then-write as structured case configuration
- long-running GC runners that call back into `run_matrix_case()` instead of re-implementing byte verification

Avoid:

- inline Python inside shell wrappers
- duplicate parse/verify helpers in shell
- one-off scripts for a case that fits the matrix model

## mmap Extension Points

When adding mmap cases, inspect:

- `MmapBuiltinCase`
- `build_builtin_mmap_cases()`
- `run_builtin_mmap_case()`
- `mmap_shared_write()`
- `fill_largefolio_pattern()`

Prefer:

- add case implementation to `rw_test.py`
- expose it through `mmap-case` and `mmap-matrix`
- keep `mkwrite_test.py` and `mmap_wp_fault_test.py` thin

## Common Validation Commands

```bash
python3 rw_test.py case --name smoke_case --file /tmp/rw_case_smoke.bin --baseline-kind existing_a --baseline-len 8k --write-style overwrite --offset 0 --size 4k --read-before-write --verify-mode cache --pattern-mode filepos --token PyWrtDta --chunk 4k --no-drop-after-prepare --cleanup-file
```

```bash
python3 rw_test.py matrix --target smoke=/tmp --baseline-kind existing_a --write-style overwrite --case-filter o0_aligned --no-read-then-write --verify-mode cache --chunk 4k --no-drop-after-prepare
```

```bash
python3 rw_test.py mmap-case --name wp_subpage_read_then_write --file /tmp/rw_mmap_case_smoke.bin --do-readahead --cleanup-file
```

```bash
python3 rw_test.py mmap-matrix --target smoke=/tmp --case-filter wp_subpage --do-readahead
```

```bash
python3 f2fs_gc_long_rw.py --allow-plain-only --runtime-sec 5 --case-filter o0_aligned
```

## Environment-Specific Runs

Use wrappers only when the user needs the real target environment:

- `./rw_matrix.sh`
- `./rw_matrix_inline.sh`
- `python3 f2fs_gc_long_rw.py --inline-root <mounted-inline-f2fs-root>`
- `python3 mkwrite_test.py --dir <mount>`

These depend on mount state, root, inlinecrypt, fscrypt, or tracefs and should not be the first validation step unless the task explicitly requires environment coverage.
