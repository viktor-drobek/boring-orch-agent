@R5 @R6 @ch01 @ch04 @ch07 @ch09 @ch11
Feature: Recover dispatch without duplicating uncertain execution
  Restart must preserve ownership, while confirmed non-start and unknown execution stay distinct.
  Source: notes/AGENT-DESIGN-BRIEF.md section 5; chapter notes 1, 4, 7, 9 and 11.

  Background:
    Given an isolated local agent
    And a fresh demo worker with 2 slots
    And a task accepted with key "recovery"

  Scenario: Attempt creation and dispatch reservation are atomic
    Given storage will fail while recording dispatch
    When the manager tries to assign work
    Then the command fails with "storage_error"
    And the task status is "Pending"
    And the task has 0 recorded attempts
    And 0 shared slots are reserved

  Scenario: Reopening the manager preserves a recoverable assignment
    Given the task is assigned but has not started
    When the manager and store are reopened
    And the manager reconciles
    And the worker delivers the assignment twice
    And the manager reconciles
    Then the task status is "Succeeded"
    And the task has 1 recorded attempt
    And the executor was claimed exactly once

  Scenario: Loss before launch does not consume the execution budget
    Given the task is assigned but has not started
    And the worker becomes unavailable
    When the manager reconciles
    And a replacement demo worker becomes available
    And the manager reconciles
    And the worker delivers the assignment twice
    And the manager reconciles
    Then the task status is "Succeeded"
    And the task has 2 recorded attempts
    And the executor was claimed exactly once

  Scenario: A crash in the launch gap must not be replayed
    Given the runner crashed immediately after claiming execution
    When the manager and store are reopened
    And the manager reconciles
    And the worker delivers the assignment twice
    Then the observation condition is "Unknown"
    And the task has 1 recorded attempt
    And 1 shared slot is reserved
    And no task result is accepted
    And the attempt error names its log file

  Scenario: A runner that cannot prove its identity never starts
    Given the task is assigned but has not started
    When the runner cannot record its process identity and delivers the assignment
    And the manager reconciles
    Then the task status is "Failed"
    And the event history includes "attempt.rejected"
    And the task has 1 recorded attempt
    And 0 shared slots are reserved
    And no task result is accepted

  Scenario: Cancellation cannot turn an unknown outcome into confirmed cessation
    Given the runner crashed immediately after claiming execution
    When the manager reconciles
    And I request cancellation
    And the manager reconciles
    Then the task status is "Scheduled"
    And the desired action is "Cancel"
    And the observation condition is "Unknown"
    And 1 shared slot is reserved

  Scenario: Operator evidence allows an unknown attempt to settle
    Given the runner crashed immediately after claiming execution
    When the manager reconciles
    And I request cancellation
    And the operator confirms that the fixture execution stopped
    And the manager reconciles
    Then the task status is "Cancelled"
    And 0 shared slots are reserved
