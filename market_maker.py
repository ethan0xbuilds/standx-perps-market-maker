"""
双向限价单做市策略
- 同时挂买单和卖单
- 监控价格变化
- 订单偏离超过阈值时取消并重新挂单
"""

import os
import sys
import time
import signal
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from standx_auth import StandXAuth
import standx_api as api
from price_providers import create_price_provider, PriceProvider
from notifier import Notifier

load_dotenv()


class MarketMaker:
    """双向限价单做市器"""
    
    def __init__(self, auth: StandXAuth, symbol: str, qty: str, target_bps: float = 7.5, min_bps: float = 7.0, max_bps: float = 10, 
                 balance_threshold_1: float = 100.0, balance_threshold_2: float = 50.0, price_source: str = "http", 
                 force_degraded_on_us_open: bool = False, notifier: Notifier = None):
        """
        初始化做市器
        
        Args:
            auth: 认证后的StandXAuth实例
            symbol: 交易对
            qty: 订单数量（字符串格式）
            target_bps: 目标挂单偏离（basis points，默认7.5，用于初始下单）
            min_bps: 最小允许偏离（默认7.0，低于此值重新挂单）
            max_bps: 最大允许偏离（默认10，超过此值重新挂单）
            balance_threshold_1: 余额阈值1-手续费容忍阈值（默认100 USDT，低于此进入降级模式1）
            balance_threshold_2: 余额阈值2-止损阈值（默认50 USDT，低于此进入降级模式2）
            price_source: 价格数据源（"http" 或 "websocket"，默认 "http"）
            force_degraded_on_us_open: 美股开盘时间是否强制降级模式2（默认False）
            notifier: 通知器实例（可选，默认从环境变量创建）
        """
        self.auth = auth
        self.symbol = symbol
        self.qty = qty
        
        # 通知器
        self.notifier = notifier or Notifier.from_env()
        
        # 创建价格提供者
        self.price_provider = create_price_provider(price_source, auth, symbol)
        self.price_source = price_source
        
        # 原始配置（正常模式）
        self.default_target_bps = target_bps
        self.default_min_bps = min_bps
        self.default_max_bps = max_bps
        
        # 当前生效的配置（会根据余额动态调整）
        self.target_bps = target_bps
        self.min_bps = min_bps
        self.max_bps = max_bps
        
        # 余额降级阈值
        self.balance_threshold_1 = balance_threshold_1
        self.balance_threshold_2 = balance_threshold_2
        
        # 美股开盘时段强制降级开关
        self.force_degraded_on_us_open = force_degraded_on_us_open
        
        # 当前模式："normal", "degraded_1", "degraded_2"
        self.current_mode = "normal"
        
        # 获取持仓配置
        positions = api.query_positions(auth, symbol=symbol)
        position = positions[0] if positions else None
        self.leverage = int(position["leverage"]) if position else 40
        self.margin_mode = position["margin_mode"] if position else "cross"
        
        # 当前订单
        self.buy_order = None
        self.sell_order = None
        
        # 订单重挂通知限流（5分钟窗口）
        self._last_reorder_notify_time = 0
        self._reorder_count_since_notify = 0
        
        # 优雅关闭相关
        self._shutdown_requested = False
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        """设置信号处理器以支持优雅关闭"""
        def handle_signal(signum, frame):
            print(f"\n🛑 收到信号 {signum}，准备优雅关闭...")
            self._shutdown_requested = True
        
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    
    def _is_us_market_open(self) -> bool:
        """判断当前是否美股开盘时间（美东时间 09:30-16:15，周一-周五，包含收盘后15分钟缓冲）"""
        try:
            # 获取美东时间（EST/EDT，自动处理冬夏令时）
            eastern = ZoneInfo("America/New_York")
            now = datetime.now(eastern)
            
            # 检查是否工作日（0=周一，6=周日）
            if now.weekday() >= 5:  # 周六、周日
                return False
            
            # 检查是否在 09:30-16:15 之间（包含收盘后15分钟缓冲，应对BTC波动）
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=15, second=0, microsecond=0)
            
            return market_open <= now < market_close
        except Exception as e:
            print(f"  ⚠️ 美股开盘时间判断失败: {e}")
            return False
    
    def check_and_update_mode(self) -> bool:
        """
        检查余额并更新做市模式
        优先检查美股开盘时段，其次检查余额
        
        Returns:
            True if mode changed, False otherwise
        """
        try:
            old_mode = self.current_mode
            reason = ""
            new_mode = "normal"
            
            # 第1步：优先检查美股开盘时段（如果启用）
            if self.force_degraded_on_us_open and self._is_us_market_open():
                new_mode = "degraded_2"
                self.target_bps = 80
                self.min_bps = 70
                self.max_bps = 95
                reason = "美股开盘时段（09:30-16:00 美东时间）"
            else:
                # 第2步：检查余额判断模式
                balance_data = api.query_balance(self.auth)
                print(f"  🔍 余额查询响应: {balance_data}")
                
                total_balance = float(balance_data.get("balance") or balance_data.get("equity") or 0)
                
                if total_balance < self.balance_threshold_2:
                    new_mode = "degraded_2"
                    self.target_bps = 80
                    self.min_bps = 70
                    self.max_bps = 95
                    reason = f"余额过低: {total_balance:.2f} USDT"
                elif total_balance < self.balance_threshold_1:
                    new_mode = "degraded_1"
                    self.target_bps = 25
                    self.min_bps = 20
                    self.max_bps = 29.5
                    reason = f"余额偏低: {total_balance:.2f} USDT"
                else:
                    new_mode = "normal"
                    self.target_bps = self.default_target_bps
                    self.min_bps = self.default_min_bps
                    self.max_bps = self.default_max_bps
                    reason = f"余额充足: {total_balance:.2f} USDT"
            
            # 模式变化时打印日志并通知
            if new_mode != old_mode:
                self.current_mode = new_mode
                mode_names = {
                    "normal": "正常模式",
                    "degraded_1": "降级模式1-手续费容忍",
                    "degraded_2": "降级模式2-止损"
                }
                beijing_tz = ZoneInfo("Asia/Shanghai")
                beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n🔄 模式切换 [{beijing_time}]: {mode_names.get(old_mode, old_mode)} → {mode_names.get(new_mode, new_mode)}")
                print(f"   原因: {reason}")
                print(f"   新挂单策略: target={self.target_bps} bps, 范围=[{self.min_bps}, {self.max_bps}]")
                
                # 发送通知
                notify_msg = (
                    f"🔄 *模式切换* [{beijing_time}]\n"
                    f"交易对: `{self.symbol}`\n"
                    f"{mode_names.get(old_mode, old_mode)} → {mode_names.get(new_mode, new_mode)}\n\n"
                    f"原因: {reason}\n"
                    f"新策略: target={self.target_bps} bps, 范围=[{self.min_bps}, {self.max_bps}]"
                )
                self.notifier.send(notify_msg)
                
                return True
            
            return False
            
        except Exception as e:
            print(f"  ⚠️ 模式更新失败: {e}，使用当前模式继续")
            return False
        
    def get_current_price(self) -> float:
        """获取当前市场价格（通过配置的价格提供者）"""
        try:
            return self.price_provider.get_current_price()
        except Exception as e:
            print(f"  ⚠️ 获取价格失败: {e}，将在下次迭代重试")
            raise
    
    def close_position(self, market_price: float) -> bool:
        """
        平仓所有持仓（市价单）
        
        Args:
            market_price: 当前市场价格
            
        Returns:
            True if closed successfully, False otherwise
        """
        try:
            positions = api.query_positions(self.auth, symbol=self.symbol)
            if not positions:
                return True
            
            position = positions[0]
            qty_str = position.get("qty")
            side = position.get("side")  # 可能为 None
            margin_mode = position.get("margin_mode")
            leverage = int(position.get("leverage")) if position.get("leverage") else None
            
            # 打印持仓详情便于调试
            print(f"  📍 持仓详情: qty={qty_str}, side={side}, margin_mode={margin_mode}, leverage={leverage}")
            
            if not qty_str or float(qty_str) == 0:
                print(f"  ⚠️ 持仓数量为 0，无需平仓")
                return True
            
            qty_f = float(qty_str)
            
            # 判断平仓方向：StandX API 可能不返回 side 字段，需通过 qty 正负判断
            if qty_f > 0:
                # qty > 0 通常表示多头 (buy)，平仓用 sell
                close_side = "sell"
                qty_send = qty_str
            elif qty_f < 0:
                # qty < 0 通常表示空头 (sell)，平仓用 buy
                close_side = "buy"
                qty_send = f"{abs(qty_f):.4f}"
            else:
                print(f"  ⚠️ 持仓数量为 0，无需平仓")
                return True
            
            print(f"\n💰 检测到持仓，立即平仓: {close_side} {qty_send}")
            
            close_resp = api.new_market_order(
                self.auth,
                symbol=self.symbol,
                side=close_side,
                qty=qty_send,
                reduce_only=True,
                margin_mode=margin_mode,
                leverage=leverage,
                time_in_force="ioc",
            )
            
            print(f"  ✅ 平仓请求已提交 (request_id: {close_resp.get('request_id')})，验证中...")
            
            # 验证：轮询持仓是否已归零（最多30秒）
            start = time.time()
            while time.time() - start < 30:
                time.sleep(1)
                latest_positions = api.query_positions(self.auth, symbol=self.symbol)
                if not latest_positions:
                    print("  ✅ 持仓已清空")
                    return True
                latest_qty = float(latest_positions[0].get("qty") or 0)
                if latest_qty == 0:
                    print("  ✅ 持仓数量为 0（已平仓）")
                    # 平仓成功通知
                    self.notifier.send(
                        f"✅ *平仓成功*\n"
                        f"交易对: `{self.symbol}`\n"
                        f"数量: {qty_str}\n"
                        f"方向: {close_side}"
                    )
                    return True
            
            print("  ⚠️ 超时：持仓仍未归零，稍后会在下一轮重试")
            # 平仓超时通知
            self.notifier.send(
                f"⚠️ *平仓超时*\n"
                f"交易对: `{self.symbol}`\n"
                f"数量: {qty_str}\n"
                f"持仓仍未归零，下一轮重试"
            )
            return False
        except Exception as e:
            print(f"  ⚠️ 平仓失败: {e}")
            # 平仓失败通知
            self.notifier.send(
                f"❌ *平仓失败*\n"
                f"交易对: `{self.symbol}`\n"
                f"错误: {e}"
            )
            return False
    
    def calculate_order_prices(self, market_price: float) -> tuple:
        """
        计算双向订单价格
        
        Args:
            market_price: 当前市场价格
            
        Returns:
            (buy_price, sell_price) 买单价格和卖单价格
        """
        buy_price = market_price * (1 - self.target_bps / 10000)
        sell_price = market_price * (1 + self.target_bps / 10000)
        return (buy_price, sell_price)
    
    def place_orders(self, market_price: float):
        """下双向限价单"""
        buy_price, sell_price = self.calculate_order_prices(market_price)
        
        print(f"\n📝 下双向限价单 (市价: {market_price:.2f}):")
        
        # 下买单
        try:
            buy_resp = api.new_limit_order(
                self.auth,
                symbol=self.symbol,
                side="buy",
                qty=self.qty,
                price=f"{buy_price:.2f}",
                time_in_force="alo",
                reduce_only=False,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )
            print(f"  ✅ 买单: {self.qty} @ {buy_price:.2f} (request_id: {buy_resp.get('request_id')})")
        except Exception as e:
            print(f"  ❌ 买单失败: {e}")
        
        # 下卖单
        try:
            sell_resp = api.new_limit_order(
                self.auth,
                symbol=self.symbol,
                side="sell",
                qty=self.qty,
                price=f"{sell_price:.2f}",
                time_in_force="alo",
                reduce_only=False,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )
            print(f"  ✅ 卖单: {self.qty} @ {sell_price:.2f} (request_id: {sell_resp.get('request_id')})")
        except Exception as e:
            print(f"  ❌ 卖单失败: {e}")
        
        # 等待订单生效（优化为1秒）
        time.sleep(1)
        self.refresh_orders()
    
    def refresh_orders(self):
        """刷新当前订单状态"""
        try:
            open_orders = api.query_open_orders(self.auth, symbol=self.symbol)
            orders = open_orders.get("result", [])
            
            self.buy_order = None
            self.sell_order = None
            
            for order in orders:
                if order["side"] == "buy":
                    self.buy_order = order
                elif order["side"] == "sell":
                    self.sell_order = order
        except Exception as e:
            print(f"  ⚠️ 刷新订单状态失败: {e}")
            # 不抛出异常，使用上次缓存的订单状态
    
    def cancel_all_orders(self):
        """取消所有订单"""
        orders_to_cancel = []
        if self.buy_order:
            orders_to_cancel.append(self.buy_order)
        if self.sell_order:
            orders_to_cancel.append(self.sell_order)
        
        for order in orders_to_cancel:
            try:
                cancel_resp = api.cancel_order(self.auth, order_id=order["id"])
                print(f"  ✅ 取消 {order['side']} 订单 @ {order['price']}")
            except Exception as e:
                print(f"  ❌ 取消失败: {e}")
    
    def run(self, check_interval: float = 0.5):
        """
        运行做市策略（无限运行）
        
        Args:
            check_interval: 检查间隔（秒，默认0.5秒）
        """
        beijing_tz = ZoneInfo("Asia/Shanghai")
        beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        print("=" * 60)
        print(f"双向限价单做市策略启动 - {beijing_time}")
        print("=" * 60)
        print(f"交易对: {self.symbol}")
        print(f"订单数量: {self.qty}")
        print(f"价格数据源: {self.price_source.upper()}")
        print(f"余额阈值1（手续费容忍）: {self.balance_threshold_1} USDT")
        print(f"余额阈值2（止损）: {self.balance_threshold_2} USDT")
        print(f"检查间隔: {check_interval}秒")
        print("=" * 60)
        
        # 启动通知
        self.notifier.send(
            f"🚀 *做市策略启动*\n"
            f"时间: {beijing_time}\n"
            f"交易对: `{self.symbol}`\n"
            f"数量: {self.qty}\n"
            f"价格源: {self.price_source.upper()}\n"
            f"阈值: {self.balance_threshold_1}/{self.balance_threshold_2} USDT"
        )
        
        # 初始化：检查余额并确定模式
        print(f"\n🔍 检查余额并确定运行模式...")
        self.check_and_update_mode()
        print(f"   当前模式: {self.current_mode}")
        print(f"   挂单策略: target={self.target_bps} bps, 范围=[{self.min_bps}, {self.max_bps}]")
        
        # 监控循环
        try:
            while True:
                # 检查是否收到关闭信号
                if self._shutdown_requested:
                    print(f"\n⏰ 收到关闭信号，停止策略")
                    break
                
                # 等待检查间隔
                time.sleep(check_interval)
                
                # 获取当前价格（容错处理）
                try:
                    market_price = self.get_current_price()
                except Exception as e:
                    print(f"  ⚠️ 跳过本次迭代，继续监控...")
                    continue
                
                # 获取北京时间
                beijing_tz = ZoneInfo("Asia/Shanghai")
                beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
                
                print(f"\n市价: {market_price:.2f} (北京时间: {beijing_time})")
                
                # 第1步：检查持仓，存在则平仓
                positions = api.query_positions(self.auth, symbol=self.symbol)
                if positions:
                    position = positions[0]
                    qty = position.get("qty")
                    if qty and float(qty) != 0:
                        print(f"\n💰 检测到持仓 (qty={qty})，立即平仓...")
                        try:
                            self.close_position(market_price)
                            # 平仓后检查余额并更新模式
                            self.check_and_update_mode()
                        except Exception as e:
                            print(f"  ⚠️ 平仓失败: {e}，下次迭代重试...")
                        continue
                
                # 第2步：检查订单状态和偏离度
                self.refresh_orders()
                need_replace = False
                reason = ""
                
                # 检查买单
                if not self.buy_order:
                    need_replace = True
                    reason = "缺少买单"
                else:
                    buy_price = float(self.buy_order["price"])
                    buy_bps = abs((market_price - buy_price) / market_price * 10000)
                    print(f"  📗 买单: {buy_price:.2f} (偏离: {buy_bps:.1f} bps)")
                    if buy_bps < self.min_bps or buy_bps > self.max_bps:
                        need_replace = True
                        reason = f"买单偏离范围: {buy_bps:.1f} bps 不在 [{self.min_bps}, {self.max_bps}]"
                
                # 检查卖单
                if not self.sell_order:
                    need_replace = True
                    reason = "缺少卖单" if not need_replace else reason
                else:
                    sell_price = float(self.sell_order["price"])
                    sell_bps = abs((sell_price - market_price) / market_price * 10000)
                    print(f"  📕 卖单: {sell_price:.2f} (偏离: {sell_bps:.1f} bps)")
                    if sell_bps < self.min_bps or sell_bps > self.max_bps:
                        need_replace = True
                        reason = f"卖单偏离范围: {sell_bps:.1f} bps 不在 [{self.min_bps}, {self.max_bps}]" if not need_replace else reason
                
                # 如果需要重新下单
                if need_replace:
                    print(f"\n🚨 {reason}，取消所有订单并重新挂单...")
                    self.cancel_all_orders()
                    time.sleep(1)
                    self.check_and_update_mode()
                    self.place_orders(market_price)
                    
                    # 订单重挂通知（5分钟限流）
                    self._reorder_count_since_notify += 1
                    now_ts = time.time()
                    throttle_window = 300  # 5分钟
                    
                    if now_ts - self._last_reorder_notify_time > throttle_window:
                        notify_msg = (
                            f"📝 *订单重挂*\n"
                            f"交易对: `{self.symbol}`\n"
                            f"过去 {throttle_window//60} 分钟内共 {self._reorder_count_since_notify} 次\n\n"
                            f"市价: {market_price:.2f}\n"
                            f"原因: {reason}"
                        )
                        self.notifier.send(notify_msg)
                        self._last_reorder_notify_time = now_ts
                        self._reorder_count_since_notify = 0
                    
                    continue
                
        except KeyboardInterrupt:
            print(f"\n\n⚠️ 收到中断信号，停止策略...")
            self.notifier.send(
                f"⚠️ *策略停止*\n"
                f"交易对: `{self.symbol}`\n"
                f"原因: 收到中断信号"
            )
        except Exception as e:
            print(f"\n\n❌ 策略运行出现严重错误: {e}")
            print(f"   正在清理订单并退出...")
            self.notifier.send(
                f"❌ *致命异常*\n"
                f"交易对: `{self.symbol}`\n"
                f"错误: {e}"
            )
        
        # 清理：取消所有订单
        print(f"\n🧹 清理所有订单...")
        self.cleanup()
        
        print(f"\n" + "=" * 60)
        print("策略已停止")
        print("=" * 60)
        
        # 停止通知
        self.notifier.send(
            f"🛑 *做市策略已停止*\n"
            f"交易对: `{self.symbol}`\n"
            f"订单已清理完成"
        )
    
    def cleanup(self):
        """清理所有订单和资源"""
        self.refresh_orders()
        orders_to_cancel = []
        
        if self.buy_order:
            orders_to_cancel.append(self.buy_order)
        if self.sell_order:
            orders_to_cancel.append(self.sell_order)
        
        for order in orders_to_cancel:
            try:
                cancel_resp = api.cancel_order(self.auth, order_id=order["id"])
                print(f"  ✅ 取消 {order['side']} 订单: {order['cl_ord_id']}")
            except Exception as e:
                print(f"  ❌ 取消失败: {e}")
        
        # 清理价格提供者资源（如 WebSocket 连接）
        self.price_provider.cleanup()


def main():
    """主函数"""
    
    # 加载配置
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    ed25519_key = os.getenv("ED25519_PRIVATE_KEY")
    symbol = os.getenv("MARKET_MAKER_SYMBOL", "BTC-USD")
    qty = os.getenv("MARKET_MAKER_QTY", "0.005")
    target_bps = float(os.getenv("MARKET_MAKER_TARGET_BPS", "7.5"))
    min_bps = float(os.getenv("MARKET_MAKER_MIN_BPS", "7.0"))
    max_bps = float(os.getenv("MARKET_MAKER_MAX_BPS", "10"))
    
    # 余额降级阈值
    balance_threshold_1 = float(os.getenv("MARKET_MAKER_BALANCE_THRESHOLD_1", "100"))
    balance_threshold_2 = float(os.getenv("MARKET_MAKER_BALANCE_THRESHOLD_2", "50"))
    
    # 监控间隔
    check_interval = float(os.getenv("MARKET_MAKER_CHECK_INTERVAL", "0.0"))
    
    # 价格数据源
    price_source = os.getenv("MARKET_MAKER_PRICE_SOURCE", "http").lower()
    
    # 美股开盘时段强制降级
    force_degraded_on_us_open = os.getenv("MARKET_MAKER_FORCE_DEGRADED_ON_US_OPEN", "false").lower() == "true"
    
    # 认证
    print("🔐 认证中...")
    token = os.getenv("ACCESS_TOKEN")  # Optional access token for scheme 2
    
    # Distinguish between two schemes
    if private_key and not ed25519_key and not token:
        # Scheme 1: Wallet-based auth (ED25519_PRIVATE_KEY and ACCESS_TOKEN should be empty)
        auth = StandXAuth(private_key, ed25519_key=None, token=None)
    elif not private_key and ed25519_key and token:
        # Scheme 2: Token-based auth (WALLET_PRIVATE_KEY should be empty)
        auth = StandXAuth(private_key=None, ed25519_key=ed25519_key, token=token)
    else:
        # Invalid configuration
        raise ValueError(
            "❌ 认证配置错误\n"
            f"   当前配置: WALLET_PRIVATE_KEY={'✓' if private_key else '✗'}, "
            f"ED25519_PRIVATE_KEY={'✓' if ed25519_key else '✗'}, "
            f"ACCESS_TOKEN={'✓' if token else '✗'}\n"
            "   请选择其中一种方案：\n"
            "   方案1: 仅设置 WALLET_PRIVATE_KEY（系统自动生成 ED25519 密钥）\n"
            "   方案2: 仅设置 ED25519_PRIVATE_KEY + ACCESS_TOKEN（WALLET_PRIVATE_KEY 应为空）"
        )
    
    # 初始化通知器（在认证前，方便发送认证失败通知）
    notifier = Notifier.from_env()
    
    try:
        auth.authenticate()
        print("✅ 认证成功\n")
    except Exception as e:
        notifier.send(
            f"❌ *认证失败*\n"
            f"交易对: `{symbol}`\n"
            f"错误: {e}"
        )
        raise
    
    # 创建做市器
    market_maker = MarketMaker(
        auth=auth,
        symbol=symbol,
        qty=qty,
        target_bps=target_bps,
        min_bps=min_bps,
        max_bps=max_bps,
        balance_threshold_1=balance_threshold_1,
        balance_threshold_2=balance_threshold_2,
        price_source=price_source,
        force_degraded_on_us_open=force_degraded_on_us_open,
        notifier=notifier,
    )
    
    # 运行策略
    market_maker.run(check_interval=check_interval)


if __name__ == "__main__":
    main()
