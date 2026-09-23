@R2 @R3 @R5 @R6 @R7 @R8 @ch01 @ch03 @ch04 @ch06 @ch07 @ch09 @ch11
Feature: Run ACP agents under an enforceable bounded contract
  The ACP adapter must distinguish a cooperative request from confirmed process cessation and must not claim isolation or budget enforcement it cannot provide.
  Source: PLAN.md, "Milestone 4 — ACP runtime" and the contracts in docs/isolation.md, docs/cancellation.md and docs/budgets.md.

  Background:
    Given an isolated local agent with an ACP runtime route

  Scenario: Tier A isolation binds the workspace and hides store state
    Given bubblewrap capability is available
    And an ACP task has sandbox "read-only"
    When the worker launches the ACP agent
    Then the workspace is bound read-only in the agent sandbox
    And the agent has private temporary and state directories
    And the store home is inaccessible
    And network access is disabled unless the task explicitly allows it

  Scenario: Workspace-write is the only Tier A mode that grants a writable workspace
    Given bubblewrap capability is available
    And an ACP task has sandbox "workspace-write" with all required write grants
    When the worker launches the ACP agent
    Then the workspace bind is writable
    And store-managed paths remain inaccessible

  Scenario Outline: A runtime without enforceable isolation fails closed for protected work
    Given isolation capability is <capability>
    And an ACP task has sandbox "read-only"
    When the worker evaluates the route
    Then the task is refused before launch
    And the refusal explains the weakened isolation contract

    Examples:
      | capability |
      | unavailable|
      | unknown    |

  Scenario: An ACP agent cannot bypass its own permission system or load local extensions
    Given an ACP agent route supports permission modes, hooks, MCP servers, subagents and skills
    When the worker launches the route
    Then bypass permission mode is refused
    And project-local hooks, MCP servers and subagents are disabled
    And automatic skill discovery is disabled
    And the agent receives an isolated home

  Scenario: Cancellation is not reported until process and adapter evidence agree
    Given an ACP agent is executing work in its own process group
    When the operator requests cancellation
    Then the adapter receives a cooperative cancellation notification
    And the task is not yet reported as "Cancelled"
    When the process group is gone and the adapter reports a terminal stop reason
    Then the task status is "Cancelled"

  Scenario Outline: Incomplete cessation evidence remains unknown and retains capacity
    Given cancellation has been requested for an ACP attempt
    And <missing_evidence> is missing
    When the cancellation grace and termination sequence finish
    Then the observation condition is "Unknown"
    And 1 shared slot is reserved

    Examples:
      | missing_evidence          |
      | process group cessation   |
      | terminal adapter stop reason |

  Scenario: A silent ACP agent is bounded by a timer-driven supervisor
    Given an ACP agent sends no callback or terminal message
    When the supervisor timeout elapses
    Then it sends SIGTERM to the process group
    And after the grace period it sends SIGKILL when required
    And the resulting observation follows the confirmed-cessation rule

  Scenario: An ACP task refuses budgets the adapter cannot enforce without opt-in
    Given an ACP adapter cannot enforce per-call limits or internal step counts
    And a task requires those budgets without a weaker-contract opt-in
    When the manager evaluates the ACP route
    Then the task is refused before launch
    And the refusal names each unenforceable budget

  Scenario: ACP accounting records approximate token totals without double counting
    Given an ACP adapter reports cumulative token usage after each opaque turn
    When the agent completes two turns
    Then the task records the latest cumulative total as a maximum
    And it does not sum cumulative totals as separate usage

  Scenario: Absolute filesystem callbacks are mapped and validated as workspace paths
    Given an ACP agent requests a filesystem callback using an absolute path inside the workspace
    When the adapter handles the callback
    Then it maps the path to a workspace-relative path before validation
    Given an ACP agent requests an absolute path outside the workspace
    When the adapter handles the callback
    Then the callback is rejected with "invalid_request"

  Scenario: ACP plan notifications are progress, not workflow specifications
    Given an ACP agent sends a plan notification containing child-like tasks
    When the adapter records the notification
    Then it is retained only as progress evidence
    And no workflow child is created

  Scenario: ACP modes and models are negotiated instead of assumed
    Given an ACP route advertises a set of modes and models
    When a task requests one compatible mode and model
    Then the negotiated route is recorded on the attempt
    When a task requests an unadvertised mode or model
    Then the task is refused before launch
