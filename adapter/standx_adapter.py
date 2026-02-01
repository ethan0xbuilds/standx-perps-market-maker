import asyncio
import json
import os
import time
from typing import Optional
from api.ws_client import StandXMarketStream, StandXOrderStream
from logger import get_logger
from standx_api import query_positions
from standx_auth import StandXAuth

logger = get_logger(__name__)


class StandXAdapter:
    """
    StandXAdapter 用于对接 StandX 市场 WebSocket，处理市场深度、订单、持仓等推送数据，
    并提供本地缓存和查询接口。支持异步订阅和事件处理。
    """

    def __init__(self):
        self._market_stream: Optional[StandXMarketStream] = None
        self._order_stream: Optional[StandXOrderStream] = None
        self._depth_mid_price: Optional[float] = None
        self._last_price_update_time: Optional[float] = None
        self._price_updated_and_processed: bool = True
        self._orders: list = []
        self._position: Optional[dict] = None
        self._positions: Optional[list] = None

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
            if data.get("channel") == "depth_book" and data.get("symbol") == "BTC-USD":
                depth_book_data = data.get("data", {})
                bids = depth_book_data.get("bids") or []
                asks = depth_book_data.get("asks") or []

                # 本地排序，bids从高到低，asks从低到高
                bids = sorted(bids, key=lambda x: float(x[0]), reverse=True)
                asks = sorted(asks, key=lambda x: float(x[0]))

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
                        # 中间价未变，打印DEBUG日志表示接收到数据但价格未变
                        logger.debug(
                            "Depth book 中间价未变: %.4f, 距上次更新 %.2f 秒",
                            mid_price,
                            time_diff,
                        )
                        return
                    else:
                        self._depth_mid_price = mid_price
                        logger.info(
                            "Depth book 中间价更新: %.4f, 距上次更新 %.2f 秒",
                            mid_price,
                            time_diff,
                        )
                        self._last_price_update_time = time.time()
                        self._price_updated_and_processed = False
        except Exception as e:
            logger.exception("处理 depth_book 数据失败: %s", e)

    async def subscribe_depth_book(self, symbol: str = "BTC-USD"):
        """
        订阅深度数据频道
        Args:
            symbol (str): 交易对，默认 BTC-USD
        """
        if not self._market_stream:
            self._market_stream = StandXMarketStream()
        if not self._market_stream.connected:
            await self._market_stream.connect()
        if not self._market_stream.authenticated:
            await self._authenticate()

        await self.subscribe_market(
            channel="depth_book", symbol=symbol, callback=self.on_depth_book
        )

    def get_depth_mid_price(self) -> Optional[float]:
        """
        获取当前中间价
        Returns:
            Optional[float]: 当前中间价
        """
        return self._depth_mid_price

    async def on_order(self, data):
        """
        处理 order 频道推送
        Args:
            data (dict): 订单推送数据
        """
        try:
            if data.get("channel") == "order":
                order_data = data.get("data", {})
                # 这里只做简单日志，后续可扩展为事件分发、状态同步等
                logger.info(
                    "订单推送: id=%s, symbol=%s, side=%s, status=%s, qty=%s, price=%s, fill_qty=%s, fill_avg_price=%s",
                    order_data.get("id"),
                    order_data.get("symbol"),
                    order_data.get("side"),
                    order_data.get("status"),
                    order_data.get("qty"),
                    order_data.get("price"),
                    order_data.get("fill_qty"),
                    order_data.get("fill_avg_price"),
                )
                # 订单取消时移除订单，否则新增订单
                for idx, order in enumerate(self._orders):
                    if (
                        order["id"] == order_data.get("id")
                        and order_data.get("status") == "canceled"
                    ):
                        self._orders.pop(idx)
                        logger.info("订单已取消，移除订单 id=%s", order_data.get("id"))
                        logger.info("当前订单总数: %d", len(self._orders))
                        break
                else:
                    # 新增订单（如未取消且 id 不在当前订单列表中）
                    logger.info("新增订单 id=%s", order_data.get("id"))
                    self._orders.append(order_data)
                    logger.info("当前订单总数: %d", len(self._orders))
        except Exception as e:
            logger.exception("处理 order 数据失败: %s", e)

    async def on_position(self, data):
        """
        处理 position 频道推送
        Args:
            data (dict): 持仓推送数据
        """
        try:
            if data.get("channel") == "position":
                pos_data = data.get("data", {})
                logger.info(
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
                self._position = pos_data
                logger.info("当前持仓数据已更新")
                logger.debug("当前持仓详情: %s", self._position)
        except Exception as e:
            logger.exception("处理 position 数据失败: %s", e)

    async def _authenticate(self):
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

    def get_buy_order_count(self) -> int:
        """
        获取买单数量
        Returns:
            int: 买单数量
        """
        if self._orders:
            return sum(1 for order in self._orders if order["side"] == "buy")
        return 0

    def get_sell_order_count(self) -> int:
        """
        获取卖单数量
        Returns:
            int: 卖单数量
        """
        if self._orders:
            return sum(1 for order in self._orders if order["side"] == "sell")
        return 0

    def get_buy_orders(self) -> list:
        """
        获取所有买单列表
        Returns:
            list: 买单列表
        """
        if self._orders:
            return [order for order in self._orders if order["side"] == "buy"]
        return []

    def get_sell_orders(self) -> list:
        """
        获取所有卖单列表
        Returns:
            list: 卖单列表
        """
        if self._orders:
            return [order for order in self._orders if order["side"] == "sell"]
        return []

    async def get_positions(self, symbol: Optional[str] = None) -> list:
        """
        获取当前持仓信息
        Args:
            symbol (Optional[str]): 交易对，若提供则过滤
        Returns:
            list: 当前持仓列表，若无则为 []
        """
        positions: list = []

        if self._positions:
            positions = list(self._positions)
        elif self._position:
            positions = [self._position]
        elif self._order_stream and self._order_stream.auth:
            positions = await query_positions(self._order_stream.auth, symbol=symbol)

        if symbol:
            positions = [pos for pos in positions if pos.get("symbol") == symbol]

        self._positions = positions
        return positions

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

    def on_login(self, data):
        """
        处理登录成功回调
        Args:
            data (dict): 登录成功数据
        """
        logger.info("WebSocket 登录成功: %s", data)

    def on_new_order(self, data):
        """
        处理新订单回调
        Args:
            data (dict): 新订单数据
        """
        logger.info("通过订单流下单成功: %s", data)

    async def connect_order_stream(self, auth):
        """
        连接订单和持仓 WebSocket（需要认证）
        Returns:
            StandXOrderStream: 已连接的订单数据流对象
        """
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

    # 新增一个方法，通过市价订单将持仓平掉
    async def close_position(self, symbol: str):
        """
        通过市价订单平掉当前持仓
        Args:
            symbol (str): 交易对
        """
        positions = await self.get_positions(symbol=symbol)
        has_position = False

        for position in positions:
            if not position:
                continue
            qty_value = float(position.get("qty", 0))
            if qty_value == 0:
                continue

            has_position = True
            side = "sell" if qty_value > 0 else "buy"
            qty = str(abs(qty_value))

            logger.info(
                "准备通过市价单平仓: symbol=%s, side=%s, qty=%s",
                symbol,
                side,
                qty,
            )

            await self.new_order(
                symbol=symbol,
                side=side,
                order_type="market",
                qty=qty,
                reduce_only=True,
            )

        if not has_position:
            logger.info("当前无持仓，无需平仓")
