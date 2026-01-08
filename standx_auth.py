"""
StandX Perps API Authentication Module (BSC Chain)
Implements wallet-based signature authentication with Ed25519 and Ethereum signing
"""

import os
import json
import requests
import jwt
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
                timeout=10
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
                timeout=10
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
    
    def make_api_call(self, endpoint: str, method: str = "GET", data: dict = None, params: dict = None) -> dict:
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
        
        try:
            method_up = method.upper()
            if method_up == "GET":
                response = requests.get(url, headers=headers, params=params, timeout=10)
            elif method_up == "POST":
                response = requests.post(url, json=data, headers=headers, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            status = getattr(e.response, "status_code", None) if hasattr(e, "response") else None
            body = getattr(e.response, "text", None) if hasattr(e, "response") and e.response is not None else None
            detail = f" status={status} url={url} body={body}" if status else f" url={url}"
            raise Exception(f"API call failed: {str(e)}{detail}")

    def query_balance(self) -> dict:
        """Query unified user balance snapshot"""
        try:
            return self.make_api_call("/api/query_balance")
        except Exception as e:
            msg = str(e)
            if "status=404" in msg and "user balance not found" in msg:
                print("\n⚠️ 未找到账户余额记录，返回零值快照（可能尚未入金/转入保证金）。")
                return {
                    "isolated_balance": "0",
                    "isolated_upnl": "0",
                    "cross_balance": "0",
                    "cross_margin": "0",
                    "cross_upnl": "0",
                    "locked": "0",
                    "cross_available": "0",
                    "balance": "0",
                    "upnl": "0",
                    "equity": "0",
                    "pnl_freeze": "0",
                }
            raise

    def query_symbol_price(self, symbol: str) -> dict:
        """Public: Query symbol price snapshot (index/mark/last/mid)"""
        return self.make_api_call("/api/query_symbol_price", params={"symbol": symbol})


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
    price = auth.query_symbol_price("BTC-USD")
    print("\nPublic Price (BTC-USD):")
    print(json.dumps(price, indent=2))

    # Query and print user balance (graceful on empty account)
    try:
        balance = auth.query_balance()
        print("\nUser Balance:")
        print(json.dumps(balance, indent=2))
    except Exception as e:
        print(f"\n❌ 查询余额失败: {e}")


if __name__ == "__main__":
    main()
