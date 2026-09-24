@R2 @R8 @ch03 @ch09 @ch13
Feature: Accept verified output rather than a runtime success claim
  A completed execution must produce an intact artifact that meets its submitted output contract.
  Source: notes/AGENT-DESIGN-BRIEF.md sections 3 and 9; chapter notes 3, 9 and 13.

  Background:
    Given an isolated local agent
    And a fresh demo worker with 2 slots

  Scenario: A conforming result is retrievable
    Given a task requiring an integer answer and returning 42
    When the first execution finishes and is reconciled
    Then the task status is "Succeeded"
    And the accepted result contains answer 42

  Scenario: A successful runtime with the wrong result type fails acceptance
    Given a task requiring an integer answer and returning "forty-two"
    When the first execution finishes and is reconciled
    Then the task status is "Failed"
    And no task result is accepted
    And the event history includes "attempt.result_rejected"

  Scenario: A file the task expects but never wrote cannot become success
    Given a task that expects the file "reports/summary.md" and finishes without writing it
    When the first execution finishes and is reconciled
    Then the task status is "Failed"
    And no task result is accepted
    And the failure reason mentions "reports/summary.md"

  Scenario: An expected file that exists lets the result be accepted
    Given a task that expects the file "reports/summary.md" and finishes without writing it
    And the workspace already contains "reports/summary.md"
    When the first execution finishes and is reconciled
    Then the task status is "Succeeded"

  Scenario Outline: An expected path outside the workspace is rejected at submission
    When I submit a task expecting the file "<path>"
    Then the command fails with "invalid_request"
    And there is exactly 0 tasks in the store

    Examples:
      | path           |
      | ../outside.md  |
      | /etc/passwd    |
      | .hidden/a.md   |

  Scenario: Altering an artifact before settlement cannot produce success
    Given a task accepted with key "result"
    And the executor has produced a valid result awaiting settlement
    When the attempt artifact is altered
    And the manager reconciles
    Then the task status is "Failed"
    And no task result is accepted

  Scenario: Retrieval checks integrity even after acceptance
    Given a task accepted with key "result"
    And the executor has produced a valid result awaiting settlement
    When the manager reconciles
    And the attempt artifact is altered
    And I retrieve the accepted result
    Then the command fails with "invalid_request"
