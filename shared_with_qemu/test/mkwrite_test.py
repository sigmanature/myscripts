#!/usr/bin/env python3
import argparse
import os
import sys
from dataclasses import dataclass

from rw_test import build_builtin_mmap_cases, run_builtin_mmap_case, stat_inode

TRACEFS = "/sys/kernel/tracing"
EVENT_ENABLE = f"{TRACEFS}/events/f2fs/f2fs_vm_page_mkwrite/enable"
TRACE_FILE = f"{TRACEFS}/trace"
TRACE_MARKER = f"{TRACEFS}/trace_marker"


def is_root() -> bool:
    return os.geteuid() == 0


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_text(path: str, s: str) -> None:
    with open(path, "w", encoding="utf-8", errors="ignore") as f:
        f.write(s)


def append_marker(msg: str) -> None:
    if os.path.exists(TRACE_MARKER):
        write_text(TRACE_MARKER, msg + "\n")


def enable_tracepoint(enable: bool) -> None:
    if not os.path.exists(EVENT_ENABLE):
        raise RuntimeError(f"tracepoint not found: {EVENT_ENABLE} (check your kernel config / trace events)")
    write_text(EVENT_ENABLE, "1\n" if enable else "0\n")


def clear_trace() -> None:
    write_text(TRACE_FILE, "")


def count_events_between_markers(trace: str, start: str, end: str) -> int:
    lines = trace.splitlines()
    s_idx = None
    e_idx = None
    for i, line in enumerate(lines):
        if start in line:
            s_idx = i
        if end in line and s_idx is not None and i > s_idx:
            e_idx = i
            break
    if s_idx is None or e_idx is None:
        return sum(1 for line in lines if "f2fs_vm_page_mkwrite" in line)
    seg = lines[s_idx:e_idx]
    return sum(1 for line in seg if "f2fs_vm_page_mkwrite" in line)


@dataclass
class TestResult:
    name: str
    ok: bool
    detail: str


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="f2fs mount directory, e.g. /mnt/f2fs")
    ap.add_argument("--no-trace", action="store_true", help="run without tracefs verification")
    ap.add_argument("--only", action="append", default=[], help="run only selected test(s)")
    ap.add_argument("--only-prefix", action="append", default=[], help="run tests whose names start with prefix")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"ERROR: not a directory: {args.dir}", file=sys.stderr)
        return 2

    if not is_root():
        print("ERROR: run as root (needed for tracefs + drop_caches)", file=sys.stderr)
        return 2

    if not args.no_trace:
        enable_tracepoint(True)
        clear_trace()

    tests = build_builtin_mmap_cases(include_wp_subpage=False)
    if args.only:
        selected = set(args.only)
        tests = [case for case in tests if case.name in selected]
        missing = sorted(selected - {case.name for case in tests})
        if missing:
            raise SystemExit(f"unknown --only test(s): {', '.join(missing)}")

    if args.only_prefix:
        prefixes = tuple(args.only_prefix)
        tests = [case for case in tests if case.name.startswith(prefixes)]
        if not tests:
            raise SystemExit(f"no tests matched --only-prefix: {', '.join(args.only_prefix)}")

    if args.only or args.only_prefix:
        print(f"RUNNER selected tests={','.join(case.name for case in tests)}", flush=True)

    results: list[TestResult] = []
    for case in tests:
        path = os.path.join(args.dir, case.file_name)
        print(f"RUNNER begin test={case.name}", flush=True)

        if not args.no_trace:
            start = f"=== {case.name} START ==="
            end = f"=== {case.name} END ==="
            append_marker(start)
            run_builtin_mmap_case(case, path)
            append_marker(end)

            trace = read_text(TRACE_FILE)
            ev = count_events_between_markers(trace, start, end)
            inode = stat_inode(path) if os.path.exists(path) else -1
            if case.expected_mkwrite is not None and ev != case.expected_mkwrite:
                results.append(
                    TestResult(
                        case.name,
                        False,
                        f"{path} inode={inode}: trace mkwrite events={ev}, expected={case.expected_mkwrite}",
                    )
                )
            else:
                results.append(
                    TestResult(
                        case.name,
                        True,
                        f"{path} inode={inode}: trace mkwrite events={ev}",
                    )
                )
        else:
            run_builtin_mmap_case(case, path)
            inode = stat_inode(path) if os.path.exists(path) else -1
            results.append(TestResult(case.name, True, f"{path} inode={inode}: (no trace mode)"))

    if not args.no_trace:
        enable_tracepoint(False)

    ok_all = True
    print("\n=== SUMMARY ===")
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status}  {result.name}: {result.detail}")
        ok_all = ok_all and result.ok
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
