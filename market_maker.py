"""
双向限价单做市策略
- 同时挂买单和卖单
- 监控价格变化
- 订单偏离超过阈值时取消并重新挂单
"""

# 首先加载环境变量，必须在其他模块导入之前
from dotenv import load_dotenv
load_dotenv()

# 标准库导入
import asyncio
import os
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

# 本地模块导入
from adapter.standx_adapter import StandXAdapter
from standx_auth import StandXAuth
import standx_api as api
from notifier import Notifier
from logger import get_logger

logger = get_logger(__name__)


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

        # 挂单参数（静态）
        self.target_bps = target_bps
        self.min_bps = min_bps
        self.max_bps = max_bps

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

    def check_order_count(self) -> tuple[bool, str]:
        """
        检查订单数量是否正确
        
        Returns:
            (need_replace, reason) 是否需要重挂和原因
        """
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
            return True, reason
        return False, ""

    def check_price_deviation(self) -> tuple[bool, str]:
        """
        检查订单偏离度是否超过阈值
        
        Returns:
            (need_replace, reason) 是否需要重挂和原因
        """
        if not (
            self.exchange_adapter.get_buy_orders()
            and self.exchange_adapter.get_sell_orders()
            and not self.exchange_adapter.is_price_updated_and_processed()
        ):
            return False, ""
        
        buy_price = float(self.exchange_adapter.get_buy_orders()[0]["price"])
        buy_bps = abs(
            (self.exchange_adapter.get_depth_mid_price() - buy_price)
            / self.exchange_adapter.get_depth_mid_price()
            * 10000
        )
        sell_price = float(self.exchange_adapter.get_sell_orders()[0]["price"])
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
            reason = f"订单偏离范围异常（买单: {buy_bps:.1f} bps, 卖单: {sell_bps:.1f} bps）"
            return True, reason
        
        self.exchange_adapter.mark_price_processed()
        return False, ""

    async def place_orders(self, market_price: float):
        """下双向限价单"""
        buy_price, sell_price = self.calculate_order_prices(market_price)

        logger.info("下双向限价单 (市价: %.2f)", market_price)

        # 下买单
        try:
            await self.exchange_adapter.new_order(
                symbol=self.symbol,
                side="buy",
                order_type="limit",
                qty=self.qty,
                price=f"{buy_price:.2f}",
                time_in_force="alo",
                reduce_only=False,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )
            logger.info(
                "买单: %s @ %.2f",
                self.qty,
                buy_price,
            )
        except Exception as e:
            logger.exception("买单失败: %s", e)

        # 下卖单
        try:
            await self.exchange_adapter.new_order(
                symbol=self.symbol,
                side="sell",
                order_type="limit",
                qty=self.qty,
                price=f"{sell_price:.2f}",
                time_in_force="alo",
                reduce_only=False,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )
            logger.info(
                "卖单: %s @ %.2f",
                self.qty,
                sell_price,
            )
        except Exception as e:
            logger.exception("卖单失败: %s", e)

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
                await api.cancel_order(self.auth, order_id=order["id"])
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
        logger.info("检查间隔: %s 秒", check_interval)

        # 启动通知
        await self.notifier.send(
            f"*做市策略启动*\n"
            f"时间: {beijing_time}\n"
            f"交易对: `{self.symbol}`\n"
            f"数量: {self.qty}\n"
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

                await self.exchange_adapter.close_position(symbol=self.symbol)

                # 检查订单状态和偏离度
                need_replace, reason = self.check_order_count()
                if not need_replace:
                    need_replace, reason = self.check_price_deviation()

                if need_replace:
                    logger.info("订单需重挂，原因: %s", reason)

                    # 取消所有订单并等待确认
                    await self.exchange_adapter.cancel_all_orders(symbol=self.symbol)
                    cancel_success = await self.exchange_adapter.wait_for_order_count(
                        0, 0, timeout=3.0
                    )
                    if not cancel_success:
                        logger.warning("订单取消确认超时，继续下单")

                    # 下单并等待确认
                    await self.place_orders(self.exchange_adapter.get_depth_mid_price())
                    order_success = await self.exchange_adapter.wait_for_orders(
                        count=2, timeout=5.0
                    )
                    if not order_success:
                        logger.warning("订单下单确认超时，将在下次循环检查")

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
        await self.exchange_adapter.cancel_all_orders(symbol=self.symbol)
        await self.exchange_adapter.cleanup()


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

    # 监控间隔
    check_interval = float(os.getenv("MARKET_MAKER_CHECK_INTERVAL", "0.0"))

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
    await standx_adapter.connect_order_stream(auth)

    # 创建做市器
    market_maker = MarketMaker(
        auth=auth,
        symbol=symbol,
        qty=qty,
        target_bps=target_bps,
        min_bps=min_bps,
        max_bps=max_bps,
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

    await standx_adapter._market_stream.close()


if __name__ == "__main__":
    asyncio.run(main())
