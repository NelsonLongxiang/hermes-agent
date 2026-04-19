#!/usr/bin/env bash
# claude-session 环境优化配置脚本
# 用途：自动配置 HERMES_STREAM_STALE_TIMEOUT，防止 Stream Stalled 中断
# 使用：bash ~/.hermes/skills/claude-session/scripts/configure.sh

set -euo pipefail

HERMES_ENV="${HERMES_HOME:-$HOME/.hermes}/.env"
VAR_NAME="HERMES_STREAM_STALE_TIMEOUT"
RECOMMENDED_VALUE="300"

echo "=== Claude Session 环境优化配置 ==="
echo ""

# 检查是否已配置
if grep -q "^${VAR_NAME}=" "$HERMES_ENV" 2>/dev/null; then
    current=$(grep "^${VAR_NAME}=" "$HERMES_ENV" | cut -d'=' -f2)
    echo "✅ ${VAR_NAME} 已配置为 ${current}"
    if [ "$current" != "$RECOMMENDED_VALUE" ]; then
        echo "⚠️  当前值 ${current} 与推荐值 ${RECOMMENDED_VALUE} 不同"
        echo "   推荐值 ${RECOMMENDED_VALUE} 适合大多数场景（研究、编码、长任务）"
    fi
else
    echo "🔧 正在配置 ${VAR_NAME}=${RECOMMENDED_VALUE} ..."
    echo "" >> "$HERMES_ENV"
    echo "# Claude Session 优化 - 防止 Stream Stalled 中断" >> "$HERMES_ENV"
    echo "${VAR_NAME}=${RECOMMENDED_VALUE}" >> "$HERMES_ENV"
    echo "✅ 已写入 ${HERMES_ENV}"
fi

echo ""
echo "⚠️  重要：需要重启 Hermes Gateway 才能生效！"
echo "   在运行 gateway 的终端中 Ctrl+C，然后运行："
echo "   hermes gateway run"
echo ""
echo "配置完成 ✅"
