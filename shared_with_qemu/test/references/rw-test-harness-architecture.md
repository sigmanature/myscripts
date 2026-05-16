# RW Test Harness Architecture Notes

## Scope

This note explains why the current `rw_matrix.sh`, `rw_matrix_inline.sh`, and `rw_test.py` overlap, and what layering should replace the duplication.

## Current duplication hotspots

### 1. Matrix data is duplicated across wrappers

- `rw_matrix.sh` and `rw_matrix_inline.sh` both define the same baseline sizes, overwrite cases, append cases, and pattern config.
- The only real difference is target file class and which case groups are enabled.

### 2. Scenario execution flow is duplicated across wrappers

Both wrappers re-implement the same sequence:

1. prepare file with `truncate`
2. optionally fill baseline with `A`
3. perform overwrite or append
4. compute expected final size
5. cold-read verify the entire file

### 3. Python logic is duplicated inside shell

The wrappers embed Python for:

- `parse_bytes`
- repeat/filepos/counter pattern generation
- full-file overlay verification
- integer conversion for shell size strings

This overlaps with logic already present in `rw_test.py`.

### 4. Primitive helpers are split across multiple files

Reusable helpers currently exist in multiple places:

- `drop_caches`: `rw_test.py`, `mkwrite_test.py`, `mmap_wp_fault_test.py`, and shell wrappers
- file fill / sparse prep: `rw_test.py`, `mkwrite_test.py`, `mmap_wp_fault_test.py`
- mmap scenario logic: `mkwrite_test.py`, `mmap_wp_fault_test.py`
- one-off read/write demos: `rw_test.sh`, `write_test.py`

## Why shell embedded Python was reasonable earlier

At the time the matrix wrappers were written, shell still owned the test orchestration:

- root escalation
- mount and `fscrypt` checks
- directory selection
- matrix expansion
- file naming

But bash is weak at:

- byte-size parsing
- deterministic pattern generation
- full-file byte-accurate verification

`rw_test.py` also did not yet expose higher-level scenario APIs such as:

- prepare baseline
- append with expected post-state
- full-file overlay verify
- read-then-write sequences
- mmap case execution

So inline Python in shell was a pragmatic bridge: shell kept orchestration, Python handled byte math without requiring a fuller reusable library first.

## Why the current shape is now a refactor target

The original split stops scaling once new dimensions are added:

- file class: plain / inline / encrypted-non-inline
- write mode: overwrite / append / read-then-write / mmap-write
- alignment: aligned / unaligned
- baseline: existing content / hole / truncated
- verification: region verify / full-file verify / cold verify / mmap-specific assert
- stress: streaming / direct memory pressure / repeated loops / mixed read-write pressure

Every new axis currently expands shell duplication instead of extending a common provider.

## Recommended target layering

### `rw_test.py`: provider + CLI

`rw_test.py` should become the canonical provider of reusable primitives and case execution.

Recommended layers:

1. utility layer
   - `parse_bytes`
   - pattern generation
   - hexdump / mismatch reporting
   - `drop_caches`

2. file preparation layer
   - create parent
   - sparse truncate
   - fill token / fill zeros / fill known pattern
   - baseline constructors for existing / hole / truncated

3. operation layer
   - `read_region`
   - `write_region`
   - `append_region`
   - `overwrite_region`
   - `read_then_write_region`
   - mmap write helpers

4. verification layer
   - region verify
   - full-file overlay verify
   - size verify
   - tail-zero verify
   - optional cold-read verify

5. execution layer
   - case dataclass
   - scenario runner
   - loop / stress runner
   - summary reporting

### shell wrappers: thin environment wrappers

Shell should only keep responsibilities that are naturally shell-centric:

- sudo/root bootstrap
- mount and `fscrypt` validation
- unlock encrypted directory
- selecting target directories for plain / inline / encrypted-non-inline
- passing case filters or matrix selectors into Python

Shell should stop embedding Python verification logic once the provider is complete.

## Suggested case model

Use a declarative case object with fields like:

- `name`
- `file_class`: `plain`, `inline`, `enc_noinline`
- `baseline_kind`: `existing_a`, `hole`, `truncated`
- `baseline_size`
- `op_kind`: `overwrite`, `append`, `read_then_write`, `mmap_write`
- `offset`
- `size`
- `pattern_mode`
- `verify_kind`: `region`, `full_file`, `tail_zero`, `mmap_persist`
- `read_before_write`: boolean or read-span descriptor
- `stress`: loop count, chunk size, pattern generation mode, readback mode

## Missing scenarios to add

### Read-then-write

This is currently missing from the matrix and should be a first-class operation, not an ad hoc pre-step.

Variants:

- read target region, then overwrite same region
- read surrounding aligned window, then write subrange
- read entire file, then append
- cold-read, warm-read, then write

### mmap

Mmap cases should join the same harness as a scenario family:

- shared mmap single-page write
- same-folio multi-page write
- cross-folio boundary write
- tail-zeroing / truncate interaction
- optional trace-backed `page_mkwrite` assertions

### stress

Pressure should be configurable instead of hidden in one-off scripts:

- large `--pattern-gen direct` allocations
- repeated write/verify loops
- mixed pread + pwrite cycles
- append growth loops
- mmap touch loops

## Migration direction

1. Move duplicated verifier logic from shell into `rw_test.py`.
2. Move baseline preparation helpers into `rw_test.py`.
3. Represent matrix cases as data rather than shell code fragments.
4. Keep shell as a wrapper for environment setup only.
5. Converge standalone mmap scripts onto the same provider layer, keeping trace-specific assertions as optional extras.

## Practical boundary decision

If `mkwrite_test.py` needs tracefs-specific assertions and special runner output, it can remain a separate front-end script.
But its shared helpers should still come from the same provider layer used by `rw_test.py`.

## Current convergence status

The current codebase now follows this converged shape:

- `rw_test.py` owns generic buffered-I/O helpers, baseline prep, overlay verification, and matrix execution.
- `rw_test.py` also owns builtin mmap providers plus `mmap-case` and `mmap-matrix` subcommands.
- `rw_matrix.sh` and `rw_matrix_inline.sh` are thin wrappers that only do environment setup and call into `rw_test.py`.
- `mkwrite_test.py` and `mmap_wp_fault_test.py` are now thin mmap-specific front-ends layered on top of the builtin mmap providers in `rw_test.py`.

That means low-level duplication across buffered-I/O and mmap families is now largely removed.
Future work should focus on adding new case families and richer mmap data modeling, not on re-solving file utility duplication again.
