# rm -rf lff2fs.img  # 删除旧的镜像文件
dd if=/dev/zero of=lff2fs.img bs=10m count=5 # 创建一个 100MB 的空镜像文件
# mkfs.f2fs -O extra_attr,compression lff2fs.img
sudo mkfs.f2fs lff2fs.img
# sudo mount -t f2fs lff2fs.img ./f2rootfs  # 挂载镜像文件
# cd ./f2rootfs
# dd if=/dev/zero of=lf.c bs=1G count=1 # 创建一个 100MB 的空镜像文件
# cd ..  # 返回上级目录
# sudo umount ./f2rootfs  # 卸载镜像文件
