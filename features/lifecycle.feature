@R2 @R3 @ch02 @ch04 @ch07
Feature: Intent has authority over execution observations
  A task's final outcome depends on committed intent and the current execution's evidence.
  Source: notes/AGENT-DESIGN-BRIEF.md sections 3 and 4; chapter notes 2, 4 and 7.

  Background:
    Given an isolated local agent
    And a fresh demo worker with 2 slots
    And a task accepted with key "lifecycle"

  Scenario: Cancellation before placement proves nothing needs to stop
    When I request cancellation
    And the manager reconciles
    Then the task status is "Cancelled"
    And the task has 0 recorded attempts
    And the event history includes "task.finished"

  Scenario: Cancellation before launch releases the reservation without a worker
    Given the task is assigned but has not started
    And the worker becomes unavailable
    When I request cancellation
    And the manager reconciles
    Then the task status is "Cancelled"
    And 0 shared slots are reserved

  Scenario: Cancellation committed before success settlement wins
    Given the executor has produced a valid result awaiting settlement
    When I request cancellation
    And the manager reconciles
    Then the task status is "Cancelled"
    And no task result is accepted
    And the attempt artifact remains available for diagnostics

  Scenario: Success committed before cancellation stays successful
    Given the executor has produced a valid result awaiting settlement
    When the manager reconciles
    And I request cancellation
    And the manager reconciles
    Then the task status is "Succeeded"
    And the event history includes "task.cancel_ignored_terminal"

  Scenario Outline: Rejected observations cannot change a running task
    Given the current executor is running
    When a <report> observation arrives
    And the manager reconciles
    Then the observation is rejected
    And the task status is "Running"
    And 1 shared slot is reserved

    Examples:
      | report         |
      | foreign worker |
      | older sequence |
      | regressive     |
