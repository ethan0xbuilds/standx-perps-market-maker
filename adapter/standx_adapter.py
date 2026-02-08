# 标准库导入
import asyncio
import json
import os
import time
from typing import Optional

# 本地模块导入
from api.ws_client import StandXMarketStream, StandXOrderStream
from logger import get_logger
from standx_api import query_positions
from standx_auth import StandXAuth


class StandXAdapter:
    """
    StandXAdapter 用于对接 StandX 市场 WebSocket，处理市场深度、订单、持仓等推送数据，
    并提供本地缓存和查询接口。支持异步订阅和事件处理。
    """

    def __init__(self, symbol: str = "BTC-USD"):
        self._market_stream: Optional[StandXMarketStream] = None
        self._order_stream: Optional[StandXOrderStream] = None
        self._depth_mid_price: Optional[float] = None
        self._depth_book_data: Optional[dict] = None  # 保存完整的盘口数据
        self._last_price_update_time: Optional[float] = None
        self._price_updated_and_processed: bool = True
        self._orders_dict: dict = {}  # 改用字典存储，key为order_id
        self._position: Optional[dict] = {}
        self._last_position_qty: float = 0  # 追踪上一次的持仓数量
        self._last_order_count: int = 0  # 追踪上一次的订单总数，用于检测超量通知
        self._order_confirmed_count: int = 0  # 追踪订单确认次数，用于等待机制
        self._price_event: asyncio.Event = asyncio.Event()  # 用于等待新价格更新
        self._last_full_sync_time: float = 0  # 上次全量同步时间
        self._sync_interval: float = 30.0  # 订单同步间隔，默认30秒
        self._sync_task: Optional[asyncio.Task] = None  # 同步任务
        self._auth: Optional[StandXAuth] = None  # 保存auth实例用于查询
        self._last_message_time: float = 0  # 最后收到消息的时间
        self._health_check_task: Optional[asyncio.Task] = None  # 健康检查任务
        self._reconnecting: bool = False  # 重连标志
        self._symbol = symbol
        self.logger = get_logger(__name__)
        self.notifier = None
        self.account_name = None

    async def connect_market_stream(self) -> StandXMarketStream:
        """
        连接市场 WebSocket（公共频道无需认证）
        Returns:
            StandXMarketStream: 已连接的市场数据流对象
        """
        if not self._market_stream:
            self._market_stream = StandXMarketStream()
        if not self._market_stream.connected:
            await self._market_stream.connect()

    async def subscribe_market(self, channel: str, symbol: str, callback=None):
        """
        订阅市场 WebSocket 频道
        Args:
            channel (str): 频道名
            symbol (str): 交易对
            callback (callable, optional): 回调函数
        """
        await self.connect_market_stream()
        await self._market_stream.subscribe(channel, symbol, callback=callback)

    async def on_depth_book(self, data):
        try:
            self._last_message_time = time.time()  # 更新心跳时间
            self.logger.debug("收到 depth_book 数据: %s", data)
            if data.get("channel") == "depth_book" and data.get("symbol") == self._symbol:
                depth_book_data = data.get("data", {})
                bids = depth_book_data.get("bids") or []
                asks = depth_book_data.get("asks") or []

                # 本地排序，bids从高到低，asks从低到高
                bids = sorted(bids, key=lambda x: float(x[0]), reverse=True)
                asks = sorted(asks, key=lambda x: float(x[0]))
                
                # 保存完整的盘口数据（用于风险计算）
                self._depth_book_data = {
                    "bids": bids,
                    "asks": asks,
                    "timestamp": time.time()
                }

                best_bid = float(bids[0][0]) if bids else None
                best_ask = float(asks[0][0]) if asks else None

                # 计算中间价
                if best_bid is not None and best_ask is not None:
                    mid_price = (best_bid + best_ask) / 2
                elif best_bid is not None:
                    mid_price = best_bid
                elif best_ask is not None:
                    mid_price = best_ask
                else:
                    mid_price = None

                if mid_price is not None:

                    time_diff = 0.0
                    if self._last_price_update_time is not None:
                        # 计算新价格距离上次价格更新的时间间隔
                        time_diff = time.time() - self._last_price_update_time

                    if mid_price == self._depth_mid_price:
                        self.logger.info(
                            "Depth book 中间价未变: %.4f, 距上次更新 %.2f 秒",
                            mid_price,
                            time_diff,
                        )
                    else:
                        self._depth_mid_price = mid_price
                        self.logger.info(
                            "Depth book 中间价更新: %.4f, 距上次更新 %.2f 秒",
                            mid_price,
                            time_diff,
                        )
                    self._last_price_update_time = time.time()
                    self._price_updated_and_processed = False
                    self._price_event.set()  # 设置事件，通知等待者有新价格
        except Exception as e:
            self.logger.exception("处理 depth_book 数据失败: %s", e)

    async def subscribe_depth_book(self):
        """
        订阅深度数据频道
        Args:
        """
        if not self._market_stream:
            self._market_stream = StandXMarketStream()
        if not self._market_stream.connected:
            await self._market_stream.connect()
        if not self._market_stream.authenticated:
            await self._authenticate_and_subscribe()

        await self.subscribe_market(
            channel="depth_book", symbol=self._symbol, callback=self.on_depth_book
        )
        
        # 启动健康检查任务
        if not self._health_check_task or self._health_check_task.done():
            self._health_check_task = asyncio.create_task(self._health_check_loop())
            self.logger.info("健康检查任务已启动")

    def get_depth_mid_price(self) -> Optional[float]:
        """
        获取当前中间价
        Returns:
            Optional[float]: 当前中间价
        """
        return self._depth_mid_price
    
    def get_depth_book_data(self) -> Optional[dict]:
        """
        获取完整的盘口数据（用于风险分析）
        Returns:
            Optional[dict]: 包含 bids, asks, timestamp 的字典
        """
        return self._depth_book_data

    async def on_order(self, data):
        """
        处理 order 频道推送（增量更新）
        Args:
            data (dict): 订单推送数据
        """
        try:
            if data.get("channel") == "order":
                order_data = data.get("data", {})
                order_id = order_data.get("id")
                order_status = order_data.get("status")
                
                # 详细日志
                self.logger.info(
                    "订单推送: id=%s, symbol=%s, side=%s, status=%s, qty=%s, price=%s, fill_qty=%s, fill_avg_price=%s",
                    order_id,
                    order_data.get("symbol"),
                    order_data.get("side"),
                    order_status,
                    order_data.get("qty"),
                    order_data.get("price"),
                    order_data.get("fill_qty"),
                    order_data.get("fill_avg_price"),
                )
                
                # 增量更新逻辑
                if order_status in ["canceled", "filled"]:
                    # 已完成的订单，从缓存中移除
                    if order_id in self._orders_dict:
                        del self._orders_dict[order_id]
                        self.logger.info("订单已完成，移除 id=%s", order_id)
                    else:
                        self.logger.debug("收到已完成订单但本地不存在 id=%s", order_id)
                else:
                    # 活跃订单，更新或添加到缓存
                    if order_id in self._orders_dict:
                        self.logger.info("订单已更新 id=%s", order_id)
                    else:
                        self.logger.info("新增订单 id=%s", order_id)
                        self._order_confirmed_count += 1
                    
                    self._orders_dict[order_id] = order_data
                
                # 检测订单总数是否超过2
                self.logger.info("当前订单总数: %d", len(self._orders_dict))
                await self._check_order_count_exceeded()
                
        except Exception as e:
            self.logger.exception("处理 order 数据失败: %s", e)

    async def _check_order_count_exceeded(self):
        """
        检测订单总数是否超过2，如果超过则发送通知
        """
        current_count = len(self._orders_dict)
        if self._last_order_count <= 2 and current_count > 2:
            # 从 <= 2 变到 > 2，发送通知
            if self.notifier:
                await self.notifier.send(
                    f"⚠️ *订单总数超过2*\n"
                    f"账户: `{self.account_name}`\n"
                    f"订单总数: {current_count}\n"
                    f"买单: {self.get_buy_order_count()}, 卖单: {self.get_sell_order_count()}"
                )
            self.logger.warning("订单总数超过2: %d", current_count)
        self._last_order_count = current_count

    async def on_position(self, data):
        """
        处理 position 频道推送
        Args:
            data (dict): 持仓推送数据
        """
        try:
            if data.get("channel") == "position":
                pos_data = data.get("data", {})
                self.logger.info(
                    "持仓推送: id=%s, symbol=%s, qty=%s, entry_price=%s, leverage=%s, margin_mode=%s, status=%s, realized_pnl=%s",
                    pos_data.get("id"),
                    pos_data.get("symbol"),
                    pos_data.get("qty"),
                    pos_data.get("entry_price"),
                    pos_data.get("leverage"),
                    pos_data.get("margin_mode"),
                    pos_data.get("status"),
                    pos_data.get("realized_pnl"),
                )
                
                # 检测持仓变化并发送通知
                current_qty = float(pos_data.get("qty", 0))
                symbol = pos_data.get("symbol", "")
                
                # 从无持仓变为有持仓
                if self._last_position_qty == 0 and current_qty != 0:
                    direction = "多头" if current_qty > 0 else "空头"
                    if self.notifier:
                        await self.notifier.send(
                            f"*新增持仓*\n"
                            f"账户: `{self.account_name}`\n"
                            f"交易对: `{symbol}`\n"
                            f"方向: {direction}\n"
                            f"数量: {abs(current_qty)}\n"
                            f"入场价: {pos_data.get('entry_price', 'N/A')}"
                        )
                # 从有持仓变为无持仓
                elif self._last_position_qty != 0 and current_qty == 0:
                    if self.notifier:
                        await self.notifier.send(
                            f"*持仓已清*\n"
                            f"账户: `{self.account_name}`\n"
                            f"交易对: `{symbol}`\n"
                            f"已实现盈亏: {pos_data.get('realized_pnl', 'N/A')}"
                        )
                
                self._last_position_qty = current_qty
                self._position = pos_data
        except Exception as e:
            self.logger.exception("处理 position 数据失败: %s", e)

    async def _authenticate_and_subscribe(self):
        """
        认证并订阅订单和持仓频道
        Raises:
            ValueError: ACCESS_TOKEN 未设置
        """
        if not os.getenv("ACCESS_TOKEN"):
            raise ValueError("环境变量 ACCESS_TOKEN 未设置")

        await self.connect_market_stream()
        await self._market_stream.authenticate(
            os.getenv("ACCESS_TOKEN"), [{"channel": "order"}, {"channel": "position"}]
        )
        await self._market_stream.subscribe("order", callback=self.on_order)
        await self._market_stream.subscribe("position", callback=self.on_position)

    async def _initial_sync_with_timeout(self):
        """初始同步订单（带超时保护，防止阻塞价格获取）"""
        try:
            # 最多等待3秒完成初始同步，超时继续运行，由定期同步补偿
            await asyncio.wait_for(
                self._sync_orders_from_server(),
                timeout=3.0
            )
        except asyncio.TimeoutError:
            self.logger.warning("初始订单同步超时（3秒），继续运行，将由定期同步补偿")
        except Exception as e:
            self.logger.exception("初始订单同步失败: %s", e)

    async def _sync_orders_from_server(self):
        """从服务器全量同步订单状态（使用HTTP API，带超时）"""
        try:
            from standx_api import query_open_orders
            
            self.logger.info("开始全量同步订单状态...")
            # 查询API最多3秒超时，防止阻塞
            try:
                result = await asyncio.wait_for(
                    query_open_orders(self._auth, symbol=None, limit=100),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                self.logger.warning("查询开仓订单API超时（3秒），本次同步跳过")
                return
            
            server_orders = result.get("result", [])
            server_order_ids = {order["id"] for order in server_orders}
            
            # 更新本地缓存为字典格式
            new_orders_dict = {order["id"]: order for order in server_orders}
            
            # 检测本地多余的订单（孤儿订单）
            local_order_ids = set(self._orders_dict.keys())
            orphaned_ids = local_order_ids - server_order_ids
            
            if orphaned_ids:
                self.logger.warning("检测到孤儿订单（本地有但服务器无）: %s", orphaned_ids)
                if self.notifier:
                    await self.notifier.send(
                        f"⚠️ *检测到孤儿订单*\n"
                        f"账户: `{self.account_name}`\n"
                        f"订单ID: {list(orphaned_ids)}\n"
                        f"已从本地缓存清除"
                    )
            
            # 检测服务器多余的订单（未推送的新订单）
            new_ids = server_order_ids - local_order_ids
            if new_ids:
                self.logger.warning("检测到未推送的订单（服务器有但本地无）: %s", new_ids)
            
            # 替换为最新数据
            self._orders_dict = new_orders_dict
            self._last_full_sync_time = time.time()
            
            self.logger.info(
                "订单同步完成: 服务器 %d 个, 本地 %d 个, 孤儿 %d 个, 新增 %d 个",
                len(server_order_ids),
                len(local_order_ids),
                len(orphaned_ids),
                len(new_ids)
            )
            
        except Exception as e:
            self.logger.exception("订单同步失败: %s", e)

    async def _periodic_sync_loop(self):
        """定期全量同步循环（带超时控制）"""
        while True:
            try:
                await asyncio.sleep(self._sync_interval)
                # 同步操作最多5秒，超时则跳过此次同步，下次继续尝试
                try:
                    await asyncio.wait_for(
                        self._sync_orders_from_server(),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("定期订单同步超时（5秒），下次继续尝试")
            except asyncio.CancelledError:
                self.logger.info("订单同步任务已取消")
                break
            except Exception as e:
                self.logger.exception("订单同步循环异常: %s", e)

    async def _health_check_loop(self):
        """健康检查循环 - 监控WebSocket连接"""
        check_interval = 30.0  # 每30秒检查一次
        timeout_threshold = 120.0  # 2分钟无消息视为超时
        
        self.logger.info("健康检查循环已启动，检查间隔: %.0f秒，超时阈值: %.0f秒", check_interval, timeout_threshold)
        
        while True:
            try:
                await asyncio.sleep(check_interval)
                
                # 检查market stream连接状态
                if self._market_stream:
                    if not self._market_stream.connected:
                        self.logger.error("检测到market stream断开，准备重连...")
                        await self._reconnect_market_stream()
                    elif self._last_message_time > 0:
                        time_since_last = time.time() - self._last_message_time
                        if time_since_last > timeout_threshold:
                            self.logger.error(
                                f"超过{timeout_threshold}秒未收到消息（上次: {time_since_last:.1f}秒前），准备重连..."
                            )
                            await self._reconnect_market_stream()
                        else:
                            self.logger.debug("健康检查通过，距上次消息: %.1f秒", time_since_last)
                
            except asyncio.CancelledError:
                self.logger.info("健康检查任务已取消")
                break
            except Exception as e:
                self.logger.exception("健康检查循环异常: %s", e)

    async def _reconnect_market_stream(self):
        """重连market stream"""
        if self._reconnecting:
            self.logger.info("重连已在进行中，跳过")
            return
        
        self._reconnecting = True
        try:
            self.logger.info("开始重连market stream...")
            
            # 关闭旧连接
            if self._market_stream:
                try:
                    await self._market_stream.disconnect()
                except Exception as e:
                    self.logger.warning(f"关闭旧连接失败: {e}")
            
            # 重新创建并连接
            self._market_stream = StandXMarketStream()
            await self._market_stream.connect()
            
            # 重新订阅depth_book
            await self._market_stream.subscribe(
                channel="depth_book", 
                symbol=self._symbol,
                callback=self.on_depth_book
            )
            self.logger.info("已重新订阅depth_book")
            
            # 如果需要认证，重新认证（订单和持仓频道）
            if os.getenv("ACCESS_TOKEN"):
                await self._market_stream.authenticate(
                    os.getenv("ACCESS_TOKEN"),
                    [{"channel": "order"}, {"channel": "position"}]
                )
                await self._market_stream.subscribe("order", callback=self.on_order)
                await self._market_stream.subscribe("position", callback=self.on_position)
                self.logger.info("已重新认证并订阅order/position")
            
            self._last_message_time = time.time()
            self.logger.info("Market stream重连成功")
            
            # 发送通知
            if self.notifier:
                await self.notifier.send(
                    f"✅ *WebSocket重连成功*\n"
                    f"账户: `{self.account_name}`\n"
                    f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            
        except Exception as e:
            self.logger.exception(f"重连失败: {e}")
            if self.notifier:
                await self.notifier.send(
                    f"⚠️ *WebSocket重连失败*\n"
                    f"账户: `{self.account_name}`\n"
                    f"错误: {e}"
                )
        finally:
            self._reconnecting = False

    def get_buy_order_count(self) -> int:
        """
        获取买单数量
        Returns:
            int: 买单数量
        """
        return sum(1 for order in self._orders_dict.values() if order["side"] == "buy")

    def get_sell_order_count(self) -> int:
        """
        获取卖单数量
        Returns:
            int: 卖单数量
        """
        return sum(1 for order in self._orders_dict.values() if order["side"] == "sell")

    def get_buy_orders(self) -> list:
        """
        获取所有买单列表
        Returns:
            list: 买单列表
        """
        return [order for order in self._orders_dict.values() if order["side"] == "buy"]

    def get_sell_orders(self) -> list:
        """
        获取所有卖单列表
        Returns:
            list: 卖单列表
        """
        return [order for order in self._orders_dict.values() if order["side"] == "sell"]

    async def get_position(self, symbol: Optional[str] = None) -> list:
        """
        获取当前持仓信息（来自 WebSocket 最新推送）
        Args:
            symbol (Optional[str]): 交易对占位参数（当前实现未使用）
        Returns:
            dict: 最新持仓信息，未收到推送时为 {}
        """
        return self._position

    def is_price_updated_and_processed(self) -> bool:
        """
        判断中间价是否已处理
        Returns:
            bool: 是否已处理
        """
        return self._price_updated_and_processed

    def mark_price_processed(self):
        """
        标记中间价已处理
        """
        self._price_updated_and_processed = True

    async def wait_for_orders(self, count: int = 2, timeout: float = 5.0) -> bool:
        """
        等待指定数量的新订单确认（通过WebSocket回调）
        Args:
            count: 等待的订单数量
            timeout: 超时时间（秒）
        Returns:
            bool: 是否在超时前收到所有订单确认
        """
        initial_count = self._order_confirmed_count
        target_count = initial_count + count

        start_time = time.time()
        while time.time() - start_time < timeout:
            if self._order_confirmed_count >= target_count:
                self.logger.info(
                    "订单确认完成: 已确认 %d 个订单，耗时 %.2f 秒",
                    count,
                    time.time() - start_time,
                )
                return True
            await asyncio.sleep(0.05)  # 50ms检查一次

        self.logger.warning(
            "订单确认超时: 期望 %d 个，实际收到 %d 个，耗时 %.2f 秒",
            count,
            self._order_confirmed_count - initial_count,
            timeout,
        )
        return False

    async def wait_for_order_count(
        self, target_buy: int, target_sell: int, timeout: float = 5.0
    ) -> bool:
        """
        等待订单数量达到目标值（用于等待订单取消）
        Args:
            target_buy: 目标买单数量
            target_sell: 目标卖单数量
            timeout: 超时时间（秒）
        Returns:
            bool: 是否在超时前达到目标
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            if (
                self.get_buy_order_count() == target_buy
                and self.get_sell_order_count() == target_sell
            ):
                self.logger.info(
                    "订单数量达到目标: 买单 %d, 卖单 %d，耗时 %.2f 秒",
                    target_buy,
                    target_sell,
                    time.time() - start_time,
                )
                return True
            await asyncio.sleep(0.05)  # 50ms检查一次

        self.logger.warning(
            "等待订单数量超时: 目标(买%d/卖%d), 实际(买%d/卖%d), 耗时 %.2f 秒",
            target_buy,
            target_sell,
            self.get_buy_order_count(),
            self.get_sell_order_count(),
            timeout,
        )
        return False

    async def wait_for_new_price(self, timeout: float = 2.0) -> bool:
        """
        等待获取新的价格更新
        Args:
            timeout: 超时时间（秒），默认2.0秒
        Returns:
            bool: 是否在超时前收到新价格，True表示成功，False表示超时
        """
        self._price_event.clear()  # 清空事件，准备等待新价格
        try:
            await asyncio.wait_for(self._price_event.wait(), timeout=timeout)
            self.logger.debug("已获取新价格，无需等待")
            return True
        except asyncio.TimeoutError:
            self.logger.warning("等待新价格超时 (%.1f秒)，取消下单", timeout)
            return False

    def on_login(self, data):
        """
        处理登录成功回调
        Args:
            data (dict): 登录成功数据
        """
        self.logger.info("WebSocket 登录成功: %s", data)

    def on_new_order(self, data):
        """
        处理新订单回调
        Args:
            data (dict): 新订单数据
        """
        self.logger.info("通过订单流下单成功: %s", data)

    def on_cancel_order(self, data):
        """
        处理取消订单回调
        Args:
            data (dict): 取消订单数据
        """
        self.logger.info("通过订单流取消订单成功: %s", data)

    async def connect_order_stream(self, auth):
        """
        连接订单和持仓 WebSocket（需要认证）
        Returns:
            StandXOrderStream: 已连接的订单数据流对象
        """
        # 保存auth实例用于后续查询
        self._auth = auth
        
        if not self._order_stream:
            self._order_stream = StandXOrderStream()
        if not self._order_stream.connected:
            await self._order_stream.connect()
        if not self._order_stream.auth:
            self._order_stream.auth = auth
        if not os.getenv("ACCESS_TOKEN"):
            raise ValueError("环境变量 ACCESS_TOKEN 未设置")

        await self._order_stream.login(
            token=os.getenv("ACCESS_TOKEN"), callback=self.on_login
        )
        
        # ✨ 改进：初始同步改为后台任务（带超时保护），不阻塞主流程，避免延迟价格获取
        asyncio.create_task(self._initial_sync_with_timeout())
        
        # 启动定期同步任务
        if not self._sync_task or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._periodic_sync_loop())

    async def new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        qty: str,
        price: Optional[str] = None,
        time_in_force: str = "gtc",
        reduce_only: bool = False,
        margin_mode: Optional[str] = None,
        leverage: Optional[int] = None,
    ) -> dict:
        """
        通过订单流下单
        Args:
            symbol (str): 交易对
            side (str): 买卖方向 "buy" 或 "sell"
            order_type (str): 订单类型 "limit" 或 "market"
            qty (str): 订单数量
            price (Optional[str]): 订单价格（限价单必填）
            time_in_force (str): 有效方式，默认 "gtc"
            reduce_only (bool): 是否仅减仓，默认 False
            margin_mode (Optional[str]): 保证金模式
            leverage (Optional[int]): 杠杆倍数
        Returns:
            dict: 下单结果
        """
        if not self._order_stream or not self._order_stream.connected:
            raise RuntimeError("订单流未连接，请先调用 connect_order_stream()")

        await self._order_stream.new_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=qty,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
            price=price,
            cl_ord_id=None,
            callback=self.on_new_order,
        )

    async def close_position(self, symbol: str):
        """
        通过市价订单平掉当前持仓
        Args:
            symbol (str): 交易对
        """
        position = await self.get_position(symbol)

        if not position:
            return

        qty_value = float(position.get("qty", 0))
        if qty_value == 0:
            return

        side = "sell" if qty_value > 0 else "buy"
        qty = str(abs(qty_value))

        self.logger.info(
            "准备通过市价单平仓: symbol=%s, side=%s, qty=%s",
            symbol,
            side,
            qty,
        )

        try:
            await self.new_order(
                symbol=symbol,
                side=side,
                order_type="market",
                qty=qty,
                reduce_only=True,
            )
            
            # 平仓成功发送通知
            if self.notifier:
                await self.notifier.send(
                    f"*持仓平仓*\n"
                    f"账户: `{self.account_name}`\n"
                    f"交易对: `{symbol}`\n"
                    f"方向: {side}\n"
                    f"数量: {qty}"
                )
        except Exception as e:
            self.logger.exception("平仓失败: %s", e)
            # 平仓失败发送通知
            if self.notifier:
                await self.notifier.send(
                    f"*平仓失败*\n"
                    f"账户: `{self.account_name}`\n"
                    f"交易对: `{symbol}`\n"
                    f"错误: {e}"
                )

    async def cancel_all_orders(self, symbol: Optional[str] = None):
        """
        取消所有未完成订单
        Args:
            symbol (Optional[str]): 交易对，若提供则只取消该交易对的订单
        """
        if not self._order_stream or not self._order_stream.connected:
            raise RuntimeError("订单流未连接，请先调用 connect_order_stream()")

        orders_to_cancel = (
            [order for order in self._orders_dict.values() if order["symbol"] == symbol]
            if symbol
            else list(self._orders_dict.values())
        )

        for order in orders_to_cancel:
            try:
                await self._order_stream.cancel_order(
                    order_id=order["id"],
                    cl_ord_id=order["cl_ord_id"],
                    callback=self.on_cancel_order,
                )
            except Exception as e:
                self.logger.exception("取消失败: %s", e)

    async def cleanup(self):
        """清理资源，关闭 WebSocket 连接"""
        # 取消健康检查任务
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                self.logger.info("健康检查任务已取消")
        
        # 取消同步任务
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                self.logger.info("订单同步任务已取消")
        
        # 关闭连接
        if self._market_stream and self._market_stream.connected:
            await self._market_stream.disconnect()
        if self._order_stream and self._order_stream.connected:
            await self._order_stream.disconnect()
