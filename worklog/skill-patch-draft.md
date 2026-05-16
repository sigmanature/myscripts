# Skill Patch Draft

## Candidate updates for qemu automation workflow

- Add instance-aware QEMU launcher metadata so skills can target one VM deterministically.
- Generalize guest control helpers to accept per-instance QGA socket and SSH port.
- Preserve existing single-instance defaults so current `qemu_start_ori.sh` flow remains unchanged.

Outcome:
- Implemented: user-level skill (`~/.agents/skills/f2fs-qemu-agent-pipeline`) updated for multi-instance parallel mode.
- Implemented: `myscripts/qemu_start_ubuntu.sh` now emits per-instance metadata and avoids fixed `-s` gdb port conflicts.

## Proposed tool changes outside `myscripts`

- `learn_os/.agents/tools/qga_exec.py`
  - Add `--sock <path>` and `QGA_SOCK` env fallback.
  - Keep `/tmp/qga.sock` as the default for current single-VM flow.
- `learn_os/.agents/tools/vm_start_bg.sh`
  - Keep no-arg behavior for `qemu_start_ori.sh`.
  - Add optional `--launcher {ori,ubuntu-cow}` and `--instance <name>`.
  - For `ubuntu-cow`, call `myscripts/qemu_start_ubuntu.sh start <instance> --log ...`.
  - Print `instance_name`, `instance_env`, `console_log`, `qga_sock`, `ssh_port`.
- `learn_os/.agents/tools/vm_ssh.sh`
  - New wrapper.
  - Load `myscripts/vm_instances/<instance>/instance.env` when `--instance` is given.
  - Fall back to existing env-driven `127.0.0.1:5022 root/1`.
- `learn_os/.agents/tools/vm_stop.sh`
  - New wrapper.
  - Support `--instance <name>` and delegate to `qemu_start_ubuntu.sh stop <instance>`.

## Skill doc deltas

- Mention `qemu_start_ubuntu.sh` as the CoW multi-instance launcher.
- Mention `instance.env` as the per-instance source of truth.
- Document that multi-instance QGA must use instance-specific sockets instead of fixed `/tmp/qga.sock`.

Status:
- Done: `learn_os/.agents/tools/qga_exec.py` supports `--sock` / `QGA_SOCK`.
- Done: user-level skill scripts support `--instance` (`scripts/vm_ssh.sh`, `scripts/vm_stop.sh`) and multi-instance start (`scripts/vm_start_bg.sh --launcher ubuntu-cow --instance vm2`).
- Deferred: adding repo-local `.agents/tools/vm_ssh.sh` and `.agents/tools/vm_stop.sh` wrappers, because the user requested user-level skill ownership first.
