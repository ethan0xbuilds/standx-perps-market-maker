# StandX Perps Market Maker

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)

简述：基于 StandX Perps (BSC) 的双向限价做市脚本，以 target_bps（默认 7.5）为中心挂单，维持价格偏离范围 [min_bps, max_bps]，超出时自动重挂；0.5 秒监控间隔，持续运行至收到停止信号。

## 功能概览

- **三层架构**：
  - [standx_auth.py](standx_auth.py)：钱包认证与 HTTP 工具（`make_api_call`、`_body_signature_headers`）
  - [standx_api.py](standx_api.py)：API 方法库（9 个函数）
  - [market_maker.py](market_maker.py)：做市策略主程序
- 做市策略：0.5 秒轮询监控，三步流程（检查持仓 → 检查价格偏离 → 重挂订单）
- 价格源：优先使用 mark_price，保证奖励资格判定一致
- 容错：Timeout/ConnectionError/ProxyError 自动重试 3 次；订单刷新失败时使用上次缓存继续运行
- 优雅关闭：支持 SIGTERM/SIGINT 信号处理，停止时自动取消所有订单

## 环境要求

- Python 3.11+
- pip / venv
- Git

## 安装与运行

```bash
# 克隆代码
git clone https://github.com/ethan0xbuilds/standx-perps-market-maker.git && cd standx-perps-market-maker

# 创建并激活虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入私钥和参数

# 运行（0.5 秒监控间隔，无限运行直至收到停止信号）
python market_maker.py
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
```

**参数说明**：

- 初始下单时以 `target_bps` 为基准计算价格
- 监控中如果实际偏离超出 [min_bps, max_bps] 范围，则取消所有订单并重新挂单
- 示例：市价 100，则以 7.5 bps 挂单 → 买 99.925，卖 100.075

## 余额自动降级策略

策略内置**三层风险控制机制**，根据实时余额自动切换运行模式。每 10 次迭代（约 5 秒）检查一次余额，若余额跨越阈值则自动调整挂单参数。

### 运行模式对照表

| 余额范围 | 模式 | target_bps | min_bps | max_bps | 说明 |
| -------- | ------ | ----------- | --------- | --------- | ------ |
| ≥ 100 USDT | **normal** | 7.5 | 7.0 | 10 | 正常做市，理想利润空间 |
| 50-100 USDT | **degraded_1** | 25 | 20 | 29.5 | 手续费容忍模式，扩大价差 |
| < 50 USDT | **degraded_2** | 80 | 70 | 95 | 止损保护模式，极端价差快速获利 |

### 工作原理

1. **初始化**：启动时调用 `check_and_update_mode()` 确定初始模式
2. **定期检查**：监控循环中每 10 次迭代检查一次余额
3. **自动切换**：余额变化触发模式转换时：
   - 取消当前订单
   - 更新挂单参数
   - 重新下双向单
4. **故障容错**：余额查询失败时保持当前模式，继续运行

### 模式切换示例

```text
初始状态：余额 120 USDT → normal 模式（7.5 bps）
          挂单：买 99.925 @ 0.005，卖 100.075 @ 0.005

市场下跌，余额扣费后变为 85 USDT
↓ 触发降级
降级后：degraded_1 模式（25 bps）
        重新挂单：买 99.75 @ 0.005，卖 100.25 @ 0.005
        （价差扩大至 0.5，抵消手续费压力）

继续亏损，余额降至 40 USDT
↓ 再次降级
极端模式：degraded_2 模式（80 bps）
         重新挂单：买 99.2 @ 0.005，卖 100.8 @ 0.005
         （极端价差快速获利，触发止损保护）
```

### 如何设置阈值

- `BALANCE_THRESHOLD_1`：设置为你能接受的"开始谨慎做市"的余额门槛
- `BALANCE_THRESHOLD_2`：设置为你的"止损红线"，低于此值则采用极端价差模式

## 运行模式

默认无限运行。策略会循环执行以下三步：

1. **检查持仓**：若存在持仓则立即市价平仓
2. **检查价格偏离**：买卖单偏离是否在 [min_bps, max_bps] 范围内
3. **重挂订单**：若超出范围则取消所有订单，等待 1 秒，重新下双向单

每次检查间隔为 **0.5 秒**，可通过修改 [market_maker.py](market_maker.py#L403) 的 `market_maker.run(check_interval=0.5)` 调整。

### 优雅关闭

程序支持 SIGTERM/SIGINT 信号，收到停止信号时会：

1. 标记 `_shutdown_requested` 为 True
2. 等待当前迭代完成
3. 清理所有订单
4. 优雅退出

### Ubuntu 生产环境部署（推荐）

```bash
# 1. 启动（后台运行 + 日志输出）
chmod +x run.sh stop.sh
./run.sh

# 2. 监控日志
tail -f logs/market_maker.log

# 3. 停止
./stop.sh
```

**启动脚本特性**：

- 自动检测虚拟环境和配置文件
- 防止重复启动
- 日志实时写入 `logs/market_maker.log`
- PID 管理，方便停止

### 手动后台运行

```bash
nohup python -u market_maker.py >> run.log 2>&1 &
# 观察日志
tail -f run.log
```

## 已知行为与策略要点

- 监控间隔 0.5 秒（可调整）；价格偏离超出 [min_bps, max_bps] 时重挂
- 检测到持仓时立即市价平仓，保证不违反杠杆限制
- 价格优先取 mark_price；接口失败会在下次迭代重试
- 订单状态查询失败时使用上次缓存状态，不中断策略
- 时间同步很重要：请在服务器启用 `timedatectl set-ntp true`，否则可能触发签名过期（403 错误）
- `.env` 与私钥严禁入库，已在 [.gitignore](.gitignore) 中忽略

## 相关文件

- 做市主程序：[market_maker.py](market_maker.py)（430 行）
  - 双向限价单管理、价格监控、订单调整
- 认证与 HTTP 工具：[standx_auth.py](standx_auth.py)（308 行）
  - EIP-191 + Ed25519 钱包认证
  - `make_api_call()`：通用 HTTP 请求工具（支持重试）
  - `_body_signature_headers()`：Ed25519 签名生成（用于有序 API）
- API 方法库：[standx_api.py](standx_api.py)（190 行）
  - 9 个函数：query_balance、query_symbol_price、query_positions
  - 订单函数：new_limit_order、new_market_order、cancel_order
  - 查询函数：query_order、query_open_orders、query_orders
- 依赖列表：[requirements.txt](requirements.txt)

## 部署建议

- 部署时手工同步 `.env`。
- 出错时先看日志中的网络错误与时间同步；必要时重启脚本或重新获取时间。

## License

MIT License
