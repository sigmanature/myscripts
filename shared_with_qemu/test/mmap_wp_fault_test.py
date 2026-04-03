#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Thin frontend for the builtin wp-fault mmap case in rw_test.py."""

import argparse
import sys

from rw_test import find_builtin_mmap_case, run_builtin_mmap_case


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="file path on the target filesystem")
    ap.add_argument("--drop-caches", action="store_true", help="attempt drop_caches before the case")
    ap.add_argument("--do-readahead", action="store_true", help="attempt readahead syscall before mmap write")
    ap.add_argument("--pdb", action="store_true", help="enter pdb before the mmap write")
    args = ap.parse_args()

    case = find_builtin_mmap_case("wp_subpage_read_then_write", include_wp_subpage=True)
    try:
        run_builtin_mmap_case(
            case,
            args.path,
            drop_caches_override=args.drop_caches if args.drop_caches else None,
            do_readahead=args.do_readahead,
            pdb_before_write=args.pdb,
        )
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
