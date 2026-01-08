# StandX Perps Market Maker

双向限价单做市策略，基于 StandX Perps (BSC)。默认挂 7.5 bps 买卖双向单，软阈值 7.0-8.0 bps，硬阈值 10 bps，偏离或成交后自动补单。

## 功能概览
- 认证与签名：EIP-191 + Ed25519 体签 ([standx_auth.py](standx_auth.py))，带 30s HTTP 超时与网络重试。
- 做市策略：双向限价单，2 秒监控；两层控制（软阈值保持、硬阈值强制重挂）([market_maker.py](market_maker.py)).
- 价格源：优先使用 mark_price，保证奖励资格判定一致。
- 容错：Timeout/ConnectionError/ProxyError 自动重试，订单状态刷新失败时使用上次缓存继续运行。

## 环境要求
- Python 3.11+
- pip / venv
- Git (推荐用私有仓库同步代码)

## 安装与运行
```bash
# 克隆代码
git clone <your-repo-url> && cd standx

# 创建并激活虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入私钥和参数

# 运行（默认 2 秒监控，600 秒测试）
python market_maker.py
```

## 环境变量 (.env)
```
WALLET_PRIVATE_KEY=0x...
LIMIT_ORDER_SYMBOL=BTC-USD
LIMIT_ORDER_QTY=0.004
LIMIT_ORDER_BPS=7.5
LIMIT_ORDER_TOLERANCE_BPS=0.5   # 目标范围 7.0-8.0 bps
MAX_ORDER_BPS=10                # 硬阈值，超过必须重挂
```

## 长时运行（示例）
当前 `main()` 默认运行 600 秒。若要跑 24h，有两种方式：
1) 外层循环守护：
```bash
while true; do python market_maker.py; sleep 2; done
```
2) 修改 `main()` 调用，把 `duration` 改为 `86400` 或 `None`（无限运行），再运行脚本。

### 后台运行示例
```bash
nohup python market_maker.py > run.log 2>&1 &
# 观察日志
tail -f run.log
```

## 已知行为与策略要点
- 软阈值：偏离不在 [7.0, 8.0] bps 时重挂；硬阈值：>10 bps 必重挂。
- 订单缺失（成交）会自动补单，不做平仓，只保持双边挂单。
- 价格优先取 mark_price；接口失败会在下次迭代重试。
- 时间同步很重要：请在服务器启用 `timedatectl set-ntp true`，否则可能触发签名过期 403。
- `.env` 与私钥严禁入库，已在 [.gitignore](.gitignore) 中忽略。

## 相关文件
- 策略主体：[market_maker.py](market_maker.py)
- 认证与 API 封装：[standx_auth.py](standx_auth.py)
- 订单监控（可选辅助）：[order_monitor.py](order_monitor.py)
- 依赖列表：[requirements.txt](requirements.txt)

## 部署建议
- 代码仓库使用私有仓库，避免泄露策略与私钥。部署时手工同步 `.env`。
- 出错时先看日志中的网络错误与时间同步；必要时重启脚本或重新获取时间。

## License
Internal use only.
