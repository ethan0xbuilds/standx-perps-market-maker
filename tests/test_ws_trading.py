#!/usr/bin/env python3
"""
WebSocket 下单 Demo - 基础版
演示如何通过 WebSocket 订阅事件和下单

使用方式：
    python tests/test_ws_trading.py
"""

import os
import sys
import json
import time
import threading
from typing import Optional

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import websocket as ws

from standx_auth import StandXAuth
import standx_api as api
from logger import get_logger, configure_logging

logger = get_logger(__name__)
load_dotenv()
configure_logging()

WS_URL = "wss://perps.standx.com/ws-stream/v1"


class SimpleWSClient:
    """简化的 WebSocket 客户端"""

    def __init__(self, auth: StandXAuth, symbol: str):
        self.auth = auth
        self.symbol = symbol
        self.ws = None
        self._running = False
        self._ws_thread = None
        self._depth_mid_price: Optional[float] = None
        self._depth_ready = threading.Event()
        self._depth_subscribed = False

    def start(self, timeout: float = 5):
        """启动 WebSocket 连接"""
        logger.info("正在连接 WebSocket...")
        self._running = True
        self._depth_ready.clear()
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        self._ws_thread.start()

        # 等待连接建立
        time.sleep(timeout)
        if not self.ws:
            raise TimeoutError("WebSocket 连接超时")
        logger.info("WebSocket 已连接")

    def _run_ws(self):
        """后台线程运行 WebSocket"""
        while self._running:
            try:
                self.ws = ws.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                if self._running:
                    logger.error("WebSocket 错误: %s，3秒后重连...", e)
                    time.sleep(3)

    def _on_open(self, ws_conn):
        """连接打开 - 发送认证"""
        logger.info("WebSocket 已连接，发送认证...")

        auth_msg = {
            "auth": {"token": self.auth.token, "request_id": self.auth.request_id}
        }
        ws_conn.send(json.dumps(auth_msg))

        # depth_book 是公有频道，立即订阅（无需等认证）
        self._subscribe_depth(ws_conn)

    def _on_message(self, ws_conn, message: str):
        """接收消息"""
        logger.info("收到 WS 消息: %s", message[:500])
        try:
            data = json.loads(message)

            # 订阅响应
            if "subscribe" in data:
                result = data.get("subscribe", {})
                logger.info(
                    "订阅响应: code=%s, msg=%s", result.get("code"), result.get("msg")
                )

            # 频道消息
            elif "channel" in data:
                channel = data.get("channel")
                msg_data = data.get("data", {})

                if channel == "order":
                    status = msg_data.get("status")
                    order_id = msg_data.get("order_id")
                    filled_qty = msg_data.get("filled_qty", "0")
                    logger.info(
                        "订单更新 [#%s]: 状态=%s, 成交量=%s",
                        order_id,
                        status,
                        filled_qty,
                    )

                elif channel == "position":
                    symbol = msg_data.get("symbol")
                    qty = msg_data.get("qty")
                    logger.info("持仓更新 [%s]: %s", symbol, qty)

                elif channel == "balance":
                    balance = msg_data.get("balance")
                    logger.info("余额更新: %s", balance)

                elif channel == "auth":
                    code = msg_data.get("code")
                    if code == 0:
                        logger.info("认证成功 (channel 消息)")
                        self._subscribe_channels(ws_conn)
                    else:
                        logger.error("认证失败 (channel 消息): %s", msg_data)

                elif channel == "depth_book":
                    bids = msg_data.get("bids") or []
                    asks = msg_data.get("asks") or []

                    def _first_price(levels):
                        if not levels:
                            return None
                        first = levels[0]
                        # level 可能是 [price, qty] 或 {"price": , "qty": }
                        if isinstance(first, (list, tuple)) and first:
                            return float(first[0])
                        if isinstance(first, dict):
                            price = first.get("price")
                            return float(price) if price is not None else None
                        return None

                    best_bid = _first_price(bids)
                    best_ask = _first_price(asks)

                    mid_price: Optional[float] = None
                    if best_bid is not None and best_ask is not None:
                        mid_price = (best_bid + best_ask) / 2
                    elif best_bid is not None:
                        mid_price = best_bid
                    elif best_ask is not None:
                        mid_price = best_ask

                    if mid_price is not None:
                        self._depth_mid_price = mid_price
                        self._depth_ready.set()
                        logger.info(
                            "收到 depth_book 价格: mid=%.4f (bid=%.4f, ask=%.4f)",
                            mid_price,
                            best_bid or 0.0,
                            best_ask or 0.0,
                        )

                else:
                    # 未识别的频道
                    logger.info("未识别的频道: %s", channel)

            # 既没有 auth 也没有 channel 的消息
            else:
                logger.info("未识别的消息类型: %s", json.dumps(data)[:200])

        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.error("消息处理错误: %s", e)

    def _on_error(self, ws_conn, error):
        """错误回调"""
        logger.error("WebSocket 错误: %s", error)

    def _on_close(self, ws_conn, close_status_code, close_msg):
        """关闭回调"""
        logger.info("WebSocket 关闭: %s", close_status_code)

    def _subscribe_channels(self, ws_conn):
        """订阅事件频道（需要认证）"""
        for channel in ["order", "position", "balance"]:
            msg = json.dumps({"subscribe": {"channel": channel}})
            ws_conn.send(msg)
            logger.info("已订阅: %s", channel)

        # 认证后再订阅一次 depth_book（幂等）
        self._subscribe_depth(ws_conn)

    def _subscribe_depth(self, ws_conn):
        """订阅 depth_book（公有频道，幂等）"""
        if self._depth_subscribed:
            return

        depth_msg = json.dumps(
            {"subscribe": {"channel": "depth_book", "symbol": self.symbol, "depth": 5}}
        )
        ws_conn.send(depth_msg)
        self._depth_subscribed = True
        logger.info("已订阅: depth_book (%s)", self.symbol)

    def stop(self):
        """停止连接"""
        logger.info("关闭 WebSocket...")
        self._running = False
        if self.ws:
            self.ws.close()
        if self._ws_thread:
            self._ws_thread.join(timeout=5)

    def wait_depth_mid_price(self, timeout: float = 20) -> Optional[float]:
        """等待 depth_book 返回的中间价"""
        if self._depth_mid_price is not None:
            return self._depth_mid_price
        if self._depth_ready.wait(timeout=timeout):
            return self._depth_mid_price

        # 超时时记录状态
        logger.warning(
            "等待 depth_book 超时: subscribed=%s, price=%s",
            self._depth_subscribed,
            self._depth_mid_price,
        )
        return None


def main():
    """基础下单 demo"""
    logger.info("\n" + "=" * 60)
    logger.info("WebSocket 下单 Demo")
    logger.info("=" * 60 + "\n")

    # 初始化认证
    try:
        auth = StandXAuth(
            private_key=os.getenv("WALLET_PRIVATE_KEY"),
            ed25519_key=os.getenv("ED25519_PRIVATE_KEY"),
            token=os.getenv("ACCESS_TOKEN"),
        )
        logger.info("认证成功\n")
    except Exception as e:
        logger.error("认证失败: %s", e)
        return

    symbol = "BTC-USD"

    # 启动 WebSocket
    client = SimpleWSClient(auth, symbol)
    try:
        client.start()
    except Exception as e:
        logger.error("WebSocket 连接失败: %s", e)
        return

    try:
        # 通过 depth_book 获取价格
        logger.info("等待 depth_book 价格...")
        mark_price = client.wait_depth_mid_price(timeout=20)
        if mark_price is None:
            raise TimeoutError("未在超时时间内收到 depth_book 价格")
        logger.info("当前价格(WS): %.2f\n", mark_price)

        # 下限价单
        qty = "0.0001"
        bid_price = f"{mark_price * 0.95:.2f}"  # 比市价低 5%

        logger.info("准备下单:")
        logger.info("   交易对: %s", symbol)
        logger.info("   方向: BUY")
        logger.info("   数量: %s", qty)
        logger.info("   价格: %s\n", bid_price)

        result = api.new_limit_order(
            auth,
            symbol=symbol,
            side="buy",
            qty=qty,
            price=bid_price,
            time_in_force="gtc",
        )

        order_id = result.get("order_id")
        logger.info("订单已下达 [order_id=%s]\n", order_id)

        # 监听 WebSocket 事件 30 秒
        logger.info("监听 WebSocket 事件推送 (30秒)...")
        logger.info("   订单状态变化: new -> partial_fill -> filled / cancelled\n")
        time.sleep(30)

    except Exception as e:
        logger.error("执行失败: %s", e)
    finally:
        client.stop()
        logger.info("\nDemo 完成")


if __name__ == "__main__":
    import signal

    def signal_handler(sig, frame):
        logger.info("\n收到信号，正在退出...")
        exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    main()
