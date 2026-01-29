import asyncio
from typing import Optional
from api.ws_client import StandXMarketStream
from logger import get_logger
logger = get_logger(__name__)

class StandXAdapter():
    def __init__(self):
        self.market_stream: Optional[StandXMarketStream] = None
        self._depth_mid_price: float = None
        self._price_subscription_active: bool = False

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

                if mid_price is not None:
                    self._depth_mid_price = mid_price
                    logger.info(
                        "收到 depth_book 价格: mid=%.4f (bid=%.4f, ask=%.4f)",
                        mid_price,
                        best_bid or 0.0,
                        best_ask or 0.0,
                    )
        except Exception as e:
            logger.warning("处理 depth_book 数据失败: %s", e)


