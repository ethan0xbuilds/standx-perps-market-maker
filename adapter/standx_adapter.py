import asyncio
import os
import time
from typing import Optional
from api.ws_client import StandXMarketStream
from logger import get_logger

logger = get_logger(__name__)


class StandXAdapter:
    def __init__(self):
        self.market_stream: Optional[StandXMarketStream] = None
        self._depth_mid_price: float = None
        self._last_price_update_time: float = None
        self._orders = []
        self._buy_orders = []
        self._sell_orders = []

    async def connect_market_stream(self) -> StandXMarketStream:
        """连接市场 WebSocket（公共频道无需认证）"""
        if not self.market_stream:
            self.market_stream = StandXMarketStream()
        if not self.market_stream.connected:
            await self.market_stream.connect()

    async def subscribe_market(self, channel: str, symbol: str, callback=None):
        """订阅市场 WebSocket 频道"""
        await self.connect_market_stream()
        await self.market_stream.subscribe(channel, symbol, callback=callback)

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

                if mid_price is not None and mid_price != self._depth_mid_price:
                    self._depth_mid_price = mid_price
                    # 计算新价格距离上次价格更新的时间间隔
                    if self._last_price_update_time is not None:
                        time_diff = time.time() - self._last_price_update_time
                        logger.info(
                            "Depth book 中间价更新: %.4f, 距上次更新 %.2f 秒",
                            mid_price,
                            time_diff,
                        )
                    self._last_price_update_time = time.time()
        except Exception as e:
            logger.exception("处理 depth_book 数据失败: %s", e)

    async def subscribe_depth_book(self, symbol: str = "BTC-USD"):
        await self.subscribe_market(
            channel="depth_book", symbol=symbol, callback=self.on_depth_book
        )

    def get_depth_mid_price(self) -> Optional[float]:
        return self._depth_mid_price

    async def on_order(self, data):
        """处理 order 频道推送"""
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
                # 将订单数据存储到 market_stream.orders 列表中
                for idx, order in enumerate(self._orders):
                    if order["id"] == order_data.get("id") and order_data.get("status") == "canceled":
                        self._orders.pop(idx)
                        logger.info("订单已取消，移除订单 id=%s", order_data.get("id"))
                        logger.info("当前订单总数: %d", len(self._orders))
                        break
                else:
                    logger.info("新增订单 id=%s", order_data.get("id"))
                    self._orders.append(order_data)
                    logger.info("当前订单总数: %d", len(self._orders))
        except Exception as e:
            logger.exception("处理 order 数据失败: %s", e)

    async def authenticate_and_subscribe_orders(self):
        """认证并订阅订单频道"""
        await self.connect_market_stream()
        await self.market_stream.authenticate(os.getenv("ACCESS_TOKEN") , [{"channel": "order"}])
        await self.market_stream.subscribe("order", callback=self.on_order)

    def get_buy_order_count(self) -> int:
        if self._orders:
            return sum(1 for order in self._orders if order["side"] == "buy")
        return 0
    
    def get_order_count(self) -> int:
        return len(self._orders) if self._orders else 0

    def get_sell_order_count(self) -> int:
        if self._orders:
            return sum(1 for order in self._orders if order["side"] == "sell")
        return 0
    
    def get_orders(self) -> list:
        return self._orders if self._orders else []
    
    def get_buy_orders(self) -> list:
        if self._orders:
            return [order for order in self._orders if order["side"] == "buy"]
        return []
    
    def get_sell_orders(self) -> list:
        if self._orders:
            return [order for order in self._orders if order["side"] == "sell"]
        return []