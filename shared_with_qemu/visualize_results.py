#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Visualize fio CSV results
- 自动识别运行环境 (Raspberry Pi 5 / QEMU VM)
- 将 kernel 字段拆分为:
      file_type:  normal | hole | com
      kernel_base: iomap | noiomap
- 对每种 file_type 各自生成 3 张图:
      bandwidth_<type>.png
      bandwidth_stability_<type>.png
      cpu_usage_<type>.png
"""
import argparse
from pathlib import Path

import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

# ---------- 命令行参数 ----------
ap = argparse.ArgumentParser(description="Visualize fio CSV results")
ap.add_argument("-i", "--input", required=True, help="输入 CSV 文件路径")
ap.add_argument("-o", "--outdir", default="plots", help="输出图片目录，默认 plots")
args = ap.parse_args()

INPUT_CSV = args.input
PLOTS_DIR = Path(args.outdir)
PLOTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------- 环境标签识别 ----------
ENV_TAGS = []
path_parts = [p.lower() for p in Path(INPUT_CSV).resolve().parts]
if any("pi" in p for p in path_parts):
    ENV_TAGS.append("Raspberry Pi 5")
if any("qemu" in p for p in path_parts):
    ENV_TAGS.append("QEMU VM")
env_suffix = f" – {' & '.join(ENV_TAGS)}" if ENV_TAGS else ""

# ---------- 读取与轻量清洗 ----------
df = pd.read_csv(INPUT_CSV, low_memory=False)

# 1) 删掉夹在中间的“重复表头”行
if "kernel" in df.columns:
    df = df[df["kernel"] != "kernel"].copy()

# 2) 数值列转为 float
num_cols = [
    "bw_mib_s", "iops", "clat_avg_us", "clat_p99_us",
    "cpu_usr_pct", "cpu_sys_pct"
]
for c in num_cols:
    if c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

# ---------- 衍生列：file_type / kernel_base ----------
def split_kernel(k: str):
    k = k.strip()
    if k.endswith("_hole"):
        return "hole", k.replace("_hole", "")
    elif k.endswith("_com"):
        return "com", k.replace("_com", "")
    return "normal", k

df[["file_type", "kernel_base"]] = df["kernel"].apply(
    lambda k: pd.Series(split_kernel(k))
)
mode_order = ["read", "write"]
if "mode" in df.columns:
    df["mode"] = pd.Categorical(df["mode"],
                                categories=mode_order, ordered=True)
# ---------- 分类顺序 ----------
bs_order = ["4k", "128k", "1M", "10M", "100M", "1G"]
if "bs" in df.columns:
    df["bs"] = pd.Categorical(df["bs"], categories=bs_order, ordered=True)

file_type_order = ["normal", "hole", "com"]
file_type_label = {
    "normal": "normal file",
    "hole": "sparse file (hole)",
    "com": "compressed file"
}

# 固定颜色：iomap = 蓝, noiomap = 橙
PALETTE = {"iomap": "#4c72b0", "noiomap": "#dd8452"}

# ---------- 循环生成各类文件的图 ----------
for ft in file_type_order:
    sub = df[df["file_type"] == ft].copy()
    if sub.empty:
        continue  # 该类型没数据

    lbl = file_type_label[ft]

    # 1. 带宽柱状图 ---------------------------------------------------------
    g = sns.catplot(
        data=sub,
        kind="bar",
        x="bs", y="bw_mib_s",
        hue="kernel_base",
        col="mem", row="mode",
        palette=PALETTE,
        height=4, aspect=1.25,
        sharey=False
    )
    g.set_axis_labels("Block Size", "Bandwidth (MiB/s)")
    g.fig.suptitle(f"Bandwidth comparison – {lbl}{env_suffix}",
                   fontsize=16, y=1.02)
    g.savefig(PLOTS_DIR / f"bandwidth_{ft}.png", bbox_inches="tight")
    plt.close(g.fig)

    # 2. 带宽箱线图 ---------------------------------------------------------
    g = sns.catplot(
        data=sub,
        kind="box",
        x="bs", y="bw_mib_s",
        hue="kernel_base",
        col="mem", row="mode",
        palette=PALETTE,
        height=4, aspect=1.25,
        sharey=False
    )
    g.set_axis_labels("Block Size", "Bandwidth (MiB/s)")
    g.fig.suptitle(f"Bandwidth stability (box, 10 runs) – {lbl}{env_suffix}",
                   fontsize=16, y=1.02)
    g.savefig(PLOTS_DIR / f"bandwidth_stability_{ft}.png", bbox_inches="tight")
    plt.close(g.fig)

    # 3. CPU usr/sys 并列柱 -------------------------------------------------
    cpu_long = (
        sub.groupby(["kernel_base", "mem", "mode", "bs"])
           [["cpu_usr_pct", "cpu_sys_pct"]]
           .mean()
           .reset_index()
           .melt(
               id_vars=["kernel_base", "mem", "mode", "bs"],
               value_vars=["cpu_usr_pct", "cpu_sys_pct"],
               var_name="cpu_type", value_name="pct"
           )
    )
    g = sns.catplot(
        data=cpu_long,
        kind="bar",
        x="bs", y="pct",
        hue="cpu_type",
        col="kernel_base",  # 左右分栏：iomap / noiomap
        row="mem",
        palette="pastel",
        height=4, aspect=1.25
    )
    g.set_axis_labels("Block Size", "CPU usage (%)")
    g.fig.suptitle(f"Average CPU breakdown – {lbl}{env_suffix}",
                   fontsize=16, y=1.02)
    g.savefig(PLOTS_DIR / f"cpu_usage_{ft}.png", bbox_inches="tight")
    plt.close(g.fig)

print("✅ Done!  图表已输出至:", PLOTS_DIR.resolve())
