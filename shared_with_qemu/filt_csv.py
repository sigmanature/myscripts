# 导入 pandas 用于数据处理，argparse 用于接收命令行参数，os 用于处理文件路径
import pandas as pd
import argparse
import os

def filter_csv_advanced(input_file, target_kernels, target_dates):
    """
    使用 pandas 库，根据指定的 kernel 列表和日期列表进行筛选。

    Args:
        input_file (str): 输入的 CSV 文件路径。
        target_kernels (list): 要筛选的 kernel 名称列表。
        target_dates (list): 要筛选的日期前缀 (YYYYMMDD) 列表。
    """
    try:
        # 1. 读取 CSV 文件到 pandas DataFrame
        df = pd.read_csv(input_file)

        # 2. 应用复合筛选条件
        # 条件1: 'kernel' 列的值必须在 target_kernels 列表中。
        #        我们使用 .isin() 方法来实现。
        kernel_condition = df['kernel'].isin(target_kernels)

        # 条件2: 'ts' 列的日期前缀必须在 target_dates 列表中。
        #        - 首先，我们使用 .str[:8] 从 'ts' 列 (格式 YYYYMMDD-HHMMSS) 中提取前8个字符，即日期部分。
        #        - 然后，我们对提取出的日期系列使用 .isin() 方法。
        date_condition = df['ts'].str[:8].isin(target_dates)

        # 最终筛选: 使用 & (AND) 操作符，组合两个条件，要求它们必须同时满足。
        filtered_df = df[kernel_condition & date_condition]

        # 3. 检查筛选结果是否为空
        if filtered_df.empty:
            print(f"警告: 没有找到任何同时满足以下条件的记录：")
            print(f"  - Kernels: {target_kernels}")
            print(f"  - Dates:   {target_dates}")
            print("新文件将不会被创建。")
            return

        # 4. 生成一个描述性的、不重复的输出文件名
        # 将列表转换为用下划线连接的字符串，以便放入文件名
        kernels_str = "_".join(target_kernels)
        dates_str = "_".join(target_dates)
        base, ext = os.path.splitext(input_file)
        # 使用 f-string 创建一个清晰的文件名，例如: data_filt_kernel_iomap_v1_date_20250811_20250630.csv
        output_file = f"{base}_filt_kernel_{kernels_str}_date_{dates_str}{ext}"

        # 5. 将筛选后的 DataFrame 保存到新的 CSV 文件
        filtered_df.to_csv(output_file, index=False, encoding='utf-8')
        
        print(f"筛选完成！ {len(filtered_df)} 行数据已写入到: {output_file}")

    except FileNotFoundError:
        print(f"错误: 输入文件 '{input_file}' 未找到。")
    except KeyError as e:
        print(f"错误: CSV文件中缺少必要的列: {e}。请检查文件头部是否包含 'kernel' 和 'ts'。")
    except Exception as e:
        print(f"处理过程中发生未知错误: {e}")

# 当该脚本被直接执行时，运行以下代码
if __name__ == "__main__":
    # 设置命令行参数解析器
    parser = argparse.ArgumentParser(description="根据 kernel 列表和日期列表筛选 CSV 文件。")
    
    # 添加输入文件参数
    parser.add_argument("input_file", help="输入的原始 CSV 文件路径。")
    
    # Kernel 参数，支持一个或多个值
    parser.add_argument(
        "--kernel", 
        required=True, 
        nargs='+',  # 接收一个或多个参数值
        help="要筛选的一个或多个 kernel 名称 (用空格分隔)。"
    )
    
    # ★★★ 关键改动 ★★★
    # Date 参数，同样支持一个或多个值
    parser.add_argument(
        "--date", 
        required=True, 
        nargs='+',  # 接收一个或多个参数值
        help="要筛选的一个或多个日期前缀 (YYYYMMDD)，用空格分隔。"
    )
    
    # 解析从命令行传入的参数
    args = parser.parse_args()
    
    # 调用主函数，args.kernel 和 args.date 现在都是列表
    filter_csv_advanced(args.input_file, args.kernel, args.date)

