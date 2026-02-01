# StandX Perps Market Maker

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

简述：基于 StandX Perps (BSC) 的双向限价做市脚本，以 target_bps（默认 7.5）为中心挂单，维持价格偏离范围 [min_bps, max_bps]，超出时自动重挂。行情使用 WebSocket depth_book，中间价更新后触发检查；下单与撤单走 WebSocket 订单流，订单与持仓状态以 WS 推送为准。

## 功能概览

- **核心模块**：
  - [standx_auth.py](standx_auth.py)：钱包认证与 HTTP 工具
  - [standx_api.py](standx_api.py)：HTTP API 方法库（查询/下单/撤单等）
  - [adapter/standx_adapter.py](adapter/standx_adapter.py)：WS 适配器，缓存 depth_book / 订单 / 持仓
  - [api/ws_client.py](api/ws_client.py)：WebSocket 客户端（市场流与订单流）
  - [market_maker.py](market_maker.py)：做市策略主程序
- 做市策略：可配置监控间隔（仅在 mid_price 更新后触发检查）
- 价格获取：WebSocket depth_book 实时推送
- 下单/撤单：WebSocket 订单流（下单结果/订单状态推送）
- 容错：Timeout/ConnectionError/ProxyError 自动重试 3 次；WS 断线记录日志并继续使用缓存状态
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

将仓库内的服务文件拷贝到 systemd 目录：

```bash
sudo cp standx.service /etc/systemd/system/standx.service
sudo systemctl daemon-reload
sudo systemctl enable standx
```

### 4. 启动并管理服务

```bash
# 启动服务
sudo systemctl start standx

# 查看状态
sudo systemctl status standx

# 查看日志
tail -f /root/standx/logs/market_maker.log

# 停止/重启服务
sudo systemctl stop standx
sudo systemctl restart standx
```

## 环境变量 (.env)

```env
WALLET_PRIVATE_KEY=0x...                # 方式1：钱包私钥（自动生成 Ed25519）

ED25519_PRIVATE_KEY=base58...           # 方式2：Ed25519 私钥
ACCESS_TOKEN=token...                   # 方式2：访问令牌（WS 订阅与订单流必需）

# Market maker configuration
MARKET_MAKER_SYMBOL=BTC-USD          # 交易对
MARKET_MAKER_QTY=0.005               # 单笔订单数量
MARKET_MAKER_TARGET_BPS=7.5          # 目标挂单偏离 (basis points)
MARKET_MAKER_MIN_BPS=7.0             # 最小允许偏离，低于此值重挂
MARKET_MAKER_MAX_BPS=10              # 最大允许偏离，超过此值重挂

# 监控间隔（秒）
MARKET_MAKER_CHECK_INTERVAL=0.0      # 价格监控间隔（0表示无延迟，默认0秒）

# Telegram 通知（可选）
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token-here
TELEGRAM_CHAT_ID=123456789           # 你的 Telegram 用户 ID 或群组 ID
```

## Telegram 通知配置

做市器支持通过 Telegram 机器人推送关键事件通知，包括持仓平仓、订单重挂、严重异常等。

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

| 事件 | 触发条件 | 备注 |
| ------ | --------- | ------ |
| 策略启动 | 进程启动 | 无 |
| 持仓平仓 | 检测到非零持仓并平仓 | 无 |
| 平仓失败/超时 | 平仓请求失败或超时未归零 | 无 |
| 订单重挂 | 价格偏离范围触发重挂 | 默认不做时间限流 |
| 致命异常 | 主循环抛出未捕获异常 | 无 |
| 策略停止 | 优雅关闭并清理订单 | 无 |
| 认证失败 | API 认证失败 | 无 |

### 限流说明

订单重挂是高频事件，通知器支持时间限流（`throttle_key` + `throttle_seconds`）。当前策略默认不启用时间限流，如需限流可在代码中为 `Notifier.send()` 传入 `throttle_seconds`。

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

策略持续运行，每次循环执行以下步骤：

1. **等待行情**：等待 depth_book mid_price 就绪
2. **检查持仓**：若存在持仓则立即市价平仓（reduce-only）
3. **检查偏离**：仅在 mid_price 更新后判断偏离是否在 [min_bps, max_bps] 范围内
4. **重挂订单**：若超出范围则取消所有订单并等待确认 → 下双向单 → 等待订单确认

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
- 价格来源为 depth_book 中间价（mid_price），仅在价格更新后触发偏离检查
- 订单与持仓状态来自 WebSocket 推送缓存，不依赖 HTTP 轮询
- WS 订单流与持仓订阅需要 `ACCESS_TOKEN`
- 时间同步很重要：请在服务器启用 `timedatectl set-ntp true`，否则可能触发签名过期（403 错误）
- `.env` 与私钥严禁入库，已在 [.gitignore](.gitignore) 中忽略

## 相关文件

- 做市主程序：[market_maker.py](market_maker.py)
  - 双向限价单管理、订单偏离检查、重挂逻辑
- WS 适配器：[adapter/standx_adapter.py](adapter/standx_adapter.py)
  - depth_book/订单/持仓缓存、订单确认等待、WS 订单流封装
- WebSocket 客户端：[api/ws_client.py](api/ws_client.py)
  - StandXMarketStream（行情+订阅）与 StandXOrderStream（下单/撤单）
- 认证与 HTTP 工具：[standx_auth.py](standx_auth.py)
  - EIP-191 + Ed25519 钱包认证、请求签名
- API 方法库：[standx_api.py](standx_api.py)
  - 查询与签名下单接口（备用/调试）
- 通知模块：[notifier.py](notifier.py)
  - Telegram 通知器（可选限流）
- 依赖列表：[requirements.txt](requirements.txt)

## License

MIT License
