import sys
import re

def extract_signatures_from_diff(file_path):
    """
    从 git diff 的 hunk header 文件中提取、清理并去重函数签名。

    Args:
        file_path (str): 包含 diff hunk header 的文本文件路径。

    Returns:
        list: 一个包含了唯一、排序后的函数签名的列表。
    """
    # 使用集合（set）来自动处理重复的函数签名
    unique_signatures = set()

    try:
        # 使用 utf-8 编码打开文件，以兼容各种代码文件
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                # 寻找一行中最后出现的 '@@' 的位置
                # 这能确保我们正确处理了前面的 '@@ -a,b +c,d @@' 部分
                last_at_at_pos = line.rfind('@@')
                
                # 如果找到了 '@@'
                if last_at_at_pos != -1:
                    # 提取 '@@' 之后的所有内容，这部分就是函数签名
                    # +2 是为了跳过 '@@' 本身
                    signature = line[last_at_at_pos + 2:].strip()
                    
                    # 有时提取出来可能是空字符串，或者只是一个 {
                    # 我们只添加有意义的签名
                    if signature and signature != '{':
                        unique_signatures.add(signature)

    except FileNotFoundError:
        print(f"错误：文件 '{file_path}' 未找到。")
        return None
    except Exception as e:
        print(f"处理文件时发生错误: {e}")
        return None

    # 将集合转换为列表并排序，以便输出结果是确定的
    return sorted(list(unique_signatures))

if __name__ == "__main__":
    # 检查命令行参数是否提供了文件名
    if len(sys.argv) < 2:
        print("用法: python parse_diff.py <your_diff_output.txt>")
        sys.exit(1)

    input_file = sys.argv[1]
    
    final_signatures = extract_signatures_from_diff(input_file)
    
    if final_signatures is not None:
        print("--- 修改的函数/方法总表 ---")
        if not final_signatures:
            print("未找到任何有效的函数签名。")
        else:
            for sig in final_signatures:
                print(sig)
