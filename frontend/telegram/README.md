# Perps Agent — Telegram bot (FE)

aiogram bot = the product UI. Deploy / monitor / stop grids by chat.

**Hard rule:** never import the engine, adapters, or any exchange/chain SDK. Talk
to the backend only through the typed `GridService` facade (+ HTTP gateway / SSE).
See repo `CLAUDE.md`. TODO: scaffold aiogram app + allowlist middleware.
