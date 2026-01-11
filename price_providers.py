"""
价格数据提供者模块
支持 HTTP 轮询和 WebSocket 实时推送两种方式
"""

import time
import json
import threading
from abc import ABC, abstractmethod
from typing import Optional
import websocket as ws
from standx_auth import StandXAuth
import standx_api as api


class PriceProvider(ABC):
    """价格数据提供者抽象基类"""
    
    @abstractmethod
    def get_current_price(self) -> float:
        """获取当前价格"""
        pass
    
    @abstractmethod
    def cleanup(self):
        """清理资源"""
        pass


class HttpPriceProvider(PriceProvider):
    """HTTP 价格提供者 - 使用轮询方式获取价格"""
    
    def __init__(self, auth: StandXAuth, symbol: str):
        """
        初始化 HTTP 价格提供者
        
        Args:
            auth: 认证后的 StandXAuth 实例
            symbol: 交易对符号
        """
        self.auth = auth
        self.symbol = symbol
    
    def get_current_price(self) -> float:
        """通过 HTTP API 获取当前价格（优先 mark_price）"""
        try:
            price_data = api.query_symbol_price(self.auth, self.symbol)
            mark_price = price_data.get("mark_price")
            mid_price = price_data.get("mid_price")
            price = float(mark_price or mid_price)
            if not price or price <= 0:
                raise ValueError(f"Invalid price: {price}")
            return price
        except Exception as e:
            print(f"  ⚠️ HTTP 获取价格失败: {e}")
            raise
    
    def cleanup(self):
        """HTTP 模式无需清理"""
        pass


class WebSocketPriceProvider(PriceProvider):
    """WebSocket 价格提供者 - 使用实时推送方式获取价格"""
    
    def __init__(self, auth: StandXAuth, symbol: str):
        """
        初始化 WebSocket 价格提供者
        
        Args:
            auth: 认证后的 StandXAuth 实例
            symbol: 交易对符号
        """
        self.auth = auth
        self.symbol = symbol
        self._latest_price: Optional[float] = None
        self._lock = threading.Lock()
        self._ws = None
        self._ws_thread = None
        self._running = False
        
        # 启动 WebSocket 连接
        self._start_websocket()
    
    def _start_websocket(self):
        """启动 WebSocket 连接线程"""
        self._running = True
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()
        print(f"  🔌 WebSocket 价格订阅启动中...")
        
        # 等待首次价格推送（最多 10 秒）
        if not self._wait_ready(timeout=10):
            raise TimeoutError("WebSocket 连接超时，未能获取初始价格")
    
    def _wait_ready(self, timeout: float = 10) -> bool:
        """等待 WebSocket 准备就绪（收到首次价格）"""
        start = time.time()
        while self._latest_price is None:
            if time.time() - start > timeout:
                return False
            time.sleep(0.1)
        print(f"  ✅ WebSocket 已连接，当前价格: {self._latest_price:.2f}")
        return True
    
    def _ws_loop(self):
        """WebSocket 连接循环（后台线程）"""
        while self._running:
            try:
                self._connect_and_subscribe()
            except Exception as e:
                print(f"  ⚠️ WebSocket 连接失败: {e}，3秒后重连...")
                time.sleep(3)
    
    def _connect_and_subscribe(self):
        """建立 WebSocket 连接并订阅价格"""
        # WebSocket URL (根据 StandX 文档：Market Stream)
        ws_url = "wss://perps.standx.com/ws-stream/v1"
        
        # 创建 WebSocket 连接（不需要在 header 中传 Authorization）
        self._ws = ws.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        
        # 运行 WebSocket（会阻塞直到连接关闭）
        self._ws.run_forever()
    
    def _on_open(self, ws):
        """WebSocket 连接建立回调"""
        print(f"  🔌 WebSocket 已连接")
        
        # StandX 的 price channel 是公开的，无需认证
        # 直接订阅价格更新
        subscribe_msg = {
            "subscribe": {
                "channel": "price",
                "symbol": self.symbol
            }
        }
        ws.send(json.dumps(subscribe_msg))
        print(f"  📡 已订阅 {self.symbol} 价格推送")
    
    def _on_message(self, ws, message):
        """WebSocket 消息接收回调"""
        try:
            data = json.loads(message)
            
            # 根据 StandX 文档解析价格数据
            # 响应格式: {"seq": 13, "channel": "price", "symbol": "BTC-USD", "data": {...}}
            if data.get("channel") == "price" and data.get("symbol") == self.symbol:
                # 提取 mark_price 或 last_price
                price_data = data.get("data", {})
                mark_price = price_data.get("mark_price")
                last_price = price_data.get("last_price")
                price = float(mark_price or last_price)
                
                if price and price > 0:
                    with self._lock:
                        self._latest_price = price
                    # 可选：打印价格更新（调试用）
                    # print(f"  📊 WS 价格更新: {price:.2f}")
        
        except Exception as e:
            print(f"  ⚠️ WebSocket 消息解析失败: {e}, 原始消息: {message}")
    
    def _on_error(self, ws, error):
        """WebSocket 错误回调"""
        print(f"  ❌ WebSocket 错误: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket 关闭回调"""
        print(f"  🔌 WebSocket 连接关闭: {close_status_code} - {close_msg}")
    
    def get_current_price(self) -> float:
        """获取最新价格（从内存读取，几乎零延迟）"""
        with self._lock:
            if self._latest_price is None:
                raise ValueError("WebSocket 价格数据尚未准备好")
            return self._latest_price
    
    def cleanup(self):
        """清理 WebSocket 连接"""
        print(f"  🔌 关闭 WebSocket 连接...")
        self._running = False
        if self._ws:
            self._ws.close()
        if self._ws_thread:
            self._ws_thread.join(timeout=5)


def create_price_provider(price_source: str, auth: StandXAuth, symbol: str) -> PriceProvider:
    """
    工厂函数：根据配置创建价格提供者
    
    Args:
        price_source: "http" 或 "websocket"
        auth: 认证后的 StandXAuth 实例
        symbol: 交易对符号
        
    Returns:
        PriceProvider 实例
    """
    if price_source == "websocket":
        return WebSocketPriceProvider(auth, symbol)
    elif price_source == "http":
        return HttpPriceProvider(auth, symbol)
    else:
        raise ValueError(f"不支持的价格数据源: {price_source}，请使用 'http' 或 'websocket'")
