import pandas as pd

# --- 配置 ---
# 输入文件名 (您原始的CSV文件)
input_filename = 'results_filt_kernel_iomap_v1_vanila_date_20250808.csv'
# 输出文件名 (处理后要保存的文件)
output_filename = 'results_without_writes.csv'
# --- 配置结束 ---

try:
    # 1. 使用pandas读取CSV文件到一个DataFrame中
    # DataFrame可以理解为一个功能强大的表格
    print(f"正在读取文件: {input_filename}...")
    df = pd.read_csv(input_filename)

    # 打印原始数据的行数
    original_rows = len(df)
    print(f"原始文件共有 {original_rows} 行数据。")

    # 2. 筛选数据
    # 这行代码是核心：
    # df['mode'] != 'write' 会生成一个布尔序列 (True/False)
    # 我们用这个序列来选择所有 'mode' 不等于 'write' 的行
    filtered_df = df[df['mode'] != 'write']

    # 打印筛选后的行数
    filtered_rows = len(filtered_df)
    print(f"筛选后剩下 {filtered_rows} 行数据。")

    # 3. 将筛选后的DataFrame保存为新的CSV文件
    # index=False 表示在保存文件时，不把DataFrame的行号（索引）写入文件
    filtered_df.to_csv(output_filename, index=False)

    print(f"\n处理完成！结果已成功保存到: {output_filename}")

except FileNotFoundError:
    print(f"错误：找不到文件 '{input_filename}'。请确保脚本和CSV文件在同一个目录下，或者提供正确的文件路径。")
except KeyError:
    print(f"错误：在文件中找不到名为 'mode' 的列。请检查您的CSV文件表头是否正确。")
except Exception as e:
    print(f"发生了一个未知错误: {e}")


