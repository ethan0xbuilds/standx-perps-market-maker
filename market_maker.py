"""
双向限价单做市策略
- 同时挂买单和卖单
- 监控价格变化
- 当订单价格偏离超过70bps时，取消并重新挂50bps的单
"""

import os
import time
from dotenv import load_dotenv
from standx_auth import StandXAuth

load_dotenv()


class MarketMaker:
    """双向限价单做市器"""
    
    def __init__(self, auth: StandXAuth, symbol: str, qty: str, target_bps: float = 7.5, max_bps: float = 10):
        """
        初始化做市器
        
        Args:
            auth: 认证后的StandXAuth实例
            symbol: 交易对
            qty: 订单数量（字符串格式）
            target_bps: 目标挂单偏离（basis points，默认7.5保持2.5bps缓冲）
            max_bps: 最大允许偏离（超过后重新挂单，默认10符合奖励资格）
        """
        self.auth = auth
        self.symbol = symbol
        self.qty = qty
        self.target_bps = target_bps
        self.max_bps = max_bps
        
        # 获取持仓配置
        positions = auth.query_positions(symbol=symbol)
        position = positions[0] if positions else None
        self.leverage = int(position["leverage"]) if position else 40
        self.margin_mode = position["margin_mode"] if position else "cross"
        
        # 当前订单
        self.buy_order = None
        self.sell_order = None
        
    def get_current_price(self) -> float:
        """获取当前市场价格（优先mark_price，因奖励资格基于mark_price计算）"""
        try:
            price_data = self.auth.query_symbol_price(self.symbol)
            mark_price = price_data.get("mark_price")
            mid_price = price_data.get("mid_price")
            price = float(mark_price or mid_price)
            if not price or price <= 0:
                raise ValueError(f"Invalid price: {price}")
            return price
        except Exception as e:
            print(f"  ⚠️ 获取价格失败: {e}，将在下次迭代重试")
            raise
    
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
    
    def place_orders(self, market_price: float):
        """下双向限价单"""
        buy_price, sell_price = self.calculate_order_prices(market_price)
        
        print(f"\n📝 下双向限价单 (市价: {market_price:.2f}):")
        
        # 下买单
        try:
            buy_resp = self.auth.new_limit_order(
                symbol=self.symbol,
                side="buy",
                qty=self.qty,
                price=f"{buy_price:.2f}",
                time_in_force="gtc",
                reduce_only=False,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )
            print(f"  ✅ 买单: {self.qty} @ {buy_price:.2f} (request_id: {buy_resp.get('request_id')})")
        except Exception as e:
            print(f"  ❌ 买单失败: {e}")
        
        # 下卖单
        try:
            sell_resp = self.auth.new_limit_order(
                symbol=self.symbol,
                side="sell",
                qty=self.qty,
                price=f"{sell_price:.2f}",
                time_in_force="gtc",
                reduce_only=False,
                margin_mode=self.margin_mode,
                leverage=self.leverage,
            )
            print(f"  ✅ 卖单: {self.qty} @ {sell_price:.2f} (request_id: {sell_resp.get('request_id')})")
        except Exception as e:
            print(f"  ❌ 卖单失败: {e}")
        
        # 等待订单生效
        time.sleep(3)
        self.refresh_orders()
    
    def refresh_orders(self):
        """刷新当前订单状态"""
        try:
            open_orders = self.auth.query_open_orders(symbol=self.symbol)
            orders = open_orders.get("result", [])
            
            self.buy_order = None
            self.sell_order = None
            
            for order in orders:
                if order["side"] == "buy":
                    self.buy_order = order
                elif order["side"] == "sell":
                    self.sell_order = order
        except Exception as e:
            print(f"  ⚠️ 刷新订单状态失败: {e}")
            # 不抛出异常，使用上次缓存的订单状态
    
    def check_and_adjust_orders(self, market_price: float) -> bool:
        """
        检查订单是否需要调整（选项B策略：成交后只补单不平仓）
        
        Args:
            market_price: 当前市场价格
            
        Returns:
            True if orders were adjusted, False otherwise
        """
        self.refresh_orders()
        
        adjusted = False
        orders_to_cancel = []
        missing_sides = []
        
        # 检查买单
        if self.buy_order:
            buy_price = float(self.buy_order["price"])
            buy_bps = abs((market_price - buy_price) / market_price * 10000)
            
            if buy_bps > self.max_bps:
                print(f"\n⚠️ 买单偏离过大: {buy_bps:.1f} bps (阈值: {self.max_bps} bps)")
                print(f"   订单价格: {buy_price:.2f}, 市价: {market_price:.2f}")
                orders_to_cancel.append(self.buy_order)
                adjusted = True
        else:
            # 买单缺失（可能成交了），需要补单
            print(f"\n💰 买单缺失（可能已成交），准备补单...")
            missing_sides.append("buy")
            adjusted = True
        
        # 检查卖单
        if self.sell_order:
            sell_price = float(self.sell_order["price"])
            sell_bps = abs((sell_price - market_price) / market_price * 10000)
            
            if sell_bps > self.max_bps:
                print(f"\n⚠️ 卖单偏离过大: {sell_bps:.1f} bps (阈值: {self.max_bps} bps)")
                print(f"   订单价格: {sell_price:.2f}, 市价: {market_price:.2f}")
                orders_to_cancel.append(self.sell_order)
                adjusted = True
        else:
            # 卖单缺失（可能成交了），需要补单
            print(f"\n💰 卖单缺失（可能已成交），准备补单...")
            missing_sides.append("sell")
            adjusted = True
        
        # 取消偏离过大的订单
        if orders_to_cancel:
            print(f"\n🗑️ 取消 {len(orders_to_cancel)} 个订单...")
            for order in orders_to_cancel:
                try:
                    cancel_resp = self.auth.cancel_order(order_id=order["id"])
                    print(f"  ✅ 取消成功: {order['side']} @ {order['price']} (request_id: {cancel_resp.get('request_id')})")
                except Exception as e:
                    print(f"  ❌ 取消失败: {e}")
            
            # 等待取消生效
            time.sleep(3)
            
            # 重新下单
            print(f"\n♻️ 重新挂{self.target_bps}bps限价单...")
            self.place_orders(market_price)
        elif missing_sides:
            # 只补缺失的单边（成交后不平仓策略）
            print(f"\n♻️ 补{', '.join(missing_sides)}单（{self.target_bps}bps）...")
            self.place_missing_orders(market_price, missing_sides)
        
        return adjusted
    
    def place_missing_orders(self, market_price: float, missing_sides: list):
        """只挂缺失的单边订单"""
        buy_price, sell_price = self.calculate_order_prices(market_price)
        
        # 补买单
        if "buy" in missing_sides:
            try:
                buy_resp = self.auth.new_limit_order(
                    symbol=self.symbol,
                    side="buy",
                    qty=self.qty,
                    price=f"{buy_price:.2f}",
                    time_in_force="gtc",
                    reduce_only=False,
                    margin_mode=self.margin_mode,
                    leverage=self.leverage,
                )
                print(f"  ✅ 买单: {self.qty} @ {buy_price:.2f} (request_id: {buy_resp.get('request_id')})")
            except Exception as e:
                print(f"  ❌ 买单失败: {e}")
        
        # 补卖单
        if "sell" in missing_sides:
            try:
                sell_resp = self.auth.new_limit_order(
                    symbol=self.symbol,
                    side="sell",
                    qty=self.qty,
                    price=f"{sell_price:.2f}",
                    time_in_force="gtc",
                    reduce_only=False,
                    margin_mode=self.margin_mode,
                    leverage=self.leverage,
                )
                print(f"  ✅ 卖单: {self.qty} @ {sell_price:.2f} (request_id: {sell_resp.get('request_id')})")
            except Exception as e:
                print(f"  ❌ 卖单失败: {e}")
        
        # 等待订单生效
        time.sleep(3)
        self.refresh_orders()
    
    def run(self, check_interval: int = 10, duration: int = None):
        """
        运行做市策略
        
        Args:
            check_interval: 检查间隔（秒）
            duration: 运行时长（秒），None表示无限运行
        """
        print("=" * 60)
        print("双向限价单做市策略启动")
        print("=" * 60)
        print(f"交易对: {self.symbol}")
        print(f"订单数量: {self.qty}")
        print(f"目标偏离: {self.target_bps} bps")
        print(f"最大偏离: {self.max_bps} bps")
        print(f"检查间隔: {check_interval}秒")
        print(f"运行时长: {duration}秒" if duration else "运行时长: 无限")
        print("=" * 60)
        
        # 初始化：下双向订单
        market_price = self.get_current_price()
        print(f"\n📊 当前市价: {market_price:.2f}")
        self.place_orders(market_price)
        
        # 监控循环
        start_time = time.time()
        iteration = 0
        
        try:
            while True:
                iteration += 1
                elapsed = time.time() - start_time
                
                if duration and elapsed > duration:
                    print(f"\n⏰ 运行时长达到 {duration}秒，停止策略")
                    break
                
                # 等待检查间隔
                time.sleep(check_interval)
                
                # 获取当前价格（容错处理）
                try:
                    market_price = self.get_current_price()
                except Exception as e:
                    print(f"  ⚠️ 跳过本次迭代，继续监控...")
                    continue
                
                print(f"\n[迭代 #{iteration}] 市价: {market_price:.2f} (运行时间: {int(elapsed)}秒)")
                
                # 显示当前订单状态
                self.refresh_orders()
                if self.buy_order:
                    buy_price = float(self.buy_order["price"])
                    buy_bps = abs((market_price - buy_price) / market_price * 10000)
                    print(f"  📗 买单: {buy_price:.2f} (偏离: {buy_bps:.1f} bps)")
                else:
                    print(f"  ⚠️ 无买单")
                
                if self.sell_order:
                    sell_price = float(self.sell_order["price"])
                    sell_bps = abs((sell_price - market_price) / market_price * 10000)
                    print(f"  📕 卖单: {sell_price:.2f} (偏离: {sell_bps:.1f} bps)")
                else:
                    print(f"  ⚠️ 无卖单")
                
                # 检查并调整订单（容错处理）
                try:
                    self.check_and_adjust_orders(market_price)
                except Exception as e:
                    print(f"  ⚠️ 调整订单失败: {e}，下次迭代重试...")
                    continue
                
        except KeyboardInterrupt:
            print(f"\n\n⚠️ 收到中断信号，停止策略...")
        except Exception as e:
            print(f"\n\n❌ 策略运行出现严重错误: {e}")
            print(f"   正在清理订单并退出...")
        
        # 清理：取消所有订单
        print(f"\n🧹 清理所有订单...")
        self.cleanup()
        
        print(f"\n" + "=" * 60)
        print("策略已停止")
        print("=" * 60)
    
    def cleanup(self):
        """清理所有订单"""
        self.refresh_orders()
        orders_to_cancel = []
        
        if self.buy_order:
            orders_to_cancel.append(self.buy_order)
        if self.sell_order:
            orders_to_cancel.append(self.sell_order)
        
        for order in orders_to_cancel:
            try:
                cancel_resp = self.auth.cancel_order(order_id=order["id"])
                print(f"  ✅ 取消 {order['side']} 订单: {order['cl_ord_id']}")
            except Exception as e:
                print(f"  ❌ 取消失败: {e}")


def main():
    """主函数"""
    
    # 加载配置
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    symbol = os.getenv("LIMIT_ORDER_SYMBOL", "BTC-USD")
    qty = os.getenv("LIMIT_ORDER_QTY", "0.004")
    target_bps = float(os.getenv("LIMIT_ORDER_BPS", "7.5"))
    max_bps = float(os.getenv("MAX_ORDER_BPS", "10"))
    
    # 认证
    print("🔐 认证中...")
    auth = StandXAuth(private_key)
    auth.authenticate()
    print("✅ 认证成功\n")
    
    # 创建做市器
    market_maker = MarketMaker(
        auth=auth,
        symbol=symbol,
        qty=qty,
        target_bps=target_bps,
        max_bps=max_bps,
    )
    
    # 运行策略（10分钟测试）
    market_maker.run(check_interval=5, duration=600)


if __name__ == "__main__":
    main()
