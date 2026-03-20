gdb-multiarch $KERNEL_DIR/vmlinux \
-tui \
-ex "target remote localhost:1234" \
-ex "break /root/learn/learn_os/linux-6.13.1/kernel/module/main.c:2883" \
-ex "add-symbol-file $MODULE_DIR/myfirstmodule.ko -s .text 0xffff80007b762000 -s .data 0xffff80007b7640b4 -s .init.text 0xffff80007b76a000"