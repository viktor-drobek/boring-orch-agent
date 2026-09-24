@R2 @R3 @R4 @R7 @R8 @ch04 @ch07 @ch09 @ch11
Feature: Workflow plans are untrusted, bounded and idempotent
  A planner's output can only narrow root authority, sibling children share one
  workflow token ceiling, plan settlement is an idempotent command, replanning
  never reports a launched child as cancelled, and workflow schema setup never
  breaks the surrounding transaction.
  Source: docs/workflows.md, docs/budgets.md, docs/cancellation.md, docs/api-v1.md.

  Background:
    Given an isolated local agent
    And a fresh demo worker with 4 slots

  Scenario Outline: A plan cannot introduce authority the workflow root never granted
    Given a planner-trust workflow root of kind "<root>"
    When the untrusted plan proposes a child that <escalation>
    Then the plan is rejected
    And the workflow has 0 execution children

    Examples:
      | root       | escalation                                  |
      | plain demo | switches to the llm runtime                 |
      | plain demo | chooses its own model                       |
      | plain demo | declares a nested workflow                  |
      | plain llm  | resumes a coddy session                     |
      | plain llm  | mentions a bypass exec subagent             |
      | coddy llm  | widens the coddy permission mode to bypass  |
      | coddy llm  | switches to another coddy session           |
      | coddy llm  | adds a bypass exec mention                  |

  Scenario: A plan may narrow inherited Coddy authority
    Given a planner-trust workflow root of kind "coddy llm"
    When the untrusted plan proposes a child that narrows the coddy permission mode to ask
    Then the untrusted plan is accepted
    And the narrowed child keeps the root coddy session with permission mode "ask"

  Scenario: Sibling token budgets are allocated from one workflow ceiling
    Given a token-bounded review workflow with a ceiling of 100 tokens
    When the untrusted plan proposes 3 children that each request 100 tokens
    Then the plan is rejected
    And the workflow has 0 execution children

  Scenario: A child without its own token ceiling receives a bounded share
    Given a token-bounded review workflow with a ceiling of 100 tokens
    When the untrusted plan proposes 2 children without token ceilings
    Then the untrusted plan is accepted
    And every review child has a token ceiling and together they total at most 100

  Scenario: Children are not claimed after the workflow token budget is exhausted
    Given a token-bounded review workflow with a ceiling of 100 tokens
    And the untrusted plan proposes 2 children that each request 50 tokens
    When the first review child reports 150 tokens of usage
    And the manager schedules the pending review children
    Then the second review child fails with "workflow_token_budget_exhausted" before any attempt

  Scenario: Resending the same plan with the same idempotency key returns the original receipt
    Given a review workflow root served over the HTTP API
    When the plan for child "a" is posted twice with idempotency key "plan-once"
    Then both plan posts return the same receipt and the second is marked duplicate
    And the served workflow has 1 plan revision and 1 task for child "a"

  Scenario: A plan post without an idempotency key is refused
    Given a review workflow root served over the HTTP API
    When the plan for child "a" is posted without an idempotency key
    Then the last plan post is answered with HTTP 400
    And the workflow has 0 execution children

  Scenario: Reusing a plan idempotency key for a different plan conflicts
    Given a review workflow root served over the HTTP API
    When the plan for child "a" is posted with idempotency key "plan-reused"
    And the plan for child "b" is posted with idempotency key "plan-reused"
    Then the last plan post is answered with HTTP 409
    And the served workflow has 1 plan revision and 1 task for child "a"

  Scenario: A settled plan cannot be settled again without replanning
    Given a review workflow root served over the HTTP API
    When the plan for child "a" is posted with idempotency key "plan-first"
    And the plan for child "b" is posted with idempotency key "plan-second"
    Then the last plan post is answered with HTTP 409
    And the served workflow has 1 plan revision and 1 task for child "a"

  Scenario: A rejected plan post does not fail an executing workflow
    Given a review workflow root served over the HTTP API
    When the plan for child "a" is posted with idempotency key "plan-good"
    And an invalid replan is posted with idempotency key "replan-bad"
    Then the last plan post records a rejected revision
    And the served workflow is still executing with child "a" pending

  Scenario: Replanning does not report a launched child as cancelled
    Given a review workflow whose child "slow" has a launched attempt
    When the workflow accepts a replan that drops "slow"
    Then "slow" is asked to cancel but is not yet reported cancelled
    And the launched attempt of "slow" still holds its reservation
    When the runner confirms that "slow" stopped
    Then "slow" is cancelled and its reservation is released

  Scenario: Ensuring the workflow schema keeps the surrounding transaction atomic
    When a transaction records a marker, ensures the workflow schema and then fails
    Then the marker is rolled back with the rest of that transaction
