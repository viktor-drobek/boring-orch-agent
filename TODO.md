# Project TODOs

## Agent runtime tips

- **Context limit recovery**: If an agent uses Coddy and receives an upstream API
  context-limit error, the agent can send `/compact` to trigger context compaction
  and continue the session without losing the conversation thread.
