---
paths: ["boring_agent/**/*.py"]
---

# Implementation order

Implement and test changes in this order: task validation and state model; Store transaction; manager transition; worker or runner observation; artifact or provider boundary; CLI or API adapter; documentation and examples. Do not proceed to a dependent layer until focused tests for its lower-layer dependency pass. Do not patch a high-level adapter to compensate for a missing lower-layer invariant.
