#!/usr/bin/env python3
"""
parse_fio_logs.py  ·  v5  ·  bullet-proof

* 兼容文件名中的连字符
* BW/bw、IOPS 大小写不敏感
* IOPS 支持 k / m 后缀并换算为「次/秒」
* clat 同时支持 usec / msec,无则写 None
* CPU 行只要在同一行里出现 usr= 和 sys= 就能抓到
"""

import re, csv, math
from pathlib import Path

RESULTS_DIR = "./fio_results"
OUTPUT_CSV  = "./results.csv"

# ---------- 小工具 ---------- #

def _parse_with_suffix(value: str) -> float:
    """把 '212k' / '15.3m' / '987' 统一转成 float"""
    if value.lower().endswith('k'):
        return float(value[:-1]) * 1_000
    if value.lower().endswith('m'):
        return float(value[:-1]) * 1_000_000
    return float(value)

# ---------- 解析单个 log ---------- #

def parse_fio_log(path: Path):
    txt    = path.read_text()
    fname  = path.name

    # 1) 文件名元数据 ----------------------------------------------------
    m = re.match(
    r"^(?P<kernel>[\w\-]+)_(?P<mem>\d+G)_mem_"
    r"(?P<mode>read|write|sync)_bs-(?P<bs>[\w\d]+)_(?P<ts>[\d\-]+)\.log$",
    fname)
    if not m:
        print(f"[skip] 文件名不匹配: {fname}")
        return None
    meta = m.groupdict()

    # 2) 性能指标 --------------------------------------------------------
    # BW  / IOPS
    bw_rg   = re.search(r'BW[=|:](\d+\.?\d*)[GM]iB/s', txt, re.I)
    iops_rg = re.search(r'IOPS=(\d+\.?\d*[kKmM]?)', txt, re.I)

    # clat 行（可选）
    clat_rg = re.search(r'clat \((usec|msec)\):.*?avg= *([\d.]+)', txt)
    p99_rg  = re.search(r'99\.00th=\[\s*([\d.]+)]', txt)

    # CPU 一行
    cpu_rg  = re.search(
        r'^.*cpu.*usr=([\d.]+)%.*sys=([\d.]+)%', txt,
        flags=re.I | re.M)

    # -------- 校验 & 容错 ---------- #
    if not (bw_rg and iops_rg and cpu_rg):
        print(f"[warn] 必要字段缺失 → {fname}")
        return None  # 跳过这份

    # 3) 单位换算 --------------------------------------------------------
    bw = float(bw_rg.group(1))
    if "GiB/s" in bw_rg.group(0):
        bw *= 1024            # 统一成 MiB/s

    iops = _parse_with_suffix(iops_rg.group(1))  # 转成 “次/秒”

    # clat 可能缺
    clat_avg = None
    clat_p99 = None
    if clat_rg:
        clat_avg = float(clat_rg.group(2))
        if clat_rg.group(1) == 'msec':
            clat_avg *= 1000  # 统一微秒
    if p99_rg:
        clat_p99 = float(p99_rg.group(1))
        if "clat percentiles (msec)" in txt:
            clat_p99 *= 1000

    cpu_usr = float(cpu_rg.group(1))
    cpu_sys = float(cpu_rg.group(2))

    return {**meta,
            "bw_mib_s":       bw,
            "iops":           iops,
            "clat_avg_us":    clat_avg,
            "clat_p99_us":    clat_p99,
            "cpu_usr_pct":    cpu_usr,
            "cpu_sys_pct":    cpu_sys}

# ---------- 主程序 ---------- #

def main():
    rows = [ r for p in Path(RESULTS_DIR).glob("*.log")
                 if (r := parse_fio_log(p)) ]

    if not rows:
        print("Nothing parsed – 检查文件格式？")
        return

    with open(OUTPUT_CSV, "w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=rows[0].keys())
        w.writeheader(); w.writerows(rows)

    print(f"✓ Parsed {len(rows)} logs → {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
