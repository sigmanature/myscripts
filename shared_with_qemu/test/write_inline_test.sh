FINLINE_PATH="/mnt/f2fs/testfile.txt"
echo "Initial content in the file." > "${FINLINE_PATH}"  # 使用 ${FINLINE_PATH} 并用双引号包裹
INLINE_POS=$((4096-1))
WRITE_DATA="Hello dd test!!"
echo "${WRITE_DATA}" | dd of="${FINLINE_PATH}" bs=1 seek=$INLINE_POS conv=notrunc # 使用 ${FINLINE_PATH} 和 ${WRITE_DATA} 并用双引号包裹
rm -rf "${FINLINE_PATH}" # 使用 ${FINLINE_PATH} 并用双引号包裹