make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig Image modules -j8 &> makelog.txt
cp ./fs/f2fs/f2fs.ko ../modshare/
cp ./modules.* ../modshare/