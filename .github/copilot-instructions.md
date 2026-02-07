# StandX Perps Market Maker - AI Coding Agent Guide

## Architecture Overview

This is an asyncio-based **dual-side limit order market maker** for StandX Perps (BSC). The bot maintains buy/sell orders around a target spread (default 7.5 bps), automatically replacing orders when price deviation exceeds [min_bps, max_bps] thresholds. Market data and state are sourced from WebSocket streams; order placement/cancel uses the WS order API.

**Core Components:**
- **standx_auth.py** (~685 lines): Wallet-based authentication using EIP-191 + Ed25519 signatures. Supports two schemes: (1) wallet-only auto-generates Ed25519, (2) token-based with pre-provided Ed25519 key + access token.
- **standx_api.py** (214 lines): HTTP API method library (querying + signed order endpoints). All methods take `StandXAuth` as first argument.
- **market_maker.py** (428 lines): Main strategy loop - checks positions → checks price deviation → replaces orders if needed.
- **adapter/standx_adapter.py** (494 lines): WebSocket adapter caching depth_book, orders, and positions; wraps WS order actions and confirmation waits.
- **api/ws_client.py** (318 lines): WebSocket clients for market data (`StandXMarketStream`) and order API (`StandXOrderStream`).
- **notifier.py** (70 lines): Telegram notifier with optional throttling.
- **logger.py** (64 lines): Rotating file logger (logs/market_maker.log, 10MB × 5 backups).

**Data Flow:**
1. WebSocket depth_book pushes → adapter caches mid_price (with update flag)
2. WebSocket order/position pushes → adapter caches orders/positions
3. Main loop reads cached mid_price + orders → calculates bps deviation → decides to replace orders
4. Orders placed/canceled via WS order API (`StandXOrderStream`) → order pushes update cache

## Critical Workflows

### Running the Bot
```bash
# Production (systemd service)
sudo systemctl start standx-market-maker
sudo journalctl -u standx-market-maker -f

# Development
source .venv/bin/activate
python market_maker.py

# Stop gracefully (sends SIGTERM → cleanup orders)
sudo systemctl stop standx-market-maker
```

### Authentication Schemes
**Scheme 1 (Wallet-based):** Set only `WALLET_PRIVATE_KEY` → auto-generates Ed25519 keypair on first auth.  
**Scheme 2 (Token-based):** Set `ED25519_PRIVATE_KEY` + `ACCESS_TOKEN`, leave `WALLET_PRIVATE_KEY` empty.

WS order/position streams require `ACCESS_TOKEN` in the environment. In practice, use scheme 2 for WS-heavy runtime or ensure `ACCESS_TOKEN` is populated.

Both schemes checked in `market_maker.py:main()` with explicit error messages. Auth happens in `standx_auth.py:authenticate()`.

## Project-Specific Conventions

### Error Handling & Retries
- **Network errors** (Timeout, ConnectionError, ProxyError) are auto-retried 3× with 1s delay via `@retry_on_network_error` decorator in standx_auth.py.
- **WS stream hiccups** during order/position updates → keep cached state, log warning, continue running (don't crash).
- **Position detected** → immediately close with market order, retry next iteration if fails.

### Async Patterns
- Use `asyncio.create_task()` for concurrent tasks (WebSocket listeners, maker loop).
- Never block event loop - callbacks in `ws_client.py` use `asyncio.create_task(self._handle_message(data))` to avoid blocking receives.
- All API calls in `standx_api.py` are `async` functions (use `await`).

### Logging & Debugging
- Use `logger.info()` for state changes (order placement, mode switches).
- Use `logger.debug()` for unchanged depth_book updates (reduces noise).
- Use `logger.exception()` when catching exceptions to include stack traces.
- Log format: `%(asctime)s %(levelname)s [%(name)s] %(message)s`

### Environment Variables
All config via `.env` (never commit - see .gitignore). Key vars:
- `WALLET_PRIVATE_KEY` / `ED25519_PRIVATE_KEY` + `ACCESS_TOKEN`
- `MARKET_MAKER_SYMBOL=BTC-USD`, `MARKET_MAKER_QTY=0.005`
- `MARKET_MAKER_TARGET_BPS=7.5` (initial spread), `MIN_BPS=7.0`, `MAX_BPS=10.0` (reorder thresholds)
- `MARKET_MAKER_CHECK_INTERVAL=0.0` (seconds between checks, 0 = no artificial delay)
- `ACCESS_TOKEN` (required for WS order/position streams)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional)

### Signature & Body Encoding
Private endpoints (new_order, cancel_order) require Ed25519 body signatures:
1. JSON payload serialized with `json.dumps(payload, separators=(",", ":"))` (no spaces)
2. Signature computed in `standx_auth.py:_body_signature_headers()` using Ed25519 private key
3. Headers: `X-Perps-Api-Signature`, `X-Perps-Api-Ts`, `X-Perps-Api-Recv-Window`, `X-Perps-Api-Request-Id`

WS order API also uses `_body_signature_headers()` and maps values to `x-request-id`, `x-request-timestamp`, `x-request-signature` in `StandXOrderStream`.

### Graceful Shutdown
`MarketMaker._setup_signal_handlers()` catches SIGTERM/SIGINT → sets `_shutdown_requested=True` → main loop breaks → `cleanup()` cancels all orders → exit. Always use signal handlers, never just kill -9.

## Key Files Reference

- **Dual-side limit strategy logic:** [market_maker.py](../market_maker.py) - main monitoring loop
- **WS adapter (mid_price + order cache):** [adapter/standx_adapter.py](../adapter/standx_adapter.py)
- **API signature generation:** [standx_auth.py](../standx_auth.py) - Ed25519 body signing
- **Order placement (WS):** [api/ws_client.py](../api/ws_client.py) - StandXOrderStream
- **HTTP API helpers:** [standx_api.py](../standx_api.py) - new_limit_order/query methods

## Common Pitfalls

1. **Time sync issues:** Server time drift causes 403 "request expired" errors. Always `timedatectl set-ntp true` on deployment servers.
2. **Mixed auth schemes:** Don't set both WALLET_PRIVATE_KEY and ED25519_PRIVATE_KEY - code explicitly rejects this.
3. **Missing ACCESS_TOKEN:** WS order/position streams require `ACCESS_TOKEN` in env, otherwise subscription/login fails.
4. **Blocking callbacks:** WebSocket callbacks must be async or wrapped in `create_task()` - never do heavy sync work in `on_depth_book()`.
5. **Forgetting `await`:** All API calls in standx_api.py are async - forgetting `await` returns a coroutine object, not the result.
6. **Position violations:** If strategy tries to open a position that violates leverage limits, orders will fail. Strategy auto-closes any detected positions to stay flat.

## Testing & Validation

- **Notification test:** `tests/test_notification.py` - validates Telegram config and sends test message.
- **Live WS test:** `tests/test_ws_trading.py` - places real orders on exchange (BE CAREFUL).
- No unit test framework - tests are standalone scripts that need manual review.

## Deployment Notes

- Systemd service file: `standx-market-maker.service` with resource limits (CPUQuota=35%, MemoryMax=800M, TasksMax=50).
- Logs: Both systemd journal (`journalctl -u standx-market-maker -f`) and file rotation (`logs/market_maker.log*`).