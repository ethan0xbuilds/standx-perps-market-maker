"""
双向限价单做市策略
- 同时挂买单和卖单
- 监控价格变化
- 订单偏离超过阈值时取消并重新挂单
"""

import asyncio
import os
import time
import signal
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from adapter import standx_adapter
from adapter.standx_adapter import StandXAdapter
from api.ws_client import StandXMarketStream
from standx_auth import StandXAuth
import standx_api as api
from price_providers import create_price_provider
from notifier import Notifier
from logger import get_logger

logger = get_logger(__name__)

load_dotenv()


class MarketMaker:
    """双向限价单做市器"""

    def __init__(
        self,
        auth: StandXAuth,
        symbol: str,
        qty: str,
        target_bps: float = 7.5,
        min_bps: float = 7.0,
        max_bps: float = 10,
        balance_threshold_1: float = 100.0,
        balance_threshold_2: float = 50.0,
        force_degraded_on_us_open: bool = False,
        notifier: Notifier = None,
        exchange_adapter: StandXAdapter = None,
    ):
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
            force_degraded_on_us_open: 美股开盘时间是否强制降级模式2（默认False）
            notifier: 通知器实例（可选，默认从环境变量创建）
        """
        self.auth = auth
        self.symbol = symbol
        self.qty = qty
        self.exchange_adapter = exchange_adapter

        # 通知器
        self.notifier = notifier or Notifier.from_env()
        # 订单重挂通知限流（秒），可通过环境变量调整，默认 3600 秒（1 小时）
        self.reorder_throttle_seconds = int(
            os.getenv("REORDER_NOTIFY_THROTTLE_SECONDS", "3600")
        )

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

        self.leverage = 40  # 杠杆倍数
        self.margin_mode = "isolated"  # 单仓模式

        # 优雅关闭相关
        self._shutdown_requested = False

    def _setup_signal_handlers(self):
        """设置信号处理器以支持优雅关闭"""

        def handle_signal(signum, frame):
            logger.info("收到信号 %s，准备优雅关闭...", signum)
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
            logger.warning("美股开盘时间判断失败: %s", e)
            return False

    async def check_and_update_mode(self) -> bool:
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

            # 检查美股开盘时段（如果启用）
            if self.force_degraded_on_us_open and self._is_us_market_open():
                new_mode = "degraded_2"
                self.target_bps = 80
                self.min_bps = 70
                self.max_bps = 95
                reason = "美股开盘时段（09:30-16:00 美东时间）"
            else:
                new_mode = "normal"
                reason = "非美股开盘时段"

            # 模式变化时打印日志并通知
            if new_mode != old_mode:
                self.current_mode = new_mode
                mode_names = {
                    "normal": "正常模式",
                    "degraded_2": "降级模式2-止损",
                }
                logger.info(
                    "模式切换 %s → %s",
                    mode_names.get(old_mode, old_mode),
                    mode_names.get(new_mode, new_mode),
                )
                logger.info("原因: %s", reason)
                logger.info(
                    "新挂单策略: target=%s bps, 范围=[%s, %s]",
                    self.target_bps,
                    self.min_bps,
                    self.max_bps,
                )

                # 发送通知
                notify_msg = (
                    f"交易对: `{self.symbol}`\n"
                    f"{mode_names.get(old_mode, old_mode)} → {mode_names.get(new_mode, new_mode)}\n\n"
                    f"原因: {reason}\n"
                    f"新策略: target={self.target_bps} bps, 范围=[{self.min_bps}, {self.max_bps}]"
                )
                await self.notifier.send(notify_msg)

                return True

            return False

        except Exception as e:
            logger.exception("模式更新失败: %s，使用当前模式继续", e)
            return False

    def get_current_price(self) -> float:
        """获取当前市场价格（通过配置的价格提供者）"""
        try:
            return self.price_provider.get_current_price()
        except Exception as e:
            logger.warning("获取价格失败: %s，将在下次迭代重试", e)
            raise

    async def close_position(self, market_price: float) -> bool:
        """
        平仓所有持仓（市价单）

        Args:
            market_price: 当前市场价格

        Returns:
            True if closed successfully, False otherwise
        """
        try:
            positions = await api.query_positions(self.auth, symbol=self.symbol)
            if not positions:
                return True

            position = positions[0]
            qty_str = position.get("qty")
            side = position.get("side")  # 可能为 None
            margin_mode = position.get("margin_mode")
            leverage = (
                int(position.get("leverage")) if position.get("leverage") else None
            )

            # 打印持仓详情便于调试
            logger.debug(
                "持仓详情: qty=%s, side=%s, margin_mode=%s, leverage=%s",
                qty_str,
                side,
                margin_mode,
                leverage,
            )

            if not qty_str or float(qty_str) == 0:
                logger.info("持仓数量为 0，无需平仓")
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
                logger.info("持仓数量为 0，无需平仓")
                return True

            logger.info("检测到持仓，立即平仓: %s %s", close_side, qty_send)

            close_resp = await api.new_market_order(
                self.auth,
                symbol=self.symbol,
                side=close_side,
                qty=qty_send,
                reduce_only=True,
                margin_mode=margin_mode,
                leverage=leverage,
                time_in_force="ioc",
            )

            logger.info(
                "平仓请求已提交 (request_id: %s)，验证中...",
                close_resp.get("request_id"),
            )

            # 验证：轮询持仓是否已归零（最多30秒）
            start = time.time()
            while time.time() - start < 30:
                await asyncio.sleep(1)
                latest_positions = await api.query_positions(
                    self.auth, symbol=self.symbol
                )
                if not latest_positions:
                    logger.info("持仓已清空")
                    return True
                latest_qty = float(latest_positions[0].get("qty") or 0)
                if latest_qty == 0:
                    logger.info("持仓数量为 0（已平仓）")
                    # 平仓成功通知
                    await self.notifier.send(
                        f"*平仓成功*\n"
                        f"交易对: `{self.symbol}`\n"
                        f"数量: {qty_str}\n"
                        f"方向: {close_side}"
                    )
                    return True

            logger.warning("超时：持仓仍未归零，稍后会在下一轮重试")
            # 平仓超时通知
            await self.notifier.send(
                f"*平仓超时*\n"
                f"交易对: `{self.symbol}`\n"
                f"数量: {qty_str}\n"
                f"持仓仍未归零，下一轮重试"
            )
            return False
        except Exception as e:
            logger.exception("平仓失败: %s", e)
            # 平仓失败通知
            await self.notifier.send(
                f"*平仓失败*\n" f"交易对: `{self.symbol}`\n" f"错误: {e}"
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

    async def place_orders(self, market_price: float):
        """下双向限价单"""
        buy_price, sell_price = self.calculate_order_prices(market_price)

        logger.info("下双向限价单 (市价: %.2f)", market_price)

        # 下买单
        try:
            buy_resp = await api.new_limit_order(
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
            logger.info(
                "买单: %s @ %.2f (request_id: %s)",
                self.qty,
                buy_price,
                buy_resp.get("request_id"),
            )
        except Exception as e:
            logger.exception("买单失败: %s", e)

        # 下卖单
        try:
            sell_resp = await api.new_limit_order(
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
            logger.info(
                "卖单: %s @ %.2f (request_id: %s)",
                self.qty,
                sell_price,
                sell_resp.get("request_id"),
            )
        except Exception as e:
            logger.exception("卖单失败: %s", e)

        # 等待订单生效（优化为1秒）
        await asyncio.sleep(1)

    async def refresh_orders(self):
        """刷新当前订单状态"""
        try:
            open_orders = await api.query_open_orders(self.auth, symbol=self.symbol)
            orders = open_orders.get("result", [])

            self.buy_orders = []
            self.sell_orders = []

            for order in orders:
                if order["side"] == "buy":
                    self.buy_orders.append(order)
                elif order["side"] == "sell":
                    self.sell_orders.append(order)
        except Exception as e:
            logger.warning("刷新订单状态失败: %s", e)

    async def cancel_all_orders(self):
        """取消所有订单"""
        for order in self.exchange_adapter._orders:
            try:
                cancel_resp = await api.cancel_order(self.auth, order_id=order["id"])
                logger.info("取消 %s 订单 @ %s", order["side"], order["price"])
            except Exception as e:
                logger.exception("取消失败: %s", e)

    async def run(self, check_interval: float = 0.5):
        """
        运行做市策略（无限运行）

        Args:
            check_interval: 检查间隔（秒，默认0.5秒）
        """
        beijing_tz = ZoneInfo("Asia/Shanghai")
        beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        logger.info("双向限价单做市策略启动 - %s", beijing_time)
        logger.info("交易对: %s", self.symbol)
        logger.info("订单数量: %s", self.qty)
        logger.info("余额阈值1（手续费容忍）: %s USDT", self.balance_threshold_1)
        logger.info("余额阈值2（止损）: %s USDT", self.balance_threshold_2)
        logger.info("检查间隔: %s 秒", check_interval)

        # 启动通知
        await self.notifier.send(
            f"*做市策略启动*\n"
            f"时间: {beijing_time}\n"
            f"交易对: `{self.symbol}`\n"
            f"数量: {self.qty}\n"
            f"阈值: {self.balance_threshold_1}/{self.balance_threshold_2} USDT"
        )

        # 初始化：检查余额并确定模式
        logger.info("检查余额并确定运行模式...")
        await self.check_and_update_mode()
        logger.info("当前模式: %s", self.current_mode)
        logger.info(
            "挂单策略: target=%s bps, 范围=[%s, %s]",
            self.target_bps,
            self.min_bps,
            self.max_bps,
        )

        # 等待 mid_price 数据就绪（只执行一次）
        while self.exchange_adapter.get_depth_mid_price() is None:
            logger.info("等待行情数据（mid_price）...")
            await asyncio.sleep(0.2)

        # 监控循环
        try:
            while True:
                # 检查是否收到关闭信号
                if self._shutdown_requested:
                    logger.info("收到关闭信号，停止策略")
                    break

                # 第1步：检查持仓，存在则平仓
                positions = self.exchange_adapter.get_position()
                if positions:
                    position = positions[0]
                    qty = position.get("qty")
                    if qty and float(qty) != 0:
                        logger.info("检测到持仓 (qty=%s)，立即平仓...", qty)
                        try:
                            await self.close_position(
                                self.exchange_adapter._depth_mid_price
                            )
                            # 平仓后检查余额并更新模式
                            await self.check_and_update_mode()
                        except Exception as e:
                            logger.exception("平仓失败: %s，下次迭代重试...", e)
                        continue

                # 第2步：检查订单状态和偏离度
                need_replace = False
                reason = ""

                # 检查订单数量是否正确
                if (
                    self.exchange_adapter.get_buy_order_count() != 1
                    or self.exchange_adapter.get_sell_order_count() != 1
                ):
                    logger.info(
                        "订单数量异常，买单: %d, 卖单: %d",
                        self.exchange_adapter.get_buy_order_count(),
                        self.exchange_adapter.get_sell_order_count(),
                    )
                    reason = "订单数量异常（非各1单）"
                    logger.info("订单需重挂，原因: %s", reason)
                    need_replace = True

                if (
                    self.exchange_adapter.get_buy_orders()
                    and self.exchange_adapter.get_sell_orders()
                    and not self.exchange_adapter.is_price_updated_and_processed()
                ):
                    buy_price = float(
                        self.exchange_adapter.get_buy_orders()[0]["price"]
                    )
                    buy_bps = abs(
                        (self.exchange_adapter.get_depth_mid_price() - buy_price)
                        / self.exchange_adapter.get_depth_mid_price()
                        * 10000
                    )
                    sell_price = float(
                        self.exchange_adapter.get_sell_orders()[0]["price"]
                    )
                    sell_bps = abs(
                        (sell_price - self.exchange_adapter.get_depth_mid_price())
                        / self.exchange_adapter.get_depth_mid_price()
                        * 10000
                    )
                    logger.info(
                        "买单: %.2f (偏离: %.1f bps), 卖单: %.2f (偏离: %.1f bps)",
                        buy_price,
                        buy_bps,
                        sell_price,
                        sell_bps,
                    )
                    if (
                        buy_bps < self.min_bps
                        or buy_bps > self.max_bps
                        or sell_bps < self.min_bps
                        or sell_bps > self.max_bps
                    ):
                        need_replace = True
                        reason = f"订单偏离范围异常（买单: {buy_bps:.1f} bps, 卖单: {sell_bps:.1f} bps）"
                    self.exchange_adapter.mark_price_processed()

                if need_replace:
                    logger.info("订单需重挂，原因: %s", reason)
                    await self.cancel_all_orders()
                    await self.place_orders(self.exchange_adapter.get_depth_mid_price())

                    # 订单重挂通知：按原因前缀（冒号前）去重 1 小时
                    reason_key = (reason or "reorder").split(":", 1)[0].strip()
                    notify_msg = (
                        f"*订单重挂*\n"
                        f"交易对: `{self.symbol}`\n"
                        f"市价: {self.exchange_adapter.get_depth_mid_price():.2f}\n"
                        f"原因: {reason}"
                    )
                    # 使用 Notifier 的限流（相同 reason_key 在窗口内只发一次）
                    await self.notifier.send(notify_msg, throttle_key=reason_key)
                
                # 等待下一个检查周期
                await asyncio.sleep(check_interval)

        except KeyboardInterrupt:
            logger.info("收到中断信号，停止策略...")
            await self.notifier.send(
                f"*策略停止*\n" f"交易对: `{self.symbol}`\n" f"原因: 收到中断信号"
            )
        except Exception as e:
            logger.exception("策略运行出现严重错误: %s", e)
            logger.info("正在清理订单并退出...")
            await self.notifier.send(
                f"*致命异常*\n" f"交易对: `{self.symbol}`\n" f"错误: {e}"
            )

        # 清理：取消所有订单
        logger.info("清理所有订单...")
        await self.cleanup()

        logger.info("策略已停止")

        # 停止通知
        await self.notifier.send(
            f"*做市策略已停止*\n" f"交易对: `{self.symbol}`\n" f"订单已清理完成"
        )

    async def cleanup(self):
        """清理所有订单和资源"""
        orders_to_cancel = self.exchange_adapter._orders

        for order in orders_to_cancel:
            try:
                cancel_resp = await api.cancel_order(self.auth, order_id=order["id"])
                logger.info("取消 %s 订单: %s", order["side"], order["cl_ord_id"])
            except Exception as e:
                logger.exception("取消失败: %s", e)


async def main():
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
    force_degraded_on_us_open = (
        os.getenv("MARKET_MAKER_FORCE_DEGRADED_ON_US_OPEN", "false").lower() == "true"
    )

    # 认证
    logger.info("认证中...")
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
        logger.info("认证成功")
    except Exception as e:
        notifier.send(f"❌ *认证失败*\n" f"交易对: `{symbol}`\n" f"错误: {e}")
        raise

    # 创建 StandX 适配器
    standx_adapter = StandXAdapter()
    # 订阅depth_book频道
    await standx_adapter.subscribe_depth_book(symbol="BTC-USD")
    # 认证并订阅order频道
    await standx_adapter.authenticate_and_subscribe_orders()

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
        force_degraded_on_us_open=force_degraded_on_us_open,
        notifier=notifier,
        exchange_adapter=standx_adapter,
    )
    # 启动做市和 WebSocket 监听为并发任务
    maker_task = asyncio.create_task(market_maker.run(check_interval=check_interval))
    # 其他需要常驻的异步任务也用 create_task
    await maker_task

    logger.info(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    await asyncio.sleep(5)  # 等待初始数据
    logger.info(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    await standx_adapter.market_stream.close()


if __name__ == "__main__":
    asyncio.run(main())
