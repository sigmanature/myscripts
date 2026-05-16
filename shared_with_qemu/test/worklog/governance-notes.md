# Governance Notes

## 2026-04-01 rw test harness analysis

- Governing target: none yet, with likely promotion to `references/` after architecture is validated.
- Expected reuse likelihood: likely.
- Initial landing zone: `worklog/governance-notes.md`, candidate later to `references/rw-test-harness-notes.md`.

### Reusable findings

- `rw_matrix.sh` and `rw_matrix_inline.sh` duplicate the same test matrix data, case runner flow, and full-file verifier logic.
- Both shell wrappers embed Python snippets for logic that already exists in `rw_test.py`, especially byte parsing and expected-content generation.
- `rw_matrix.sh` contains repeated inline Python `parse_bytes()` fragments just to convert shell size strings into integers for verification.
- `rw_matrix*.sh` duplicate `drop_caches`, baseline fill, pattern write, and cold-read verify helpers; these are candidate provider functions for `rw_test.py`.
- `rw_test.py` already owns most primitive I/O behaviors (`parse_bytes`, pattern generation, `pwrite`, `pread`, cache drop, stream/direct verification) but does not yet expose higher-level scenario primitives such as baseline preparation, append/overwrite/read-then-write sequences, or full-file overlay verification.
- `mmap_wp_fault_test.py` and `mkwrite_test.py` carry separate mmap-oriented test logic and their own cache-drop/file-fill helpers; these look like candidates to converge under the same harness rather than remain standalone one-offs.

### Hypothesis on why shell embedded Python

- Shell needs matrix assembly and environment checks, but accurate byte parsing and content verification are awkward in bash.
- At the time these matrix scripts were written, `rw_test.py` exposed only low-level CLI operations (`read`/`write`/`verify`) instead of reusable scenario APIs, so the shell wrappers had to re-implement verifier logic inline to express matrix expectations.
- Inline Python was likely chosen as a compromise: keep orchestration in bash, but use Python for content math and byte-accurate verification without creating a larger Python harness first.

### Candidate promotion items

- Reference candidate: why matrix wrappers grew inline Python and when that becomes a refactor smell.
- Reference candidate: recommended layering for `rw_test.py` provider + shell wrappers.
- Script candidate only after refactor: a single matrix wrapper that selects file class and scenario via declarative cases.

### Triage status

- Promoted to `references/rw-test-harness-architecture.md`: duplication hotspots, why shell embedded Python, recommended layering, missing scenario families.
- Promoted to implementation: unified provider/wrapper split landed in `rw_test.py`, `rw_matrix.sh`, and `rw_matrix_inline.sh`.
- Deferred with reason: mmap convergence details, because the final ownership split between `rw_test.py` and `mkwrite_test.py` is still an architecture choice.

### Implementation closure

- Landed provider refactor in `rw_test.py`: added structured `case` and `matrix` runners, baseline preparation, full-file overlay verification, and builtin read-then-write matrix variants.
- Landed thin-wrapper refactor in `rw_matrix.sh` and `rw_matrix_inline.sh`: shell now keeps root/bootstrap, mount/fscrypt validation, directory wiring, and forwards matrix execution into `rw_test.py`.
- Landed full mmap provider integration in `rw_test.py`: added builtin mmap case registry plus `mmap-case` and `mmap-matrix` subcommands.
- Landed thin mmap frontends: `mkwrite_test.py` and `mmap_wp_fault_test.py` now delegate case logic to `rw_test.py` instead of embedding their own test implementations.
- Landed reusable skill packaging: created `rw-test-harness` skill draft under the repo and installed it to `/home/nzzhao/.agents/skills/rw-test-harness` with a live symlink at `/home/nzzhao/.codex/skills/rw-test-harness`.
- Remaining deferred item: mmap cases are now unified, but trace-aware expectations still live in the specialized `mkwrite_test.py` frontend rather than a generic trace runner module.

### Validated commands

- `python3 -m py_compile rw_test.py`
- `python3 rw_test.py case --name smoke_case --file /tmp/rw_case_smoke.bin --baseline-kind existing_a --baseline-len 8k --write-style overwrite --offset 0 --size 4k --read-before-write --verify-mode cache --pattern-mode filepos --token PyWrtDta --chunk 4k --no-drop-after-prepare --cleanup-file`
- `python3 rw_test.py matrix --target smoke=/tmp --baseline-kind existing_a --write-style overwrite --case-filter o0_aligned --no-read-then-write --verify-mode cache --chunk 4k --no-drop-after-prepare`
- `python3 rw_test.py mmap-case --name wp_subpage_read_then_write --file /tmp/rw_mmap_case_smoke.bin --do-readahead --cleanup-file`
- `python3 rw_test.py mmap-matrix --target smoke=/tmp --case-filter wp_subpage --do-readahead`
- `python3 mmap_wp_fault_test.py /tmp/mmap_wp_fault_smoke.bin`
- `python3 mmap_wp_fault_test.py /tmp/mmap_wp_fault_wrapper_smoke.bin --do-readahead`
- `python3 mkwrite_test.py --help`
- `python3 -m json.tool skills/rw-test-harness/evals/evals.json`
- `bash -n rw_matrix.sh`
- `bash -n rw_matrix_inline.sh`

### Open items

- Need to decide whether `mmap_wp_fault_test.py` and `mkwrite_test.py` become subcommands in `rw_test.py` or remain separate scenario modules imported by a common runner.
- Need to define the canonical scenario model: baseline kind, file class, operation pattern, alignment, verify mode, and stress profile.

## 2026-04-02 gc long-run pressure case

- Governing target: existing skill + script + reference.
- Expected reuse likelihood: likely.
- Initial landing zone:
  - `f2fs_gc_long_rw.py`
  - `references/gc-long-run-targets.md`
  - `skills/rw-test-harness/`

### Reusable findings

- The existing `rw_matrix_inline.sh` wrapper is tied to `fscrypt` directory encryption and therefore is the wrong entrypoint when the user wants a non-fs-level inline target.
- A long-running GC/writeback case can still reuse the current framework by orchestrating mount/churn/GC in a dedicated runner while delegating every data check to `run_matrix_case()`.
- A small loop-backed F2FS image plus low-free-space churn is a reusable way to make GC selection likely without baking device-specific assumptions into each test.

### Triage status

- Promoted to script: `f2fs_gc_long_rw.py`.
- Promoted to reference: `references/gc-long-run-targets.md`.
- Promoted to skill update: `skills/rw-test-harness/SKILL.md` and `skills/rw-test-harness/references/framework-map.md`.
- Discarded: trying to reuse `rw_matrix_inline.sh` for this scenario, because it would silently test the wrong abstraction.
