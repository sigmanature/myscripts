import os

filename = "interleaved_file_40k.bin"
block_size = 4 * 1024  # 4KB
num_blocks = 10

with open(filename, "wb") as f:
    for i in range(num_blocks):
        if (i + 1) % 2 != 0:  # 奇数块 (1, 3, 5, 7, 9)
            # 数据块 - 写入数据
            data = os.urandom(block_size)  # 使用随机数据填充，你可以替换成你想要的数据
            f.write(data)
            print(f"写入数据块 {i+1} (块大小: {block_size} 字节)")
        else:  # 偶数块 (2, 4, 6, 8, 10)
            # 空洞块 - 使用 seek 跳过
            f.seek(block_size, os.SEEK_CUR) # 从当前位置向前移动 block_size 字节，创建空洞
            print(f"创建空洞块 {i+1} (块大小: {block_size} 字节)")

print(f"文件 '{filename}' 已创建，大小为 {num_blocks * block_size / 1024}KB，包含交错的数据块和空洞块。")
