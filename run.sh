#!/bin/bash
# StandX Market Maker 启动脚本

set -e

cd "$(dirname "$0")"

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "❌ 虚拟环境不存在，请先运行: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo "❌ .env 文件不存在，请先配置环境变量"
    exit 1
fi

# 激活虚拟环境
source .venv/bin/activate

# 创建日志目录
mkdir -p logs

# 日志文件路径
LOG_FILE="logs/market_maker.log"

# 如果日志文件已存在，转储备份并加上时间戳
if [ -f "$LOG_FILE" ]; then
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    BACKUP_LOG="logs/market_maker_${TIMESTAMP}.log"
    mv "$LOG_FILE" "$BACKUP_LOG"
    echo "📦 日志已备份: $BACKUP_LOG"
fi

# 检查是否已经在运行
if [ -f "logs/market_maker.pid" ]; then
    PID=$(cat logs/market_maker.pid)
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "⚠️  Market maker 已在运行中 (PID: $PID)"
        echo "如需重启，请先运行: ./stop.sh"
        exit 1
    fi
fi

# 启动策略（python -u 禁用缓冲，实时写入日志）
echo "🚀 启动 Market Maker..."
nohup python -u market_maker.py >> "$LOG_FILE" 2>&1 &
PID=$!

# 保存 PID
echo "$PID" > logs/market_maker.pid

echo "✅ Market maker 已启动"
echo "   PID: $PID"
echo "   日志: $LOG_FILE"
echo ""
echo "监控日志: tail -f $LOG_FILE"
echo "停止运行: ./stop.sh"
