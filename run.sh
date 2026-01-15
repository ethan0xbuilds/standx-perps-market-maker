#!/bin/bash
set -e
cd "$(dirname "$0")"

# 检查虚拟环境和配置
[ -d ".venv" ] || { echo "❌ 虚拟环境不存在"; exit 1; }
[ -f ".env" ] || { echo "❌ .env 不存在"; exit 1; }

source .venv/bin/activate
mkdir -p logs

# 安装依赖（确保运行时依赖完整）
echo "📦 安装依赖..."
.venv/bin/python -m pip install -r requirements.txt >/dev/null || { echo "❌ 依赖安装失败"; exit 1; }

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

# 启动（前台运行供 systemd 管理）
echo "🚀 启动 Market Maker..."
exec .venv/bin/python -u market_maker.py 2>&1 | tee -a "$LOG_FILE"
