#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

# 检查用户是否提供了命令行参数
if len(sys.argv) != 2:
    print("用法: python hex2dec.py <十六进制数>")
    print("例如: python hex2dec.py FF")
    sys.exit(1) # 退出脚本，并返回一个错误码

# 获取第一个命令行参数（sys.argv[0] 是脚本名本身）
hex_string = sys.argv[1]

try:
    # 使用 int() 函数进行转换，第二个参数 16 表示输入的是16进制
    decimal_value = int(hex_string, 16)
    print(f"十六进制 '{hex_string}' -> 十进制: {decimal_value}")
except ValueError:
    # 如果输入的字符串无效（例如 "FG"），int() 会抛出 ValueError
    print(f"错误: '{hex_string}' 不是一个有效的十六进制数。")
    sys.exit(1)
