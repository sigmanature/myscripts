#!/bin/bash

PASSWORD="1"  # 替换成你的实际密码
HOST="localhost"
PORT="5022"
USER="root"
COMMAND="cd ~/shared_with_host/mymodules/file_call; bash"  # 你想要执行的命令 bash意味着启动一个新的bash shell会话窗

sshpass -p "$PASSWORD" ssh -t -p "$PORT" "$USER@$HOST" "$COMMAND" 