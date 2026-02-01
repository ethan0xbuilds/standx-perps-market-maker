"""
Telegram 通知模块
支持时间限流，防止高频事件刷屏
"""

# 标准库导入
import os
import time
from typing import Optional

# 第三方库导入
import requests


class Notifier:
    """Telegram 通知器（带限流）"""
    
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.bot_token and self.chat_id)
        
        # 限流状态（用于订单重挂等高频事件）
        self._throttle_state = {}
    
    async def send(self, text: str, throttle_key: Optional[str] = None, throttle_seconds: int = 0):
        """
        发送 Telegram 消息
        
        Args:
            text: 消息内容
            throttle_key: 限流键（如 "reorder"），相同键在限流窗口内只发一次
            throttle_seconds: 限流窗口时长（秒），0 表示不限流
        
        Returns:
            是否成功发送
        """
        if not self.enabled:
            return False
        
        # 限流检查
        if throttle_key and throttle_seconds > 0:
            now = time.time()
            last_time = self._throttle_state.get(throttle_key, 0)
            
            if now - last_time < throttle_seconds:
                return False  # 在限流窗口内，跳过
            
            self._throttle_state[throttle_key] = now
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            response = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception:
            # 静默失败，避免影响主流程
            return False
    
    @staticmethod
    def from_env() -> "Notifier":
        """从环境变量创建通知器"""
        return Notifier()
