# Telegram bot (FE)

aiogram bot — the product UI: deploy, monitor, and stop grids by chat.

Rule: never import the engine, adapters, or any exchange/chain SDK. Talk to the
backend through the typed `GridService` facade only (over an HTTP gateway / SSE).

TODO: scaffold aiogram app + allowlist middleware.
