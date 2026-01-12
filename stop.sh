#!/bin/bash
cd "$(dirname "$0")"

echo "🛑 停止 Market Maker..."
pkill -f "python.*market_maker.py" || echo "⚠️  未找到进程"
echo "✅ 已停止"
