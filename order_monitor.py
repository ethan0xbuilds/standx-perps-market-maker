"""
StandX Order Monitor - WebSocket客户端监听订单状态
用于获取异步订单的真实执行结果（接受/拒绝）
"""

import json
import time
import threading
from websocket import WebSocketApp
from standx_auth import StandXAuth
import os
from dotenv import load_dotenv

load_dotenv()

# WebSocket endpoints
WS_MARKET_STREAM = "wss://perps.standx.com/ws-stream/v1"

class OrderMonitor:
    """监听订单状态变化的WebSocket客户端"""
    
    def __init__(self, token: str):
        """
        初始化订单监听器
        
        Args:
            token: JWT认证令牌
        """
        self.token = token
        self.ws = None
        self.connected = False
        self.authenticated = False
        self.orders_received = []
        
    def on_message(self, ws, message):
        """处理WebSocket消息"""
        try:
            data = json.loads(message)
            channel = data.get("channel")
            
            if channel == "auth":
                # 认证响应
                auth_data = data.get("data", {})
                if auth_data.get("code") in [0, 200]:
                    self.authenticated = True
                    print("\n✅ WebSocket认证成功")
                else:
                    print(f"\n❌ WebSocket认证失败: {auth_data}")
                    
            elif channel == "order":
                # 订单更新
                order_data = data.get("data", {})
                self.orders_received.append(order_data)
                
                status = order_data.get("status")
                cl_ord_id = order_data.get("cl_ord_id", "N/A")
                side = order_data.get("side")
                symbol = order_data.get("symbol")
                qty = order_data.get("qty")
                price = order_data.get("price")
                
                print(f"\n📬 订单更新:")
                print(f"  - cl_ord_id: {cl_ord_id}")
                print(f"  - 状态: {status}")
                print(f"  - 交易对: {symbol}")
                print(f"  - 方向: {side}")
                print(f"  - 数量: {qty}")
                print(f"  - 价格: {price}")
                
                if status in ["filled", "partially_filled"]:
                    fill_qty = order_data.get("fill_qty")
                    fill_avg_price = order_data.get("fill_avg_price")
                    print(f"  - 成交数量: {fill_qty}")
                    print(f"  - 成交均价: {fill_avg_price}")
                elif status == "rejected":
                    print(f"  - ⚠️ 订单被拒绝")
                elif status == "canceled":
                    print(f"  - 订单已取消")
                    
            else:
                # 其他消息类型
                print(f"\n📨 收到消息 [{channel}]: {json.dumps(data, indent=2)}")
                
        except Exception as e:
            print(f"\n❌ 处理消息失败: {e}")
            print(f"原始消息: {message}")
    
    def on_error(self, ws, error):
        """处理WebSocket错误"""
        print(f"\n❌ WebSocket错误: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """WebSocket连接关闭"""
        self.connected = False
        self.authenticated = False
        print(f"\n🔌 WebSocket连接关闭 (code: {close_status_code}, msg: {close_msg})")
    
    def on_open(self, ws):
        """WebSocket连接建立"""
        self.connected = True
        print("\n✅ WebSocket连接已建立")
        
        # 发送认证请求
        auth_request = {
            "auth": {
                "token": self.token,
                "streams": [{"channel": "order"}]
            }
        }
        ws.send(json.dumps(auth_request))
        print("📤 已发送认证请求并订阅order频道")
    
    def start(self):
        """启动WebSocket客户端"""
        self.ws = WebSocketApp(
            WS_MARKET_STREAM,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        
        # 在单独的线程中运行WebSocket
        ws_thread = threading.Thread(target=self.ws.run_forever)
        ws_thread.daemon = True
        ws_thread.start()
        
        # 等待连接和认证
        timeout = 10
        start_time = time.time()
        while not self.authenticated and time.time() - start_time < timeout:
            time.sleep(0.1)
        
        if not self.authenticated:
            raise Exception("WebSocket认证超时")
    
    def stop(self):
        """停止WebSocket客户端"""
        if self.ws:
            self.ws.close()


def main():
    """演示：启动订单监听器并下单"""
    print("=" * 60)
    print("StandX Order Monitor - 实时监听订单状态")
    print("=" * 60)
    
    # 加载私钥
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    if not private_key:
        raise ValueError("未找到WALLET_PRIVATE_KEY")
    
    # 认证
    print("\n[1/3] 正在认证...")
    auth = StandXAuth(private_key)
    auth_response = auth.authenticate()
    token = auth_response.get("token")
    print(f"✅ 认证成功，获取JWT令牌")
    
    # 启动订单监听器
    print("\n[2/3] 启动WebSocket订单监听器...")
    monitor = OrderMonitor(token)
    monitor.start()
    print("✅ 订单监听器已启动")
    
    # 下单测试
    print("\n[3/3] 下单测试...")
    
    # 获取价格和持仓配置
    symbol = os.getenv("LIMIT_ORDER_SYMBOL", "BTC-USD")
    price = auth.query_symbol_price(symbol)
    positions = auth.query_positions(symbol=symbol)
    
    # 提取持仓配置
    position = positions[0] if positions else None
    current_leverage = int(position["leverage"]) if position else None
    current_margin_mode = position["margin_mode"] if position else None
    
    # 计算限价单价格
    bps = int(os.getenv("LIMIT_ORDER_BPS", "50"))
    side = os.getenv("LIMIT_ORDER_SIDE", "buy").lower()
    qty = float(os.getenv("LIMIT_ORDER_QTY", "0.001"))
    
    base_price_f = float(price.get("mid_price") or price.get("mark_price"))
    sign = -1 if side == "buy" else 1
    limit_price = base_price_f * (1 + sign * (bps / 10000))
    limit_price_str = f"{limit_price:.2f}"
    qty_str = f"{qty:.4f}"
    
    print(f"\n📝 订单参数:")
    print(f"  - 交易对: {symbol}")
    print(f"  - 方向: {side}")
    print(f"  - 数量: {qty_str}")
    print(f"  - 价格: {limit_price_str} (市场价: {base_price_f:.2f})")
    print(f"  - 杠杆: {current_leverage}x")
    print(f"  - 保证金模式: {current_margin_mode}")
    
    # 下单
    order_resp = auth.new_limit_order(
        symbol=symbol,
        side=side,
        qty=qty_str,
        price=limit_price_str,
        time_in_force="gtc",
        reduce_only=False,
        margin_mode=current_margin_mode,
        leverage=current_leverage,
    )
    
    request_id = order_resp.get("request_id")
    print(f"\n✅ 订单已提交")
    print(f"  - request_id: {request_id}")
    print(f"  - HTTP响应: {order_resp}")
    
    # 等待订单更新
    print(f"\n⏳ 等待订单状态更新（最多30秒）...")
    timeout = 30
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        if monitor.orders_received:
            print(f"\n✅ 收到 {len(monitor.orders_received)} 条订单更新")
            break
        time.sleep(1)
    else:
        print(f"\n⚠️ 等待超时，未收到订单更新")
    
    # 再次查询订单列表
    print(f"\n📋 查询订单列表...")
    open_orders = auth.query_open_orders(symbol=symbol, limit=10)
    if open_orders.get("result"):
        print(f"✅ 找到 {len(open_orders['result'])} 个待处理订单:")
        for ord in open_orders["result"]:
            print(f"  - {ord['cl_ord_id']}: {ord['status']} @ {ord['price']} qty={ord['qty']}")
    else:
        print(f"  (无待处理订单)")
    
    # 停止监听器
    print(f"\n🛑 停止监听器...")
    monitor.stop()
    time.sleep(1)
    
    print(f"\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
