# StandX Perps — Copilot instructions

## 通信语言（Communication Language）
**所有与本项目的交互请使用中文（中文）回复。** Please respond in Chinese for all interactions with this project.

## Purpose（目的）
- Help contributors make safe, small, reviewable changes to this Python market-maker project.
- Primary focus: trading safety, authentication, price feeds, notifications.

## How to help（如何协助）
- Propose minimal, focused diffs. Include imports, type hints, and a one-line rationale.
- For non-trivial changes, include or update a unit test and list required `pip` deps to add to `requirements.txt`.
- When suggesting API/network code, always include timeout, retry/backoff, and error handling.

## Safety & secrets（安全与隐私）
- Never hardcode private keys, tokens, or secrets. Use `os.getenv()` or secret managers. （中文：严禁在代码或示例中写入任何私钥/令牌）
- Flag any code that may log secrets; recommend redaction.

## Reliability & correctness（可靠性与正确性）
- Prioritize idempotency, retries with backoff, and explicit timeouts for HTTP/WebSocket calls.
- Prefer explicit concurrency models consistent with existing code (threads vs asyncio) — ask before switching.

## Performance & observability（性能与可观测性）
- Recommend structured logs and light metrics for critical flows (auth, order placement, reconciliation).
- For trading logic, verify edge cases: stale price feed, partial fills, negative/zero balances.

## Style & maintainability（代码风格与可维护性）
- Follow idiomatic Python: type hints, docstrings, short functions, clear names.
- Keep changes documented and update `README.md` when behavior or configuration changes.

## File-specific priorities（文件优先级）
- `market_maker.py`: preserve safety checks; suggest adding a simulation/safe mode for tests. （中文：改动策略逻辑前必须保证可回滚）
- `standx_auth.py` & `standx_api.py`: validate token flows, refresh, and signature logic.
- `price_providers.py`: ensure feed freshness and fallback ordering (mark > mid > last).
- `notifier.py`: non-blocking sends, throttling correctness, silent fail-safe.

## Examples for Copilot suggestions（Copilot 建议示例）
- Provide code + test + short commit message suggestion.
- When proposing new env vars, include `.env.example` snippet and README note.

## Rationale（理由）
This file balances model compatibility (English instructions) with Chinese safety highlights for maintainers. 目的：让 Copilot 给出可审查、测试过且以安全为先的建议。
