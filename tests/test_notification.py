#!/usr/bin/env python
"""
Telegram 通知测试脚本
运行前请确保 .env 中已配置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID
"""

import os
import sys

# 添加父目录到路径，以便导入 notifier 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from notifier import Notifier

def main():
    load_dotenv()
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    print("🔍 检查配置...")
    print(f"   TELEGRAM_BOT_TOKEN: {'✓ 已设置' if bot_token else '✗ 未设置'}")
    print(f"   TELEGRAM_CHAT_ID: {'✓ 已设置' if chat_id else '✗ 未设置'}")
    
    if not (bot_token and chat_id):
        print("\n⚠️  请先在 .env 中配置 Telegram 通知参数")
        print("   参考 README.md 中的 'Telegram 通知配置' 章节")
        return
    
    print("\n📤 发送测试消息...")
    notifier = Notifier.from_env()
    
    test_message = (
        "✅ *StandX 通知测试*\n\n"
        "如果你看到这条消息，说明通知配置成功！\n\n"
        "支持的通知事件：\n"
        "• 策略启动/停止\n"
        "• 模式切换\n"
        "• 持仓平仓\n"
        "• 订单重挂（5分钟限流）\n"
        "• 致命异常"
    )
    
    result = notifier.send(test_message)
    
    if result:
        print("✅ 发送成功！请检查 Telegram 查看消息")
    else:
        print("❌ 发送失败，请检查配置或网络连接")
        print("   常见问题：")
        print("   1. Bot Token 或 Chat ID 错误")
        print("   2. 需要先与 Bot 发起对话（发送 /start）")
        print("   3. 网络无法访问 Telegram API（需要代理）")

if __name__ == "__main__":
    main()
