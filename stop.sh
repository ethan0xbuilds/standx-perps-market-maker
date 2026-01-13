#!/bin/bash
cd "$(dirname "$0")"

echo "🛑 停止 Market Maker..."

# 查找进程
PID=$(pgrep -f "python.*market_maker.py")

if [ -z "$PID" ]; then
    echo "⚠️  未找到运行中的进程"
    exit 0
fi

echo "📋 找到进程 PID: $PID"
echo "⏳ 发送 SIGTERM 信号，等待优雅退出（最多30秒）..."

# 发送 SIGTERM 信号让程序优雅关闭（取消订单）
kill -TERM "$PID"

# 等待最多30秒让程序优雅退出
for i in {1..30}; do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "✅ 进程已优雅退出"
        exit 0
    fi
    sleep 1
    if [ $((i % 5)) -eq 0 ]; then
        echo "   等待中... ${i}秒"
    fi
done

# 如果30秒后还在运行，强制终止
echo "⚠️  进程未在30秒内退出，强制终止..."
kill -KILL "$PID" 2>/dev/null
sleep 1

if kill -0 "$PID" 2>/dev/null; then
    echo "❌ 无法终止进程"
    exit 1
else
    echo "✅ 进程已强制终止"
    exit 0
fi
