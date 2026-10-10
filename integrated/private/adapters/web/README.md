# Future web adapter

This directory is intentionally empty in v1. A future HTTP/WebSocket adapter should call `core.agent.Agent.handle_message(user_id, text)` and render the returned `AgentResponse` without moving business logic out of `core/`.
