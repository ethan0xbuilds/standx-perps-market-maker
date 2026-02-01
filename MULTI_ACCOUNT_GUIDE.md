# 多账户部署指南

本指南说明如何在同一台服务器上运行多个账户的做市策略。

## 架构说明

- **共享代码**：所有账户共享同一套代码目录
- **独立配置**：每个账户使用独立的 `.env` 配置文件
- **独立日志**：每个账户的日志文件独立存储（带账户前缀）
- **独立服务**：每个账户作为独立的 systemd 服务运行

## 部署步骤

### 1. 准备配置文件

为每个账户创建独立的配置文件：

```bash
cd /root/standx

# 复制示例配置文件
cp .env.account1.example .env.account1
cp .env.account2.example .env.account2

# 编辑配置文件，填入各账户的私钥和参数
vim .env.account1
vim .env.account2
```

**重要**：每个账户的配置文件必须包含：
- 独立的私钥（`WALLET_PRIVATE_KEY` 或 `ED25519_PRIVATE_KEY` + `ACCESS_TOKEN`）
- 可选：独立的 Telegram 通知配置

### 2. 部署 systemd 服务

```bash
# 复制服务文件到 systemd 目录
sudo cp standx-account1.service /etc/systemd/system/
sudo cp standx-account2.service /etc/systemd/system/

# 重载 systemd 配置
sudo systemctl daemon-reload

# 启用开机自启
sudo systemctl enable standx-account1
sudo systemctl enable standx-account2
```

### 3. 启动服务

```bash
# 启动账户1
sudo systemctl start standx-account1

# 启动账户2
sudo systemctl start standx-account2

# 查看状态
sudo systemctl status standx-account1
sudo systemctl status standx-account2
```

### 4. 查看日志

```bash
# 方式1：查看文件日志（推荐）
tail -f /root/standx/logs/account1_market_maker.log
tail -f /root/standx/logs/account2_market_maker.log

# 方式2：查看 systemd 日志
sudo journalctl -u standx-account1 -f
sudo journalctl -u standx-account2 -f
```

## 管理命令

### 启动/停止单个账户

```bash
# 启动
sudo systemctl start standx-account1

# 停止（优雅关闭，会取消所有订单）
sudo systemctl stop standx-account1

# 重启
sudo systemctl restart standx-account1

# 查看状态
sudo systemctl status standx-account1
```

### 批量管理

```bash
# 启动所有账户
sudo systemctl start standx-account1 standx-account2

# 停止所有账户
sudo systemctl stop standx-account1 standx-account2

# 查看所有账户状态
sudo systemctl status 'standx-account*'
```

## 添加新账户

如果需要添加第3个、第4个账户：

1. **创建配置文件**：
   ```bash
   cp .env.account1.example .env.account3
   vim .env.account3  # 填入新账户信息
   ```

2. **复制并修改服务文件**：
   ```bash
   cp standx-account1.service standx-account3.service
   vim standx-account3.service
   ```
   
   修改以下内容：
   - `Description=StandX Account 3`
   - `ExecStart=...--config /root/standx/.env.account3 --log-prefix account3`
   - `ExecStop=...account3...`

3. **部署并启动**：
   ```bash
   sudo cp standx-account3.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable standx-account3
   sudo systemctl start standx-account3
   ```

## 资源监控

查看资源使用情况：
```bash
# 查看进程状态
systemctl status standx-account1

# 查看系统资源
top -p $(pgrep -f "market_maker.py --config.*account1")

# 查看所有做市进程
ps aux | grep market_maker.py
```

## 注意事项

1. **端口冲突**：WebSocket 连接是客户端模式，不会有端口冲突
2. **网络限制**：注意 StandX API 的速率限制
3. **日志轮转**：日志文件会自动轮转（10MB × 5 备份）
4. **优雅停止**：始终使用 `systemctl stop` 停止服务，避免 `kill -9`

## 故障排查

### 服务启动失败
```bash
# 查看详细错误日志
sudo journalctl -u standx-account1 -n 50 --no-pager

# 检查配置文件
python -c "from dotenv import load_dotenv; load_dotenv('.env.account1'); import os; print(os.getenv('WALLET_PRIVATE_KEY'))"
```

### 进程查找
```bash
# 查看所有做市进程
ps aux | grep market_maker.py

# 查看特定账户进程
pgrep -fa "market_maker.py --config.*account1"
```

### 手动测试
```bash
# 手动运行测试（前台运行，便于调试）
cd /root/standx
source .venv/bin/activate
python market_maker.py --config .env.account1 --log-prefix account1
```
