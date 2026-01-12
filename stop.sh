#!/bin/bash
cd "$(dirname "$0")"

echo "🛑 停止 Market Maker..."
pkill -f "python.*market_maker.py" || echo "⚠️  未找到进程"
sleep 1
pkill -9 -f "cpulimit.*market_maker" || true
echo "✅ 已停止"
