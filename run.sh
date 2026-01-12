#!/bin/bash
set -e
cd "$(dirname "$0")"

# 检查虚拟环境和配置
[ -d ".venv" ] || { echo "❌ 虚拟环境不存在"; exit 1; }
[ -f ".env" ] || { echo "❌ .env 不存在"; exit 1; }

source .venv/bin/activate
mkdir -p logs

LOG_FILE="logs/market_maker.log"

# 日志轮转
if [ -f "$LOG_FILE" ]; then
    mv "$LOG_FILE" "logs/market_maker_$(date +%Y%m%d_%H%M%S).log"
fi

# 检查是否已运行
pgrep -f "python.*market_maker.py" > /dev/null && { 
    echo "⚠️  已在运行"
    exit 1
}

# 启动（cpulimit 70%）
echo "🚀 启动 Market Maker (CPU限制: 70%)..."
nohup cpulimit -l 70 python -u market_maker.py >> "$LOG_FILE" 2>&1 &
echo "✅ 已启动 PID: $!"
echo "   日志: tail -f $LOG_FILE"
