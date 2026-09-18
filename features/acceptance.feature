@R1 @R4 @ch02 @ch05 @ch08 @ch11
Feature: Durable command acceptance
  Operators need stable receipts before execution starts so a lost reply cannot duplicate work.
  Source: notes/AGENT-DESIGN-BRIEF.md sections 3 and 5; chapter notes 2, 5, 8 and 11.

  Background:
    Given an isolated local agent

  Scenario: Accepted work is immediately queryable but has not run
    When I submit a valid task with key "request-1"
    Then the task status is "Pending"
    And the receipt identifies different command and task records
    And the task has 0 recorded attempts

  Scenario: A lost acknowledgement does not create another task after restart
    Given a task accepted with key "request-1"
    When the manager and store are reopened
    And I repeat the original submission
    Then I receive the original receipt marked as a duplicate
    And there is exactly 1 task in the store

  Scenario: An idempotency key cannot change the requested work
    Given a task accepted with key "request-1"
    When I submit a different objective with the same key
    Then the command fails with "conflict"
    And there is exactly 1 task in the store
    And the accepted objective is unchanged

  Scenario Outline: Invalid intent is rejected before acceptance
    When I submit a task with invalid <input>
    Then the command fails with "invalid_request"
    And there is exactly 0 tasks in the store

    Examples:
      | input              |
      | empty objective    |
      | supplied state     |
      | negative deadline  |
      | external schema    |
      | unauthorized write |

  Scenario: A storage failure cannot leave an accepted task without a receipt
    Given storage will fail while recording the command
    When I submit a valid task with key "request-1"
    Then the command fails with "storage_error"
    And there is exactly 0 tasks in the store

  Scenario: Cancelling a missing task creates no command
    When I cancel a nonexistent task
    Then the command fails with "not_found"
    And there is exactly 0 tasks in the store
