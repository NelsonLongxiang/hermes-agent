#!/bin/bash
# 清理僵尸Claude Code进程
# 这些进程会话已停止但进程未被终止

echo "=== 检测僵尸Claude进程 ==="
echo "当前Claude进程数: $(ps aux | grep -c '[c]laude.*--output-format')"

echo ""
echo "=== 查找符合条件的僵尸进程 ==="
# 查找符合条件的Claude Code进程（有--output-format stream-json参数）
zombie_pids=$(ps aux | grep '[c]laude.*--output-format' | awk '{print $2}')

if [ -z "$zombie_pids" ]; then
    echo "✓ 没有发现僵尸Claude进程"
    exit 0
fi

echo "发现以下僵尸进程:"
ps aux | grep '[c]laude.*--output-format' | head -20

echo ""
echo "=== 终止僵尸进程 ==="
count=0
for pid in $zombie_pids; do
    echo "终止 PID $pid..."
    kill -TERM "$pid" 2>/dev/null
    ((count++))
done

echo "已发送SIGTERM到 $count 个进程"

echo ""
echo "=== 等待3秒让进程优雅退出 ==="
sleep 3

echo ""
echo "=== 检查是否还有残留进程 ==="
remaining=$(ps aux | grep -c '[c]laude.*--output-format')
if [ "$remaining" -gt 0 ]; then
    echo "仍有 $remaining 个进程未终止，强制SIGKILL..."
    ps aux | grep '[c]laude.*--output-format' | awk '{print $2}' | xargs -r kill -9
    sleep 1
fi

echo ""
echo "=== 最终状态 ==="
final_count=$(ps aux | grep -c '[c]laude.*--output-format')
echo "当前Claude进程数: $final_count"

if [ "$final_count" -eq 0 ]; then
    echo "✓ 所有僵尸进程已清理"
else
    echo "⚠ 仍有 $final_count 个进程运行，可能需要手动检查"
    ps aux | grep '[c]laude.*--output-format' | head -10
fi
