# QEMU CoW Multi-Instance Workflow

## Purpose

Describe the normalized contract for `qemu_start_ubuntu.sh` after aligning it with `qemu_start_ori.sh`.

This document exists so future automation can target a specific VM instance without guessing ports, sockets, or log paths.

## Entry point

`myscripts/qemu_start_ubuntu.sh`

## Supported commands

- `start <instance>`
- `stop <instance>`
- `status <instance>`
- `cleanup <instance>`

## Alignment with `qemu_start_ori.sh`

- Sources `../.vars.sh` and reuses `DEFAULT_KERDIR`, `DEFAULT_MEM`, `IMG_BASE`, and `SCRIPT`.
- Uses the same kernel append policy as `qemu_start_ori.sh`.
- Supports QGA and QMP sockets.
- Supports `--log` to keep guest console output in a deterministic file.
- Keeps QEMU in the foreground so wrappers can decide whether to background it.

## Multi-instance contract

Each instance writes `myscripts/vm_instances/<instance>/instance.env` with:

- `VM_SSH_PORT`
- `VM_HTTP_PORT`
- `VM_QGA_SOCK`
- `VM_QMP_SOCK`
- `VM_CONSOLE_LOG`
- `VM_PID_FILE`
- `VM_KERNEL_DIR`
- `VM_MEM`

Automation should prefer reading this file or `status <instance>` instead of inferring ports or socket names.

## Default naming rules

- Root overlay: `myscripts/vm_instances/<instance>/root.qcow2`
- F2FS overlay: `myscripts/vm_instances/<instance>/f2fs.qcow2`
- Shared dir copy: `myscripts/vm_instances/<instance>/shared_with_qemu`
- QGA socket: `/tmp/qga.<instance>.sock`
- QMP socket: `/tmp/qemu-qmp.<instance>.sock`

If the instance name ends with digits and no explicit ports are given:

- `ssh_port = 5022 + suffix - 1`
- `http_port = 5080 + suffix - 1`

Examples:

- `vm1` -> ssh `5022`, http `5080`
- `vm2` -> ssh `5023`, http `5081`

If the instance name has no numeric suffix, callers must provide explicit ports or `--port-offset`.

## Safe validation pattern

Use `--dry-run` first when adding a new instance:

```bash
bash myscripts/qemu_start_ubuntu.sh start vm2 --dry-run
```

This prepares overlays and metadata, prints the resolved QEMU command, but does not boot the VM.

## Skill integration notes

The remaining integration work is outside `myscripts`:

- `.agents/tools/qga_exec.py` should accept a per-instance socket path.
- `.agents/tools/vm_start_bg.sh` should accept launcher and instance parameters.
- `.agents/tools/vm_ssh.sh` / `vm_stop.sh` should load `instance.env` so an agent can target one VM deterministically.

Until those tool changes are applied, `qemu_start_ubuntu.sh status <instance>` is the source of truth for per-instance connection metadata.
