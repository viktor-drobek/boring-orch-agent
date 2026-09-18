@R1 @R7 @R8 @ch03 @ch09
Feature: Retry only confirmed safe executions within cumulative budgets
  Retransmission is not a retry, and a retry does not erase prior evidence or usage.
  Source: notes/AGENT-DESIGN-BRIEF.md sections 3 and 6; chapter notes 3 and 9.

  Background:
    Given an isolated local agent
    And a fresh demo worker with 2 slots

  Scenario Outline: Replay policy controls confirmed failures
    Given a task that fails once with "<failure>" and replay safety "<safe>"
    When the first execution finishes and is reconciled
    Then the task status is "<status>"

    Examples:
      | failure   | safe  | status  |
      | transient | true  | Pending |
      | transient | false | Failed  |
      | permanent | true  | Failed  |

  Scenario: An eligible retry has a new identity and rejects old observations
    Given a task that fails once with "transient" and replay safety "true"
    When the first execution finishes and is reconciled
    And the retry backoff elapses
    And the worker delivers the assignment twice
    And the manager reconciles
    And the previous attempt sends a late running observation
    Then the task status is "Succeeded"
    And the task has 2 recorded attempts
    And the attempts have different identities
    And the observation is rejected

  Scenario: Exhausted attempt budget is terminal
    Given a replay-safe task whose two executions both fail
    When the first execution finishes and is reconciled
    And the retry backoff elapses
    And the worker delivers the assignment twice
    And the manager reconciles
    Then the task status is "Failed"
    And the task has 2 recorded attempts
    And 0 shared slots are reserved

  Scenario Outline: Missing or exhausted cumulative usage blocks retry
    Given a replay-safe task with a token budget of 10
    When its executor reports a confirmed transient failure with <usage> tokens
    And the manager reconciles
    Then the task status is "Failed"
    And settled usage is marked "<validity>"

    Examples:
      | usage   | validity |
      | 10      | known    |
      | unknown | unknown  |

  Scenario: Usage within one attempt cannot go backwards
    Given a replay-safe task with a token budget of 10
    And the current executor has reported 10 consumed tokens
    When the same executor reports a transient failure with only 1 consumed token
    And the manager reconciles
    Then the task status is "Failed"
    And the task retains at least 10 known consumed tokens
    And the task has 1 recorded attempt

  Scenario: An expired pending task never starts
    Given a task accepted with key "expired"
    And its task deadline has elapsed
    When the manager reconciles
    Then the task status is "Failed"
    And the task has 0 recorded attempts
    And 0 shared slots are reserved
