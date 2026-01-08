#!/bin/bash
# StandX Market Maker 停止脚本

set -e

cd "$(dirname "$0")"

if [ ! -f "logs/market_maker.pid" ]; then
    echo "⚠️  未找到 PID 文件，market maker 可能未运行"
    exit 1
fi

PID=$(cat logs/market_maker.pid)

if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "⚠️  进程 $PID 不存在，market maker 可能已停止"
    rm -f logs/market_maker.pid
    exit 1
fi

echo "🛑 停止 Market Maker (PID: $PID)..."
kill "$PID"

# 等待进程退出
for i in {1..10}; do
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "✅ Market maker 已停止"
        rm -f logs/market_maker.pid
        exit 0
    fi
    sleep 1
done

# 如果还没停止，强制终止
echo "⚠️  正常停止失败，强制终止..."
kill -9 "$PID" 2>/dev/null || true
rm -f logs/market_maker.pid
echo "✅ Market maker 已强制停止"
