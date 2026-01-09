#!/usr/bin/env python
"""测试平仓逻辑"""

import os
from dotenv import load_dotenv
from standx_auth import StandXAuth

load_dotenv()

def test_close():
    # 认证
    auth = StandXAuth(os.getenv('WALLET_PRIVATE_KEY'))
    auth.authenticate()
    print("✅ 认证成功\n")
    
    # 查询持仓
    print("=" * 60)
    print("查询持仓")
    print("=" * 60)
    positions = auth.query_positions(symbol='BTC-USD')
    print(f"持仓数量: {len(positions)}")
    
    if positions:
        for i, pos in enumerate(positions):
            print(f"\n持仓 #{i+1}:")
            print(f"  symbol: {pos.get('symbol')}")
            print(f"  side: {pos.get('side')}")
            print(f"  qty: {pos.get('qty')} (类型: {type(pos.get('qty'))})")
            print(f"  entry_price: {pos.get('entry_price')}")
            print(f"  完整数据: {pos}")
    else:
        print("⚠️ 无持仓")
    
    # 查询订单
    print("\n" + "=" * 60)
    print("查询订单")
    print("=" * 60)
    orders_resp = auth.query_open_orders(symbol='BTC-USD')
    orders = orders_resp.get('result', [])
    print(f"订单数量: {len(orders)}")
    
    if orders:
        for i, order in enumerate(orders):
            print(f"\n订单 #{i+1}:")
            print(f"  side: {order.get('side')}")
            print(f"  qty: {order.get('qty')}")
            print(f"  price: {order.get('price')}")
            print(f"  status: {order.get('status')}")
    else:
        print("⚠️ 无订单")
    
    # 测试平仓逻辑
    if positions:
        print("\n" + "=" * 60)
        print("测试平仓逻辑")
        print("=" * 60)
        
        position = positions[0]
        qty = position.get("qty")
        side = position.get("side")
        margin_mode = position.get("margin_mode")
        leverage = int(position.get("leverage")) if position.get("leverage") else None
        
        print(f"持仓方向: {side}")
        print(f"持仓数量: {qty}")
        print(f"qty 布尔值: {bool(qty)}")
        print(f"float(qty): {float(qty) if qty else 'None'}")
        print(f"float(qty) > 0: {float(qty) > 0 if qty else 'False'}")
        
        # 平仓方向由 qty 正负判断（side 可能为 None）
        if not qty:
            print("\n❌ 无持仓数量，跳过")
            return

        qty_f = float(qty)
        if qty_f > 0:
            close_side = "sell"
            qty_send = qty
        elif qty_f < 0:
            close_side = "buy"
            qty_send = f"{abs(qty_f):.4f}"
        else:
            print("\n❌ 持仓数量为0，跳过")
            return

        print(f"\n✅ 条件满足，应该平仓: {close_side} {qty_send}")

        # 确认是否执行
        confirm = input("\n是否执行市价平仓？ (yes/no): ")
        if confirm.lower() == 'yes':
            try:
                close_resp = auth.new_market_order(
                    symbol='BTC-USD',
                    side=close_side,
                    qty=qty_send,
                    reduce_only=True,
                    margin_mode=margin_mode,
                    leverage=leverage,
                    time_in_force='ioc',
                )
                print(f"✅ 平仓请求已提交: {close_resp}")
                # 查询订单状态
                try:
                    ord = auth.query_order(cl_ord_id=close_resp.get('request_id'))
                    print(f"📋 订单状态: {ord}")
                except Exception as e:
                    print(f"⚠️ 查询订单状态失败: {e}")
                # 验证持仓是否归零
                import time as _t
                start = _t.time()
                while _t.time() - start < 10:
                    _t.sleep(1)
                    latest = auth.query_positions(symbol='BTC-USD')
                    if not latest:
                        print("✅ 持仓已清空")
                        break
                        lqty = float(latest[0].get('qty') or 0)
                        if lqty == 0:
                            print("✅ 持仓数量为 0（已平仓）")
                            break
                else:
                    print("⚠️ 超时：持仓仍未归零")
            except Exception as e:
                print(f"❌ 平仓失败: {e}")
        else:
            # 不会进入此分支，已提前返回
            pass

if __name__ == "__main__":
    test_close()
