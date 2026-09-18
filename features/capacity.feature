@R7 @ch06 @ch10
Feature: Admit work only against feasible reserved capacity
  Missing evidence is not zero usage and concurrent scheduling cannot overbook a shared slot.
  Source: notes/AGENT-DESIGN-BRIEF.md section 6; chapter notes 6 and 10.

  Background:
    Given an isolated local agent

  Scenario: No feasible worker leaves work pending
    Given a task accepted with key "capacity"
    When the manager reconciles
    Then the task status is "Pending"
    And 0 shared slots are reserved

  Scenario: A stale worker cannot be treated as available
    Given a fresh demo worker with 2 slots
    And the worker becomes unavailable
    And a task accepted with key "capacity"
    When the manager reconciles
    Then the task status is "Pending"
    And the worker capacity is marked invalid

  Scenario: Concurrent admissions cannot both acquire the last shared slot
    Given the installation permits 1 active task
    And a fresh demo worker with 8 slots
    And 8 tasks are waiting for placement
    When 8 scheduling decisions run concurrently
    Then 1 shared slot is reserved
    And exactly 1 task is "Scheduled"
    And exactly 7 tasks are "Pending"

  Scenario: Feasible workers receive round-robin placements
    Given the installation permits 4 active tasks
    And a fresh demo worker with 2 slots
    And a replacement demo worker becomes available
    And 4 tasks are waiting for placement
    When the manager reconciles
    Then each worker has 2 reserved slots

  Scenario: A missing heartbeat does not release running capacity
    Given a fresh demo worker with 1 slots
    And a task accepted with key "capacity"
    And the current executor is running
    And its observation becomes stale while its process is alive
    When the manager reconciles
    Then the observation condition is "Stale"
    And the task status is "Running"
    And 1 shared slot is reserved

  Scenario: A queued assignment is waiting for capacity, not missing evidence
    Given a fresh demo worker with 1 slots
    And a task accepted with key "capacity"
    And the task is assigned but has not started
    And the assignment has waited longer than the observation window
    When the manager reconciles
    Then the observation condition is "Fresh"
    And the task status is "Scheduled"
    And 1 shared slot is reserved
