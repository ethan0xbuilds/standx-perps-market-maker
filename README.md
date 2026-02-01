# StandX Perps Market Maker

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

简述：基于 StandX Perps (BSC) 的双向限价做市脚本，以 target_bps（默认 7.5）为中心挂单，维持价格偏离范围 [min_bps, max_bps]，超出时自动重挂。支持 HTTP 轮询和 WebSocket 实时推送两种价格源。

## 功能概览

- **核心模块**：
  - [standx_auth.py](standx_auth.py)：钱包认证与 HTTP 工具
  - [standx_api.py](standx_api.py)：HTTP API 方法库（9 个函数）
  - [market_maker.py](market_maker.py)：做市策略主程序
- 做市策略：可配置监控间隔，三步流程（检查持仓 → 检查价格偏离 → 重挂订单）
- 价格获取：支持 HTTP 轮询和 WebSocket 实时推送两种模式
- 容错：Timeout/ConnectionError/ProxyError 自动重试 3 次；订单刷新失败时使用上次缓存继续运行
- 优雅关闭：支持 SIGTERM/SIGINT 信号处理，停止时自动取消所有订单

## 环境要求

- Python 3.11+
- pip / venv
- Git

## 安装与运行

### 1. 安装依赖

```bash
# Ubuntu 安装 Python 虚拟环境
sudo apt-get update && sudo apt-get install -y python3-venv
```

### 2. 克隆代码并配置

```bash
# 克隆到 /root/standx
git clone https://github.com/ethan0xbuilds/standx-perps-market-maker.git /root/standx
cd /root/standx

# 设置脚本权限
chmod +x run.sh stop.sh

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入私钥和参数

# 安装 Python 依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 部署为 systemd 服务

创建服务文件 `/etc/systemd/system/standx-market-maker.service`：

```ini
[Unit]
Description=StandX Market Maker
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/standx
ExecStart=/root/standx/run.sh
ExecStop=/root/standx/stop.sh
# --- 资源限制开始 ---
# 限制 CPU 使用率为单核的 35%
CPUQuota=35%
# 内存使用达到 500M 时，系统会尝试回收内存页，进程仍可运行但性能可能受限
MemoryHigh=500M
# 内存硬上限 800M，超过此值系统将直接 Kill 进程（保护节点不重启）
MemoryMax=800M
# 限制该服务及其子进程的总线程/进程数
TasksMax=50
# 禁止使用 Swap 交换分区，确保内存使用的确定性（可选，推荐）
MemorySwapMax=0
# --- 资源限制结束 ---

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 4. 启动并管理服务

```bash
# 重载配置并启用开机自启
sudo systemctl daemon-reload
sudo systemctl enable standx-market-maker

# 启动服务
sudo systemctl start standx-market-maker

# 查看状态
sudo systemctl status standx-market-maker

# 查看日志
sudo journalctl -u standx-market-maker -f
tail -f /root/standx/logs/market_maker.log

# 停止/重启服务
sudo systemctl stop standx-market-maker
sudo systemctl restart standx-market-maker
```

## 环境变量 (.env)

```env
WALLET_PRIVATE_KEY=0x...

# Market maker configuration
MARKET_MAKER_SYMBOL=BTC-USD          # 交易对
MARKET_MAKER_QTY=0.005               # 单笔订单数量
MARKET_MAKER_TARGET_BPS=7.5          # 目标挂单偏离 (basis points)
MARKET_MAKER_MIN_BPS=7.0             # 最小允许偏离，低于此值重挂
MARKET_MAKER_MAX_BPS=10              # 最大允许偏离，超过此值重挂

# Balance-based degradation (risk control)
MARKET_MAKER_BALANCE_THRESHOLD_1=100  # 降级阈值1（单位：USDT）
MARKET_MAKER_BALANCE_THRESHOLD_2=50   # 降级阈值2（单位：USDT）

# 监控间隔（秒）
MARKET_MAKER_CHECK_INTERVAL=0.0         # 价格监控间隔（0表示无延迟，默认0秒）

# Telegram 通知（可选）
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token-here
TELEGRAM_CHAT_ID=123456789              # 你的 Telegram 用户 ID 或群组 ID
```

## Telegram 通知配置

做市器支持通过 Telegram 机器人推送关键事件通知，包括模式切换、持仓平仓、订单重挂、严重异常等。

### 创建 Telegram Bot

1. **与 BotFather 对话**  
   在 Telegram 搜索 `@BotFather`，发送 `/newbot` 命令创建新机器人

2. **设置机器人名称**  
   按提示设置机器人的显示名称和用户名（用户名必须以 `bot` 结尾）

3. **获取 Bot Token**  
   创建成功后，BotFather 会返回一个 Token，格式类似：`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

4. **获取 Chat ID**  
   - 方法1：搜索 `@userinfobot`，发送任意消息，它会返回你的用户 ID
   - 方法2：与你的 Bot 发送一条消息，然后访问：  

     ```url
     https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
     ```

     在返回的 JSON 中找到 `message.chat.id`

5. **配置环境变量**  
   在 `.env` 文件中添加：

   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   TELEGRAM_CHAT_ID=123456789
   ```

### 通知事件列表

| 事件 | 触发条件 | 限流 |
| ------ | --------- | ------ |
| 策略启动 | 进程启动 | 无 |
| 模式切换 | 余额跨阈值或美股开盘时段 | 无 |
| 持仓平仓 | 检测到非零持仓并平仓 | 无 |
| 平仓失败/超时 | 平仓请求失败或超时未归零 | 无 |
| 订单重挂 | 价格偏离范围触发重挂 | **5分钟**（聚合显示次数） |
| 致命异常 | 主循环抛出未捕获异常 | 无 |
| 策略停止 | 优雅关闭并清理订单 | 无 |
| 认证失败 | API 认证失败 | 无 |

### 限流说明

订单重挂是高频事件，为避免刷屏：

- **首次重挂**：立即通知
- **5 分钟内**：后续重挂静默累计
- **5 分钟后**：下次重挂时发送通知，并显示"过去 5 分钟内共 N 次"

### 测试通知

在配置完成后，可以先测试通知是否正常：

```bash
source .venv/bin/activate
python tests/test_notification.py
```

脚本会检查配置并发送测试消息到 Telegram。

### 禁用通知

如果不需要通知，只需不设置 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`，或将它们留空即可。策略会正常运行，仅跳过通知发送。

## 策略运行逻辑

策略持续运行，每次循环执行以下三步：

1. **检查持仓**：若存在持仓则立即市价平仓
2. **检查价格偏离**：买卖单偏离是否在 [min_bps, max_bps] 范围内
3. **重挂订单**：若超出范围则取消所有订单，等待 1 秒，重新下双向单

检查间隔可通过 `MARKET_MAKER_CHECK_INTERVAL` 环境变量配置：

- `0.0`：无人工延迟（推荐，依赖 API 调用本身的延迟）
- `0.5`：500毫秒间隔
- `1.0`：1秒间隔

### 优雅关闭

程序支持 SIGTERM/SIGINT 信号，收到停止信号时会：

1. 标记 `_shutdown_requested` 为 True
2. 等待当前迭代完成
3. 清理所有订单
4. 优雅退出

## 已知行为与策略要点

- 检查间隔可配置（默认 0 秒）；价格偏离超出 [min_bps, max_bps] 时重挂
- 检测到持仓时立即市价平仓，保证不违反杠杆限制
- 价格优先取 mark_price；接口失败会在下次迭代重试
- 订单状态查询失败时使用上次缓存状态，不中断策略
- 时间同步很重要：请在服务器启用 `timedatectl set-ntp true`，否则可能触发签名过期（403 错误）
- `.env` 与私钥严禁入库，已在 [.gitignore](.gitignore) 中忽略

## 相关文件

- 做市主程序：[market_maker.py](market_maker.py)（~600 行）
  - 双向限价单管理、价格监控、订单调整
- 认证与 HTTP 工具：[standx_auth.py](standx_auth.py)（~600 行）
  - EIP-191 + Ed25519 钱包认证
  - `make_api_call()`：通用 HTTP 请求工具（支持重试）
  - `_body_signature_headers()`：Ed25519 签名生成（用于有序 API）
- API 方法库：[standx_api.py](standx_api.py)（190 行）
  - 9 个函数：query_balance、query_symbol_price、query_positions
  - 订单函数：new_limit_order、new_market_order、cancel_order
  - 查询函数：query_order、query_open_orders、query_orders
- 通知模块：[notifier.py](notifier.py)（~70 行）
  - Telegram 通知器，支持时间限流防刷屏
- 依赖列表：[requirements.txt](requirements.txt)

## License

MIT License
