HOLE_PATH="/mnt/f2fs/interleaved_file_40k.bin"
POS=$((0))
dd if=/dev/zero of="${HOLE_PATH}" bs=36K count=1 seek=$POS conv=notrunc 
# rm -rf "${HOLE_PATH}" 
