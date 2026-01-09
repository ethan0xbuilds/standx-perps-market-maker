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
from base58 import b58encode
from nacl.signing import SigningKey

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
                        print(f"  ⚠️ 网络错误 (尝试 {attempt + 1}/{max_retries}): {type(e).__name__}，{delay}秒后重试...")
                        time.sleep(delay)
                    else:
                        print(f"  ❌ 重试{max_retries}次后仍失败")
                except Exception as e:
                    # 非网络错误直接抛出
                    raise
            raise last_exception
        return wrapper
    return decorator


class StandXAuth:
    """Handle StandX authentication flow for BSC"""
    
    def __init__(self, private_key: str):
        """
        Initialize with wallet private key
        
        Args:
            private_key: Wallet private key with 0x prefix (e.g., 0x123...)
        """
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.wallet_address = self.account.address
        self.token = None
        # Generate ephemeral ed25519 keypair; public key (base58) is used as requestId
        self.ed25519_signing_key = SigningKey.generate()
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
        print(f"[1/4] Calling prepare-signin endpoint...")
        
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
            
            print(f"✓ Received signedData JWT")
            return {"signedData": signed_data}
            
        except requests.exceptions.RequestException as e:
            detail = ""
            if hasattr(e, "response") and e.response is not None:
                detail = f" status={e.response.status_code} body={e.response.text}"
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
        print(f"[2/4] Extracting message from JWT...")
        
        try:
            # Decode without verification (as we don't have StandX's public key here)
            # In production, verify with: jwt.decode(signed_data, public_key, algorithms=["ES256"])
            decoded = jwt.decode(signed_data, options={"verify_signature": False})
            
            message = decoded.get("message")
            if not message:
                raise Exception("No message field in JWT payload")
            
            print(f"✓ Extracted message: {message[:50]}...")
            return message
            
        except jwt.DecodeError as e:
            raise Exception(f"JWT decode error: {str(e)}")
    
    def _sign_message(self, message: str) -> str:
        """
        Step 3: Sign the message with wallet private key using Ethereum signing
        
        Args:
            message: Message to sign
            
        Returns:
            Signature hex string
        """
        print(f"[3/4] Signing message with wallet...")
        
        try:
            # Create message hash using Ethereum standard (EIP-191)
            message_hash = encode_defunct(text=message)
            
            # Sign with account
            signed_message = self.account.sign_message(message_hash)
            
            signature = signed_message.signature.hex()
            print(f"✓ Generated signature: {signature[:20]}...")
            
            return signature
            
        except Exception as e:
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
        print(f"[4/4] Calling login endpoint...")
        
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
            
            print(f"✓ Successfully authenticated!")
            print(f"  - Address: {data.get('address')}")
            print(f"  - Alias: {data.get('alias', 'N/A')}")
            print(f"  - Chain: {data.get('chain')}")
            print(f"  - Perps Alpha: {data.get('perpsAlpha')}")
            
            return data
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"HTTP error in login: {str(e)}")
    
    def authenticate(self) -> dict:
        """
        Execute full authentication flow
        
        Returns:
            Dictionary with authentication response including access token
        """
        print(f"\n{'='*60}")
        print(f"StandX Authentication Flow (BSC)")
        print(f"Wallet: {self.wallet_address}")
        print(f"requestId (base58 ed25519 pubkey): {self.request_id}")
        print(f"{'='*60}\n")
        
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
            
            print(f"\n{'='*60}")
            print(f"✓ Authentication Successful!")
            print(f"Access Token: {self.token[:50]}...")
            print(f"{'='*60}\n")
            
            return auth_response
            
        except Exception as e:
            print(f"\n❌ Authentication failed: {str(e)}\n")
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
            raise Exception(f"API call failed: {str(e)}{detail}")
        except requests.exceptions.RequestException as e:
            status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            body = getattr(e.response, "text", None) if hasattr(e, "response") and e.response is not None else None
            detail = f" status={status} url={url} body={body}" if status else f" url={url}"
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
    
    if not private_key:
        raise ValueError(
            "WALLET_PRIVATE_KEY not found in .env file. "
            "Please set your wallet private key."
        )
    
    # Initialize authentication
    auth = StandXAuth(private_key)
    
    # Authenticate and get token
    auth_response = auth.authenticate()
    
    # Print auth response (optional)
    print("\nFull Authentication Response:")
    print(json.dumps(auth_response, indent=2))

    # Public sanity check: query symbol price
    symbol = os.getenv("LIMIT_ORDER_SYMBOL", "BTC-USD")
    price = auth.query_symbol_price(symbol)
    print(f"\nPublic Price ({symbol}):")
    print(json.dumps(price, indent=2))

    # Query and print user balance (graceful on empty account)
    try:
        balance = auth.query_balance()
        print("\nUser Balance:")
        print(json.dumps(balance, indent=2))
    except Exception as e:
        print(f"\n❌ 查询余额失败: {e}")

    # Query and print user positions
    try:
        positions = auth.query_positions(symbol=symbol)
        print("\nUser Positions:")
        if positions:
            print(json.dumps(positions, indent=2))
            # Extract leverage and margin_mode from position for order placement
            position = positions[0] if positions else None
            current_leverage = int(position["leverage"]) if position else None
            current_margin_mode = position["margin_mode"] if position else None
        else:
            print("  无持仓")
            current_leverage = None
            current_margin_mode = None
    except Exception as e:
        print(f"\n❌ 查询持仓失败: {e}")
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
        print(f"\nPlaced {side} limit order @ {limit_price_str} ({bps} bps adj)")
        print(json.dumps(order_resp, indent=2))
    except Exception as e:
        print(f"\n❌ 下单失败: {e}")
        order_request_id = None

    # Query order status using client order ID (request_id)
    if 'order_request_id' in locals() and order_request_id:
        print(f"\n⏳ Waiting 5s for order to be recorded in backend...")
        time.sleep(5)
        
        # Try query_open_orders with symbol
        print(f"\n📋 Querying open orders for {symbol}...")
        try:
            open_orders = auth.query_open_orders(symbol=symbol, limit=10)
            print(f"Open Orders ({symbol}):")
            if open_orders.get("result"):
                for ord in open_orders["result"]:
                    print(f"  ✅ {ord['cl_ord_id']}: {ord['status']} @ {ord['price']} qty={ord['qty']} side={ord['side']}")
            else:
                print(f"  (empty, page_size={open_orders.get('page_size')})")
        except Exception as e:
            print(f"  ❌ Error: {e}")
        
        # Try query_orders with symbol filter
        print(f"\n📜 Querying all orders with symbol={symbol}...")
        try:
            all_orders = auth.query_orders(symbol=symbol, limit=50)
            print(f"Orders ({symbol}, recent 50):")
            if all_orders.get("result"):
                for ord in all_orders["result"][:10]:  # Show first 10
                    print(f"  - {ord['cl_ord_id']}: {ord['status']} @ {ord['price']} qty={ord['qty']} side={ord['side']}")
            else:
                print(f"  (empty, page_size={all_orders.get('page_size')})")
        except Exception as e:
            print(f"  ❌ Error: {e}")


if __name__ == "__main__":
    main()
