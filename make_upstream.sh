make -C $BASE/f2fs O=$BASE/f2fs_upstream ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig Image  -j$(nproc) &> $BASE/f2fs_upstream/makelog.txt
# make O=$BASE/f2fs_bench ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig Image  -j$(nproc) &> $BASE/f2fs_bench/makebenchlog.txt
# make O=$BASE/f2fs_release ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig Image  -j$(nproc) &> $BASE/f2fs_release/makebenchlog.txt
# cp ./fs/f2fs/f2fs.ko ../modshare/
# cp ./modules.* ../modshare/
# cp ./lib/zstd/zstd_compress.ko ../modshare/
# cp ./lib/lz4/lz4_compress.ko ../modshare/
# cp ./lib/lz4/lz4hc_compress.ko ../modshare/
# make ARCH=arm64 mrproper