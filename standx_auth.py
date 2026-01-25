"""
StandX Perps API Authentication Module (BSC Chain)
Implements wallet-based signature authentication with Ed25519 and Ethereum signing
"""

import os
import json
import base64
import time
import uuid
import requests
import jwt
from functools import wraps
from dotenv import load_dotenv
from eth_account import Account
from eth_account.messages import encode_defunct
from base58 import b58encode, b58decode
from nacl.signing import SigningKey
from nacl.utils import random
from logger import get_logger

logger = get_logger(__name__)

# API endpoints
PREPARE_SIGNIN_URL = "https://api.standx.com/v1/offchain/prepare-signin"
LOGIN_URL = "https://api.standx.com/v1/offchain/login"
PERPS_BASE_URL = "https://perps.standx.com"
CHAIN = "bsc"

# Load environment variables
load_dotenv()

# Network configuration
DEFAULT_TIMEOUT = 30  # 增加超时时间到30秒
MAX_RETRIES = 3  # 最大重试次数
RETRY_DELAY = 1  # 重试延迟（秒）- 优化为1秒


def retry_on_network_error(max_retries=MAX_RETRIES, delay=RETRY_DELAY):
    """网络错误重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.Timeout, 
                        requests.exceptions.ConnectionError,
                        requests.exceptions.ProxyError) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning("网络错误 (尝试 %d/%d): %s，%s秒后重试...", attempt + 1, max_retries, type(e).__name__, delay)
                        time.sleep(delay)
                    else:
                        logger.error("重试%d次后仍失败", max_retries)
                except Exception as e:
                    # 非网络错误直接抛出
                    raise
            raise last_exception
        return wrapper
    return decorator


class StandXAuth:
    """Handle StandX authentication flow for BSC"""
    
    def __init__(self, private_key: str, ed25519_key: str = None, token: str = None):
        """
        Initialize with authentication parameters (two schemes supported).
        
        Scheme 1 (Wallet-based): Only private_key is provided
            - private_key: Ethereum wallet private key with 0x prefix
            - ed25519_key: None or empty (will be auto-generated)
            - token: None or empty (will be obtained via wallet signature)
        
        Scheme 2 (Token-based): private_key is None/empty, ed25519_key and token are provided
            - private_key: None or empty (wallet signing not used)
            - ed25519_key: Ed25519 private key from StandX (44-char base58 string)
            - token: Pre-obtained access token from StandX
        
        Args:
            private_key: Ethereum wallet private key (scheme 1) or None (scheme 2)
            ed25519_key: Ed25519 private key (scheme 2) or None (scheme 1)
            token: Access token (scheme 2) or None (scheme 1)
            
        Raises:
            ValueError: If parameters don't match either scheme
        """
        # Normalize None/empty to None
        private_key = private_key if private_key else None
        ed25519_key = ed25519_key if ed25519_key else None
        token = token if token else None
        
        # Validate parameter combinations
        if private_key and ed25519_key and token:
            raise ValueError(
                "❌ 参数配置错误：检测到同时设置了WALLET_PRIVATE_KEY和(ED25519_PRIVATE_KEY + ACCESS_TOKEN)\n"
                "   请选择其中一种方案：\n"
                "   方案1: 仅设置WALLET_PRIVATE_KEY（系统自动生成ED25519密钥）\n"
                "   方案2: 仅设置ED25519_PRIVATE_KEY + ACCESS_TOKEN（WALLET_PRIVATE_KEY应为空）"
            )
        
        # Scheme 1: Wallet-based authentication
        if private_key and not ed25519_key and not token:
            logger.info("方案1: 基于钱包签名的完整认证")
            self.private_key = private_key
            self.account = Account.from_key(private_key)
            self.wallet_address = self.account.address
            self.token = None
            
            # Auto-generate Ed25519 keypair
            ed25519_key = self._generate_ed25519_keypair()
            logger.info("已自动生成ED25519密钥对")
            self._load_ed25519_key(ed25519_key)
            
        # Scheme 2: Token-based authentication
        elif not private_key and ed25519_key and token:
            logger.info("方案2: 基于预配置令牌的快速认证")
            self.private_key = None
            self.account = None
            self.wallet_address = None
            self.token = token
            
            # Load provided Ed25519 key
            self._load_ed25519_key(ed25519_key)
            
        else:
            # Invalid combination
            raise ValueError(
                "❌ 参数配置不完整或不符合任何方案\n"
                "   方案1: 需要提供WALLET_PRIVATE_KEY（其他参数为空）\n"
                "   方案2: 需要同时提供ED25519_PRIVATE_KEY和ACCESS_TOKEN（WALLET_PRIVATE_KEY为空）\n"
                f"   当前配置: WALLET_PRIVATE_KEY={'✓' if private_key else '✗'}, "
                f"ED25519_PRIVATE_KEY={'✓' if ed25519_key else '✗'}, "
                f"ACCESS_TOKEN={'✓' if token else '✗'}"
            )
    
    @staticmethod
    def _generate_ed25519_keypair() -> str:
        """
        Generate a new Ed25519 keypair and return the private key.
        
        Returns:
            Ed25519 private key as 44-character base58 encoded string
        """
        # Generate 32-byte random seed
        seed_bytes = random(32)
        # Encode as base58
        ed25519_key_b58 = b58encode(seed_bytes).decode()
        return ed25519_key_b58
    
    def _load_ed25519_key(self, ed25519_key: str):
        """
        Load Ed25519 signing key from base58 string.
        
        Args:
            ed25519_key: Ed25519 private key (44-char base58 string)
            
        Raises:
            ValueError: If key format is invalid
        """
        try:
            seed_bytes = b58decode(ed25519_key)
            if len(seed_bytes) != 32:
                raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(seed_bytes)}")
            self.ed25519_signing_key = SigningKey(seed_bytes)
        except Exception as e:
            raise ValueError(f"ED25519_PRIVATE_KEY 格式错误，必须是 44 字符的 base58 编码字符串: {e}")
        
        self.request_id = b58encode(self.ed25519_signing_key.verify_key.encode()).decode()
        
    @retry_on_network_error()
    def _get_prepare_signin_data(self) -> dict:
        """
        Step 1: Call prepare-signin to get signature data (signedData JWT)
        
        Returns:
            Dictionary with signedData JWT
            
        Raises:
            Exception: If API call fails
        """
        logger.info("Calling prepare-signin endpoint... [1/4]")
        
        params = {"chain": CHAIN}
        payload = {
            "address": self.wallet_address,
            "requestId": self.request_id
        }
        headers = {"Content-Type": "application/json"}
        
        try:
            response = requests.post(
                PREPARE_SIGNIN_URL,
                params=params,
                json=payload,
                headers=headers,
                timeout=DEFAULT_TIMEOUT
            )
            response.raise_for_status()
            
            data = response.json()
            
            if not data.get("success"):
                raise Exception(f"prepare-signin failed: {data}")
            
            signed_data = data.get("signedData")
            if not signed_data:
                raise Exception("No signedData in response")
            
            logger.info("Received signedData JWT")
            return {"signedData": signed_data}
            
        except requests.exceptions.RequestException as e:
            detail = ""
            if hasattr(e, "response") and e.response is not None:
                detail = f" status={e.response.status_code}"
            logger.exception("HTTP error in prepare-signin: %s %s", str(e), detail)
            raise Exception(f"HTTP error in prepare-signin: {str(e)}{detail}")
    
    def _extract_message_from_jwt(self, signed_data: str) -> str:
        """
        Step 2: Extract message from JWT without verification (for demonstration)
        
        Note: In production, you should verify the JWT signature using StandX's public key
        
        Args:
            signed_data: JWT token from prepare-signin
            
        Returns:
            Message string to be signed
        """
        logger.info("Extracting message from JWT... [2/4]")
        
        try:
            # Decode without verification (as we don't have StandX's public key here)
            # In production, verify with: jwt.decode(signed_data, public_key, algorithms=["ES256"])
            decoded = jwt.decode(signed_data, options={"verify_signature": False})
            
            message = decoded.get("message")
            if not message:
                raise Exception("No message field in JWT payload")
            
            logger.debug("Extracted message (truncated): %s...", message[:50])
            return message
            
        except jwt.DecodeError as e:
            logger.exception("JWT decode error: %s", str(e))
            raise Exception(f"JWT decode error: {str(e)}")
    
    def _sign_message(self, message: str) -> str:
        """
        Step 3: Sign the message with wallet private key using Ethereum signing
        
        Args:
            message: Message to sign
            
        Returns:
            Signature hex string
        """
        logger.info("Signing message with wallet... [3/4]")
        
        try:
            # Create message hash using Ethereum standard (EIP-191)
            message_hash = encode_defunct(text=message)
            
            # Sign with account
            signed_message = self.account.sign_message(message_hash)
            
            signature = signed_message.signature.hex()
            logger.debug("Generated signature (masked)")
            
            return signature
            
        except Exception as e:
            logger.exception("Signing error: %s", str(e))
            raise Exception(f"Signing error: {str(e)}")
    
    @retry_on_network_error()
    def _get_access_token(self, signature: str, signed_data: str) -> dict:
        """
        Step 4: Call login endpoint with signature to get access token
        
        Args:
            signature: Signed message hex string
            signed_data: JWT from prepare-signin
            
        Returns:
            Dictionary with access token and user info
            
        Raises:
            Exception: If login fails
        """
        logger.info("Calling login endpoint... [4/4]")
        
        payload = {
            "signature": signature,
            "signedData": signed_data,
            "expiresSeconds": 604800  # 7 days
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post(
                LOGIN_URL,
                params={"chain": CHAIN},
                json=payload,
                headers=headers,
                timeout=DEFAULT_TIMEOUT
            )
            response.raise_for_status()
            
            data = response.json()
            
            if "token" not in data:
                raise Exception(f"Login failed: {data}")

            self.token = data.get("token")
            logger.info("Access token received (redacted)")
            logger.info("Successfully authenticated")
            logger.debug("Auth response meta: address=%s alias=%s chain=%s perpsAlpha=%s", data.get('address'), data.get('alias', 'N/A'), data.get('chain'), data.get('perpsAlpha'))

            return data
            
        except requests.exceptions.RequestException as e:
            logger.exception("HTTP error in login: %s", str(e))
            raise Exception(f"HTTP error in login: {str(e)}")
    
    def authenticate(self) -> dict:
        """
        Execute full authentication flow or use pre-provided token
        
        Returns:
            Dictionary with authentication response including access token
        """
        # If token was provided at initialization, skip authentication
        if self.token:
            logger.info("StandX Authentication (BSC) using provided token")
            logger.debug("Wallet: %s", self.wallet_address)
            return {"token": self.token}
        
        logger.info("StandX Authentication Flow (BSC)")
        logger.debug("Wallet: %s", self.wallet_address)
        logger.debug("requestId (ed25519 pubkey): %s", self.request_id)
        
        try:
            # Step 1: Get signature data
            prepare_data = self._get_prepare_signin_data()
            signed_data = prepare_data["signedData"]
            
            # Step 2: Extract message from JWT
            message = self._extract_message_from_jwt(signed_data)
            
            # Step 3: Sign the message
            signature = self._sign_message(message)
            
            # Step 4: Get access token
            auth_response = self._get_access_token(signature, signed_data)
            
            logger.info("Authentication successful (access token redacted)")
            
            return auth_response
            
        except Exception as e:
            logger.exception("Authentication failed: %s", str(e))
            raise
    
    def get_token(self) -> str:
        """Get current access token for API calls"""
        return self.token
    
    @retry_on_network_error()
    def make_api_call(self, endpoint: str, method: str = "GET", data: dict = None, params: dict = None, headers_extra: dict = None, raw_body: str = None) -> dict:
        """
        Make authenticated API call to StandX
        
        Args:
            endpoint: API endpoint path (e.g., "/v1/user/profile")
            method: HTTP method (GET, POST, etc.)
            data: Request body for POST/PUT requests
            
        Returns:
            Response JSON
        """
        if not self.token:
            raise Exception("Not authenticated. Call authenticate() first.")

        # Normalize endpoint to avoid trailing-slash 404s
        normalized_endpoint = endpoint.rstrip("/") if endpoint else ""
        url = f"{PERPS_BASE_URL}{normalized_endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        if headers_extra:
            headers.update(headers_extra)
        
        try:
            method_up = method.upper()
            if method_up == "GET":
                response = requests.get(url, headers=headers, params=params, timeout=DEFAULT_TIMEOUT)
            elif method_up == "POST":
                if raw_body is not None:
                    response = requests.post(
                        url, data=raw_body, headers=headers, params=params, timeout=DEFAULT_TIMEOUT
                    )
                else:
                    response = requests.post(
                        url, json=data, headers=headers, params=params, timeout=DEFAULT_TIMEOUT
                    )
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text if e.response is not None else None
            # 特殊处理403签名过期错误
            if status == 403 and body and "signature has expired" in body:
                raise Exception(f"Body signature expired (403): {body} - 请检查系统时间是否同步")
            detail = f" status={status} url={url} body={body}" if status else f" url={url}"
            logger.exception("API call HTTPError: %s %s", str(e), detail)
            raise Exception(f"API call failed: {str(e)}{detail}")
        except requests.exceptions.RequestException as e:
            status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            body = getattr(e.response, "text", None) if hasattr(e, "response") and e.response is not None else None
            detail = f" status={status} url={url} body={body}" if status else f" url={url}"
            logger.exception("API call RequestException: %s %s", str(e), detail)
            raise Exception(f"API call failed: {str(e)}{detail}")

    def _body_signature_headers(self, payload_str: str) -> dict:
        """Build body signature headers (ed25519, base64)."""
        x_request_id = str(uuid.uuid4())
        x_request_timestamp = str(int(time.time() * 1000))  # milliseconds
        message = f"v1,{x_request_id},{x_request_timestamp},{payload_str}"
        signature_bytes = self.ed25519_signing_key.sign(message.encode("utf-8")).signature
        signature_b64 = base64.b64encode(signature_bytes).decode()
        return {
            "x-request-sign-version": "v1",
            "x-request-id": x_request_id,
            "x-request-timestamp": x_request_timestamp,
            "x-request-signature": signature_b64,
        }


def main():
    """Example usage of StandX authentication"""
    
    # Get private key from environment
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    ed25519_key = os.getenv("ED25519_PRIVATE_KEY")
    token = os.getenv("ACCESS_TOKEN")  # Optional pre-provided token

    if not private_key:
        raise ValueError(
            "WALLET_PRIVATE_KEY not found in .env file. "
            "Please set your wallet private key."
        )
    
    if not ed25519_key:
        raise ValueError(
            "ED25519_PRIVATE_KEY not found in .env file. "
            "Please set your Ed25519 private key from StandX platform."
        )
    
    # Initialize authentication
    auth = StandXAuth(private_key, ed25519_key, token=token)
    
    # Authenticate and get token
    auth_response = auth.authenticate()
    logger.debug("Authentication response: %s", auth_response)
    
    # Debug: auth response and public price (redacted in logs)
    logger.debug("Full Authentication Response: %s", json.dumps(auth_response, indent=2))

    # Public sanity check: query symbol price
    symbol = os.getenv("LIMIT_ORDER_SYMBOL", "BTC-USD")
    price = auth.query_symbol_price(symbol)
    logger.debug("Public Price (%s): %s", symbol, json.dumps(price, indent=2))

    # Query and log user balance (graceful on empty account)
    try:
        balance = auth.query_balance()
        logger.debug("User Balance: %s", json.dumps(balance, indent=2))
    except Exception as e:
        logger.warning("查询余额失败: %s", e)

    # Query and print user positions
    try:
        positions = auth.query_positions(symbol=symbol)
        logger.debug("User Positions: %s", json.dumps(positions, indent=2))
        if positions:
            position = positions[0] if positions else None
            current_leverage = int(position["leverage"]) if position else None
            current_margin_mode = position["margin_mode"] if position else None
        else:
            logger.debug("无持仓")
            current_leverage = None
            current_margin_mode = None
    except Exception as e:
        logger.warning("查询持仓失败: %s", e)
        current_leverage = None
        current_margin_mode = None

    # Place a demo limit order using env-configured bps/qty/side
    try:
        bps = int(os.getenv("LIMIT_ORDER_BPS", "50"))
        side = os.getenv("LIMIT_ORDER_SIDE", "buy").lower()
        qty = float(os.getenv("LIMIT_ORDER_QTY", "0.00001"))

        mid_price = price.get("mid_price")
        mark_price = price.get("mark_price")
        last_price = price.get("last_price")
        base_price = mid_price or mark_price or last_price
        if base_price is None:
            raise ValueError("No price fields found in symbol price snapshot.")

        base_price_f = float(base_price)
        if side not in {"buy", "sell"}:
            raise ValueError("LIMIT_ORDER_SIDE must be 'buy' or 'sell'")
        sign = -1 if side == "buy" else 1
        limit_price = base_price_f * (1 + sign * (bps / 10000))
        if limit_price <= 0:
            raise ValueError("Computed limit price is non-positive")
        limit_price_str = f"{limit_price:.2f}"
        qty_str = f"{qty:.4f}"

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
        order_request_id = order_resp.get("request_id")
        logger.info("Placed %s limit order @ %s (%s bps adj)", side, limit_price_str, bps)
        logger.debug("Order response: %s", json.dumps(order_resp, indent=2))
    except Exception as e:
        logger.exception("下单失败: %s", e)
        order_request_id = None

    # Query order status using client order ID (request_id)
    if 'order_request_id' in locals() and order_request_id:
        logger.info("Waiting 5s for order to be recorded in backend...")
        time.sleep(5)

        # Try query_open_orders with symbol
        logger.info("Querying open orders for %s...", symbol)
        try:
            open_orders = auth.query_open_orders(symbol=symbol, limit=10)
            logger.info("Open Orders (%s):", symbol)
            if open_orders.get("result"):
                for ord in open_orders["result"]:
                    logger.info("%s: %s @ %s qty=%s side=%s", ord['cl_ord_id'], ord['status'], ord['price'], ord['qty'], ord['side'])
            else:
                logger.info("(empty, page_size=%s)", open_orders.get('page_size'))
        except Exception as e:
            logger.warning("Query open orders error: %s", e)
        
        # Try query_orders with symbol filter
        logger.info("Querying all orders with symbol=%s...", symbol)
        try:
            all_orders = auth.query_orders(symbol=symbol, limit=50)
            logger.info("Orders (%s, recent 50):", symbol)
            if all_orders.get("result"):
                for ord in all_orders["result"][:10]:  # Show first 10
                    logger.info("- %s: %s @ %s qty=%s side=%s", ord['cl_ord_id'], ord['status'], ord['price'], ord['qty'], ord['side'])
            else:
                logger.info("(empty, page_size=%s)", all_orders.get('page_size'))
        except Exception as e:
            logger.warning("Query orders error: %s", e)


if __name__ == "__main__":
    main()
