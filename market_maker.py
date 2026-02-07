"""
双向限价单做市策略
- 同时挂买单和卖单
- 监控价格变化
- 订单偏离超过阈值时取消并重新挂单
"""

# 标准库导入
import argparse
import asyncio
import os
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

# 首先加载环境变量，必须在其他模块导入之前
from dotenv import load_dotenv

# 本地模块导入
from adapter.standx_adapter import StandXAdapter
from standx_auth import StandXAuth
import standx_api as api
from notifier import Notifier
from logger import get_logger, configure_logging


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
        account_name: str = None,
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
        self.account_name = account_name or "default"

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
        
        # 获取 logger 实例
        self.logger = get_logger(__name__)

    def _setup_signal_handlers(self):
        """设置信号处理器以支持优雅关闭"""

        def handle_signal(signum, frame):
            self.logger.info("收到信号 %s，准备优雅关闭...", signum)
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
    
    def calculate_market_risk(self) -> tuple[float, str]:
        """
        计算市场风险等级（基于盘口压力）
        
        Returns:
            (risk_score, description) 风险分数 0-100 和描述
        """
        depth_data = self.exchange_adapter.get_depth_book_data()
        if not depth_data:
            return 50.0, "数据不足"
        
        bids = depth_data.get("bids", [])
        asks = depth_data.get("asks", [])
        
        if len(bids) < 5 or len(asks) < 5:
            return 50.0, "深度不足"
        
        mid_price = self.exchange_adapter.get_depth_mid_price()
        if not mid_price:
            return 50.0, "价格缺失"
        
        # 1. 计算买卖盘口价差（相对值）
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        spread_bps = (best_ask - best_bid) / mid_price * 10000
        
        # 2. 计算前5档买卖量比
        bid_volume = sum(float(b[1]) for b in bids[:5])
        ask_volume = sum(float(a[1]) for a in asks[:5])
        volume_ratio = min(bid_volume, ask_volume) / max(bid_volume, ask_volume) if max(bid_volume, ask_volume) > 0 else 0.5
        
        # 3. 计算价格密集度（前10档价格跨度）
        if len(bids) >= 10 and len(asks) >= 10:
            bid_depth = (float(bids[0][0]) - float(bids[9][0])) / mid_price * 10000
            ask_depth = (float(asks[9][0]) - float(asks[0][0])) / mid_price * 10000
            depth_avg = (bid_depth + ask_depth) / 2
        else:
            depth_avg = 50  # 默认中等
        
        # 综合评分（0-100，越高越危险）
        # 价差大 -> 风险高；买卖不平衡 -> 风险高；深度越大（盘口越稀） -> 风险高
        risk_score = (
            spread_bps * 2 +  # 价差权重
            (1 - volume_ratio) * 50 +  # 不平衡度权重
            min(depth_avg, 50) * 0.5  # 深度权重
        )
        
        risk_score = max(0, min(100, risk_score))
        
        desc = f"价差:{spread_bps:.1f}bps 量比:{volume_ratio:.2f} 深度:{depth_avg:.1f}bps"
        return risk_score, desc
    
    def get_adaptive_bps(self) -> tuple[float, float, str]:
        """
        根据市场风险动态调整挂单偏离
        
        Returns:
            (target_bps, min_bps, reason) 目标偏离、最小偏离、决策原因
        """
        # 计算市场风险
        risk_score, risk_desc = self.calculate_market_risk()
        
        # 根据风险分数决定挂单策略
        if risk_score < 20:
            target_bps = 8.0
            min_bps = 6.0
            max_bps = 10.0
            reason = f"低风险({risk_score:.0f})"
        elif risk_score < 50:
            target_bps = 25.0
            min_bps = 20.0
            max_bps = 30.0
            reason = f"中风险({risk_score:.0f})"
        else:
            target_bps = 80.0
            min_bps = 60.0
            max_bps = 100.0
            reason = f"高风险({risk_score:.0f})"
        
        return target_bps, min_bps, max_bps, f"{reason} - {risk_desc}"

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
            self.logger.info(
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
        self.logger.info(
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

    async def place_orders(self, market_price: float = None):
        """下双向限价单
        
        Args:
            market_price: 市场价格，如果为None则等待最新价格更新
        """
        # 如果未提供价格，则等待最新价格更新
        if market_price is None:
            if await self.exchange_adapter.wait_for_new_price(timeout=2.0):
                # 成功等待到新价格
                market_price = self.exchange_adapter.get_depth_mid_price()
            else:
                # 超时则取消下单
                self.logger.warning("获取市场价格超时，取消下单")
                return
        
        buy_price, sell_price = self.calculate_order_prices(market_price)

        self.logger.info("下双向限价单 (市价: %.2f)", market_price)

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
            self.logger.info(
                "买单: %s @ %.2f",
                self.qty,
                buy_price,
            )
        except Exception as e:
            self.logger.exception("买单失败: %s", e)

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
            self.logger.info(
                "卖单: %s @ %.2f",
                self.qty,
                sell_price,
            )
        except Exception as e:
            self.logger.exception("卖单失败: %s", e)

    async def run(self, check_interval: float = 0.025):
        """
        运行做市策略（事件驱动架构）

        Args:
            check_interval: 保留参数以兼容旧配置，实际使用事件驱动机制
        """
        
        # 设置信号处理器
        self._setup_signal_handlers()

        beijing_tz = ZoneInfo("Asia/Shanghai")
        beijing_time = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        self.logger.info("双向限价单做市策略启动（事件驱动模式） - %s", beijing_time)
        self.logger.info("交易对: %s", self.symbol)
        self.logger.info("订单数量: %s", self.qty)

        # 启动通知
        await self.notifier.send(
            f"*做市策略启动*\n"
            f"账户: `{self.account_name}`\n"
            f"时间: {beijing_time}\n"
            f"交易对: `{self.symbol}`\n"
            f"数量: {self.qty}\n"
            f"模式: 事件驱动\n"
        )

        # 等待 mid_price 数据就绪（只执行一次）
        while self.exchange_adapter.get_depth_mid_price() is None:
            self.logger.info("等待行情数据（mid_price）...")
            await asyncio.sleep(0.2)

        # 创建独立的监控任务
        try:
            price_check_task = asyncio.create_task(self._price_monitor_loop())
            position_check_task = asyncio.create_task(self._position_monitor_loop())
            
            # 等待任务完成（通常是收到关闭信号）
            await asyncio.gather(price_check_task, position_check_task)

        except KeyboardInterrupt:
            self.logger.info("收到中断信号，停止策略...")
            await self.notifier.send(
                f"*策略停止*\n" f"账户: `{self.account_name}`\n" f"交易对: `{self.symbol}`\n" f"原因: 收到中断信号"
            )
        except Exception as e:
            self.logger.exception("策略运行出现严重错误: %s", e)
            self.logger.info("正在清理订单并退出...")
            await self.notifier.send(
                f"*致命异常*\n" f"账户: `{self.account_name}`\n" f"交易对: `{self.symbol}`\n" f"错误: {e}"
            )

    async def _price_monitor_loop(self):
        """
        价格监控循环 - 仅在价格变化时触发检查
        使用事件驱动机制 + 自适应挂单策略
        """
        self.logger.info("价格监控任务启动（自适应挂单模式）")
        
        while not self._shutdown_requested:
            try:
                # 等待新价格更新（阻塞直到有新价格或超时）
                price_updated = await self.exchange_adapter.wait_for_new_price(timeout=30.0)
                
                if not price_updated:
                    # 30秒无新价格更新，继续等待
                    self.logger.debug("30秒内无价格更新，继续等待...")
                    continue
                
                # 动态调整挂单参数（基于市场风险）
                new_target_bps, new_min_bps, new_max_bps, reason = self.get_adaptive_bps()
                
                # 检测参数是否发生显著变化（超过20%）
                params_changed = (
                    abs(new_target_bps - self.target_bps) / self.target_bps > 0.2 if self.target_bps > 0 else False
                )
                
                if params_changed:
                    self.logger.info(
                        "📊 挂单参数调整: %.1f→%.1f bps (范围: %.1f-%.1f), 原因: %s",
                        self.target_bps, new_target_bps, new_min_bps, new_max_bps, reason
                    )
                    self.target_bps = new_target_bps
                    self.min_bps = new_min_bps
                    self.max_bps = new_max_bps
                    # 参数变化时强制重挂单
                    await self._replace_orders(f"策略调整: {reason}")
                    continue
                else:
                    # 参数未变化，更新内部值（用于下次比较）
                    self.target_bps = new_target_bps
                    self.min_bps = new_min_bps
                    self.max_bps = new_max_bps
                
                # 正常偏离检查
                need_replace, check_reason = self.check_order_count()
                if not need_replace:
                    need_replace, check_reason = self.check_price_deviation()
                
                if need_replace:
                    await self._replace_orders(check_reason)
                    
            except asyncio.TimeoutError:
                # wait_for_new_price 超时，继续循环
                continue
            except Exception as e:
                self.logger.exception("价格监控循环异常: %s", e)
                await asyncio.sleep(1.0)  # 出错后等待1秒再继续
        
        self.logger.info("价格监控任务结束")

    async def _position_monitor_loop(self):
        """
        持仓监控循环 - 定期检查并平仓（低频）
        持仓检查频率较低，1秒一次即可
        """
        self.logger.info("持仓监控任务启动")
        
        while not self._shutdown_requested:
            try:
                await self.exchange_adapter.close_position(symbol=self.symbol)
                await asyncio.sleep(1.0)  # 持仓检查频率：1秒/次
            except Exception as e:
                self.logger.exception("持仓监控循环异常: %s", e)
                await asyncio.sleep(1.0)  # 出错后等待1秒再继续
        
        self.logger.info("持仓监控任务结束")

    async def _replace_orders(self, reason: str):
        """
        订单重挂逻辑（提取为独立方法）
        
        Args:
            reason: 重挂原因
        """
        self.logger.info("订单需重挂，原因: %s", reason)
        
        # 取消所有订单并等待确认
        await self.exchange_adapter.cancel_all_orders(symbol=self.symbol)
        cancel_success = await self.exchange_adapter.wait_for_order_count(
            0, 0, timeout=3.0
        )
        if not cancel_success:
            self.logger.warning("订单取消确认超时，跳过下单")
            return
        
        # 下单时等待最新价格，并等待确认
        await self.place_orders()
        order_success = await self.exchange_adapter.wait_for_orders(
            count=2, timeout=5.0
        )
        if not order_success:
            self.logger.warning("订单下单确认超时，将在下次循环检查")
            return

    async def cleanup(self):
        """清理所有订单和资源"""
        await self.exchange_adapter.cancel_all_orders(symbol=self.symbol)
        await self.exchange_adapter.cleanup()


async def main():
    """主函数"""
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='StandX 做市机器人')
    parser.add_argument('--config', type=str, default='.env',
                        help='配置文件路径 (默认: .env)')
    parser.add_argument('--log-prefix', type=str, default='',
                        help='日志文件前缀 (默认: 空)')
    args = parser.parse_args()
    
    # 配置日志（如果指定了前缀则使用前缀）
    if args.log_prefix:
        configure_logging(log_prefix=args.log_prefix)
    else:
        configure_logging()  # 使用默认配置
    
    # 获取 logger 实例
    logger = get_logger(__name__)
    
    # 加载指定的配置文件
    load_dotenv(args.config)
    logger.info("使用配置文件: %s", args.config)

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
        logger.exception("认证失败: %s", e)
        account_name = args.log_prefix or symbol
        await notifier.send(f"❌ *认证失败*\n" f"账户: `{account_name}`\n" f"交易对: `{symbol}`\n" f"错误: {e}")
        raise

    # 创建 StandX 适配器
    standx_adapter = StandXAdapter()
    # 订阅depth_book频道
    await standx_adapter.subscribe_depth_book(symbol="BTC-USD")
    await standx_adapter.connect_order_stream(auth)

    # 创建做市器
    # 从log_prefix获取账户名
    account_name = args.log_prefix or symbol
    
    # 设置adapter的通知信息
    standx_adapter.notifier = notifier
    standx_adapter.account_name = account_name
    
    market_maker = MarketMaker(
        auth=auth,
        symbol=symbol,
        qty=qty,
        target_bps=target_bps,
        min_bps=min_bps,
        max_bps=max_bps,
        notifier=notifier,
        exchange_adapter=standx_adapter,
        account_name=account_name,
    )
    
    try:
        # 启动做市和 WebSocket 监听为并发任务
        maker_task = asyncio.create_task(market_maker.run(check_interval=check_interval))
        # 其他需要常驻的异步任务也用 create_task
        await maker_task
    finally:
        # 确保清理资源，即使被Ctrl+C中断也会执行
        logger.info("执行清理操作...")
        await market_maker.cleanup()
        
        # 停止通知
        await notifier.send(
            f"*做市策略已停止*\n" f"账户: `{account_name}`\n" f"交易对: `{symbol}`\n" f"订单已清理完成"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # 优雅退出，不显示traceback
