HOLE_PATH="/mnt/f2fs/holes.txt"
truncate -s 35M "${HOLE_PATH}" # 使用 ${HOLE_PATH} 并用双引号包裹
POS=$((200))
WRITE_DATA="Hello dd test!!"
dd if=/dev/zero of="${HOLE_PATH}" bs=40K count=1 seek=$POS conv=notrunc # 使用 ${FINLINE_PATH} 和 ${WRITE_DATA} 并用双引号包裹
rm -rf "${HOLE_PATH}" 
