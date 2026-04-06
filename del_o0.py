#!/usr/bin/env python3
# kdbg_o0.py: add/clean function-level O0 optimize attribute for kernel debugging
#
# Usage:
#   ./tools/kdbg_o0.py add <file.c> <func1> [func2 ...]
#   ./tools/kdbg_o0.py clean            # clean O0 attributes in git-modified files
#   ./tools/kdbg_o0.py clean --staged   # clean O0 attributes in staged files
#   ./tools/kdbg_o0.py clean --all      # clean O0 attributes under current dir (c/h only)
#
# Notes:
# - Adds: append attribute right after the function parameter ')' of the definition.
# - Clean: removes optimize("O0") / __optimize__("O0") / optnone lines or inline attributes.

import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import List, Tuple

ATTR_ADD = ' __attribute__((optimize("O0")))'

# remove patterns (line form + inline form)
ATTR_INLINE_PATTERNS = [
    r'\s*__attribute__\s*\(\(\s*optimize\s*\(\s*"O0"\s*\)\s*\)\)\s*',
    r'\s*__attribute__\s*\(\(\s*__optimize__\s*\(\s*"O0"\s*\)\s*\)\)\s*',
    r'\s*__attribute__\s*\(\(\s*optnone\s*\)\)\s*',  # clang-style
]

# whole-line attribute
ATTR_LINE_PATTERNS = [
    r'^\s*__attribute__\s*\(\(\s*optimize\s*\(\s*"O0"\s*\)\s*\)\)\s*;\s*$',
    r'^\s*__attribute__\s*\(\(\s*optimize\s*\(\s*"O0"\s*\)\s*\)\)\s*$',
    r'^\s*__attribute__\s*\(\(\s*__optimize__\s*\(\s*"O0"\s*\)\s*\)\)\s*$',
    r'^\s*__attribute__\s*\(\(\s*optnone\s*\)\)\s*$',
]

def run_git(args: List[str]) -> Tuple[int, str]:
    try:
        p = subprocess.run(["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return p.returncode, p.stdout
    except FileNotFoundError:
        return 1, ""

def list_git_files(staged: bool) -> List[str]:
    cmd = ["diff", "--name-only"]
    if staged:
        cmd.insert(1, "--cached")
    rc, out = run_git(cmd)
    if rc != 0:
        return []
    files = [x.strip() for x in out.splitlines() if x.strip()]
    return files

def is_c_like(path: str) -> bool:
    return path.endswith((".c", ".h"))

def clean_text(text: str) -> str:
    # remove whole-line attribute
    for lp in ATTR_LINE_PATTERNS:
        text = re.sub(lp + r'\n', '', text, flags=re.M)
    # remove inline occurrences
    for ip in ATTR_INLINE_PATTERNS:
        text = re.sub(ip, ' ', text)
    # fix spacing around ') {' etc minimally
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'  +', ' ', text)
    return text

def find_func_def_close_paren(text: str, func: str) -> List[int]:
    """Return indices (in text) right after the closing ')' of a function definition of `func`."""
    # match "func(" not preceded by '.' or '->' (avoid member calls)
    pat = re.compile(r'\b' + re.escape(func) + r'\b\s*\(')
    results = []

    for m in pat.finditer(text):
        before2 = text[max(0, m.start()-2):m.start()]
        if before2 in ("->", ". "):  # crude guard
            continue
        if text[max(0, m.start()-3):m.start()] == "->.":
            continue

        # find matching close paren for the '(' we matched
        i = m.end() - 1
        depth = 0
        end = None
        while i < len(text):
            c = text[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        if end is None:
            continue

        # determine if it's a definition: scan forward until '{' or ';'
        j = end + 1
        def_found = False
        while j < len(text):
            # skip whitespace
            if text[j].isspace():
                j += 1
                continue
            # skip C comments
            if text.startswith("/*", j):
                k = text.find("*/", j+2)
                if k == -1:
                    break
                j = k + 2
                continue
            if text.startswith("//", j):
                k = text.find("\n", j)
                if k == -1:
                    break
                j = k + 1
                continue

            if text[j] == '{':
                def_found = True
                break
            if text[j] == ';':
                def_found = False
                break

            # skip identifiers/macros and their optional (...) args, e.g. __releases(lock)
            if re.match(r'[A-Za-z_]', text[j]):
                k = j + 1
                while k < len(text) and re.match(r'[A-Za-z0-9_]', text[k]):
                    k += 1
                # skip optional whitespace
                while k < len(text) and text[k].isspace():
                    k += 1
                # if macro has (...) args, skip balanced parens
                if k < len(text) and text[k] == '(':
                    depth2 = 0
                    while k < len(text):
                        if text[k] == '(':
                            depth2 += 1
                        elif text[k] == ')':
                            depth2 -= 1
                            if depth2 == 0:
                                k += 1
                                break
                        k += 1
                j = k
                continue

            # unknown token, just move on
            j += 1

        if not def_found:
            continue

        # already has O0/optnone attribute nearby?
        lookahead = text[end+1: min(len(text), end+200)]
        if re.search(r'__attribute__\s*\(\(\s*(optimize|__optimize__)\s*\(\s*"O0"\s*\)', lookahead) or \
           re.search(r'__attribute__\s*\(\(\s*optnone\s*\)\)', lookahead):
            continue

        results.append(end + 1)  # insertion point right after ')'
    return results

def apply_add(file_path: str, funcs: List[str]) -> bool:
    p = Path(file_path)
    data = p.read_text(encoding="utf-8", errors="ignore")
    insert_points = []
    for f in funcs:
        insert_points.extend(find_func_def_close_paren(data, f))
    if not insert_points:
        return False

    # apply from back to front so indices stay valid
    insert_points = sorted(set(insert_points), reverse=True)
    for idx in insert_points:
        data = data[:idx] + ATTR_ADD + data[idx:]

    p.write_text(data, encoding="utf-8")
    return True

def apply_clean_on_files(files: List[str]) -> List[str]:
    changed = []
    for fp in files:
        if not is_c_like(fp):
            continue
        p = Path(fp)
        if not p.exists():
            continue
        old = p.read_text(encoding="utf-8", errors="ignore")
        new = clean_text(old)
        if new != old:
            p.write_text(new, encoding="utf-8")
            changed.append(fp)
    return changed

def find_all_c_like(root: str = ".") -> List[str]:
    out = []
    for dirpath, _, filenames in os.walk(root):
        # skip common huge dirs if you want; keep simple here
        for fn in filenames:
            if fn.endswith((".c", ".h")):
                out.append(str(Path(dirpath) / fn))
    return out

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_add = sub.add_parser("add", help="add optimize O0 attribute to specific function definitions")
    ap_add.add_argument("file", help="path to .c/.h")
    ap_add.add_argument("funcs", nargs="+", help="function names")

    ap_clean = sub.add_parser("clean", help="remove optimize O0/optnone attributes")
    ap_clean.add_argument("--staged", action="store_true", help="clean staged files instead of working tree modified files")
    ap_clean.add_argument("--all", action="store_true", help="clean all .c/.h under current directory")

    args = ap.parse_args()

    if args.cmd == "add":
        ok = apply_add(args.file, args.funcs)
        if not ok:
            print(f"[WARN] no function definition matched in {args.file}: {', '.join(args.funcs)}")
        else:
            print(f"[OK] added O0 attribute in {args.file}")
        return

    if args.cmd == "clean":
        if args.all:
            files = find_all_c_like(".")
        else:
            files = list_git_files(staged=args.staged)
            if not files:
                print("[INFO] no git-modified files found (or not a git repo). Try: clean --all")
                return
        changed = apply_clean_on_files(files)
        if changed:
            print("[OK] cleaned O0 attributes in:")
            for f in changed:
                print("  -", f)
        else:
            print("[INFO] nothing to clean.")
        return

if __name__ == "__main__":
    main()
