# Case Authoring Notes

## Buffered-I/O Cases

Use the existing dimensions before inventing new structure:

- baseline kind
  - `existing_a`
  - `hole`
- write style
  - `overwrite`
  - `append`
- read-before-write
  - on/off
- size and offset alignment
  - aligned
  - unaligned

If the new case still fits those dimensions, add it to builtin matrix generation rather than writing a custom runner.

If the case introduces a genuinely new operation mode, extend:

- `MatrixCase`
- case generation
- `run_matrix_case()`

## mmap Cases

Add mmap cases when the behavior is fundamentally mmap-specific, for example:

- write-fault counting
- same-folio multi-page writes
- cross-folio writes
- truncate and tail-zeroing interactions
- wp-fault read-then-write paths

Keep the builtin case registry authoritative. A new mmap case should usually require:

1. a `MmapBuiltinCase` entry
2. a new branch in `run_builtin_mmap_case()`
3. smoke validation via `rw_test.py mmap-case`

## Thin Frontend Rule

Do not grow `mkwrite_test.py` or `mmap_wp_fault_test.py` into separate frameworks again.

They may keep:

- trace-specific CLI
- trace-specific result interpretation
- focused entrypoint names

They should not keep:

- duplicated test implementation
- duplicated low-level file helpers
- duplicated case registries
