"""测试取消订单功能"""

import os
import time
from dotenv import load_dotenv
from standx_auth import StandXAuth

load_dotenv()


def main():
    print("=" * 60)
    print("测试取消订单功能")
    print("=" * 60)
    
    # 认证
    print("\n[认证] 连接到StandX...")
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    auth = StandXAuth(private_key)
    auth.authenticate()
    
    symbol = "BTC-USD"
    
    # 查询当前订单
    print(f"\n[1] 查询当前待处理订单...")
    open_orders = auth.query_open_orders(symbol=symbol)
    
    if not open_orders.get("result"):
        print("❌ 没有待处理订单，先创建一个测试订单...")
        
        # 获取价格和持仓信息
        price = auth.query_symbol_price(symbol)
        positions = auth.query_positions(symbol=symbol)
        position = positions[0] if positions else None
        
        # 下一个测试订单（低于市价0.5%，不会成交）
        base_price = float(price.get("mid_price"))
        order_price = base_price * 0.995
        
        print(f"   创建测试订单: buy 0.001 @ {order_price:.2f} (市价: {base_price:.2f})")
        
        order_resp = auth.new_limit_order(
            symbol=symbol,
            side="buy",
            qty="0.001",
            price=f"{order_price:.2f}",
            time_in_force="gtc",
            reduce_only=False,
            margin_mode=position["margin_mode"] if position else "cross",
            leverage=int(position["leverage"]) if position else 40,
        )
        print(f"   ✅ 订单已创建: {order_resp.get('request_id')}")
        
        # 等待订单被记录
        print(f"   ⏳ 等待5秒让订单生效...")
        time.sleep(5)
        open_orders = auth.query_open_orders(symbol=symbol)
    
    # 显示订单
    orders = open_orders.get("result", [])
    print(f"\n✅ 当前有 {len(orders)} 个待处理订单:")
    for i, ord in enumerate(orders, 1):
        print(f"\n   订单 #{i}:")
        print(f"   - order_id: {ord['id']}")
        print(f"   - cl_ord_id: {ord['cl_ord_id']}")
        print(f"   - 状态: {ord['status']}")
        print(f"   - 详情: {ord['side']} {ord['qty']} @ {ord['price']}")
    
    if not orders:
        print("❌ 没有可取消的订单")
        return
    
    # 取消第一个订单
    first_order = orders[0]
    order_id = first_order["id"]
    cl_ord_id = first_order["cl_ord_id"]
    
    print(f"\n[2] 取消订单...")
    print(f"   - order_id: {order_id}")
    print(f"   - cl_ord_id: {cl_ord_id}")
    
    cancel_resp = auth.cancel_order(order_id=order_id)
    print(f"\n✅ 取消请求已提交")
    print(f"   - request_id: {cancel_resp.get('request_id')}")
    print(f"   - code: {cancel_resp.get('code')}")
    print(f"   - message: {cancel_resp.get('message')}")
    
    # 等待取消生效
    print(f"\n[3] 等待5秒后验证取消结果...")
    time.sleep(5)
    
    try:
        order_status = auth.query_order(order_id=order_id)
        status = order_status.get('status')
        print(f"   订单状态: {status}")
        if status == 'canceled':
            print("   ✅ 订单已成功取消！")
        else:
            print(f"   ⚠️ 订单状态为: {status}")
    except Exception as e:
        if "404" in str(e):
            print("   ✅ 订单已被删除（取消成功）")
        else:
            print(f"   ❌ 查询失败: {e}")
    
    # 再次查询待处理订单
    print(f"\n[4] 查询剩余待处理订单...")
    open_orders = auth.query_open_orders(symbol=symbol)
    remaining = open_orders.get("result", [])
    print(f"   剩余 {len(remaining)} 个待处理订单")
    
    if remaining:
        for i, ord in enumerate(remaining, 1):
            print(f"   #{i}: {ord['cl_ord_id']} - {ord['status']}")
    
    print(f"\n" + "=" * 60)
    print("✅ 测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
