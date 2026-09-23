@R3 @R6 @R7 @R8 @ch03 @ch06 @ch09 @ch10
Feature: Admit model work against the effective context window
  Context admission must be bound to the model that will execute an attempt and must never treat missing limits as unlimited capacity.
  Source: PLAN.md, "Milestone 1 — Effective model identity and context admission".

  Background:
    Given an isolated local agent with context-aware workers

  Scenario: Claim records the exact effective model profile for an attempt
    Given a compatible worker advertises a provider model and context limit
    When the manager assigns a task
    Then the attempt records its runtime, provider, model and context limit
    And the profile records the source of every known limit

  Scenario: A task-pinned model is admitted against its own profile
    Given a worker supports a small-context model and a large-context model
    And a task pins the small-context model
    When the task estimate exceeds the small-context admission limit
    Then the task is not assigned to the large-context profile
    And the task is marked for planning with its measured requirement

  Scenario Outline: Effective context limit is the smallest known limit
    Given a task limit of <task> tokens
    And a worker limit of <worker> tokens
    And a server-advertised limit of <server> tokens
    When I compute the effective context limit
    Then the effective limit is <effective> tokens

    Examples:
      | task | worker | server | effective |
      | 8000 | 16000  | 12000  | 8000      |
      | 0    | 16000  | 12000  | 12000     |
      | 0    | 0      | 12000  | 12000     |

  Scenario: Unknown context capacity follows an explicit bounded policy
    Given no task, worker or server context limit is known
    When the manager evaluates context admission
    Then the attempt profile marks the limit source as "unknown"
    And admission uses the configured byte bound
    And unknown capacity is not treated as infinite

  Scenario: Requested output reserves only the model-supported amount
    Given a task requests 4000 output tokens
    And the selected model supports at most 1000 output tokens
    When the manager evaluates admission
    Then it reserves 1000 output tokens from the context window

  Scenario: The input estimate includes the complete serialized request
    Given a task has a system prompt, output schema, expected files, history, tool results and dependency inputs
    When the manager estimates context input
    Then the estimate includes every serialized component
    And the estimate source is provider usage when it is available
    And otherwise it records the conservative character-to-token estimate

  Scenario: Preflight overflow is a confirmed non-start without retry cost
    Given a task cannot fit before its first model request
    When the manager evaluates admission
    Then the task has no launched attempt
    And no workspace effect exists
    And no retry is consumed

  Scenario Outline: Mid-run overflow preserves evidence and selects a safe next action
    Given a task exceeds context capacity after <effect>
    And replay safety is "<replay_safe>"
    When the runner reports the overflow
    Then the next action is "<next_action>"

    Examples:
      | effect         | replay_safe | next_action          |
      | a model reply  | true        | return_to_planning   |
      | a file write   | false       | await_operator       |

  Scenario: An unschedulable task is visible to planning instead of pending forever
    Given a task's measured requirement exceeds every compatible profile
    When the manager reconciles
    Then the task is marked unschedulable for planning
    And it is not repeatedly scheduled

  Scenario: Range reads can reduce a large planning input without escaping the workspace
    Given a readable workspace file larger than one planning chunk
    When a planner reads an offset and bounded length
    Then it receives only that range
    And an absolute, parent or oversized range request is rejected

  Scenario: An indivisible oversized input becomes terminal
    Given a required input cannot be split within the effective context limit
    When planning evaluates the input
    Then the task fails with error kind "permanent"
    And replanning is not retried indefinitely
