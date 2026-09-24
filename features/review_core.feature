@R1 @R2 @R5 @R8 @ch03 @ch05 @ch13
Feature: Keep tool validation, settlement, retention and store opening deterministic
  Hostile model input, a vanished artifact, a late command or a failed open must
  end in a normal, bounded outcome instead of wedging execution or capacity.
  Source: core review findings F5, F6, F13, F17 and F18.

  Background:
    Given an isolated local agent

  @R1 @R3 @ch02 @ch03 @ch05
  Scenario: A model tool path containing a NUL byte is a tool error, not a runner crash
    Given a local HTTP provider fixture
    And the fixture model first reads a path containing a NUL byte then returns a final answer
    When the core-review LLM agent runs against the fixture
    Then the task status is "Succeeded"
    And 0 shared slots are reserved
    And the provider received exactly 2 requests
    And the model received a tool error for its NUL path

  @R1 @R3 @ch02 @ch03 @ch05
  Scenario: A failed file operation reports no host path to the model
    Given a local HTTP provider fixture
    And the installation and worker permit workspace writes
    And the fixture model first writes beneath a regular file then returns a final answer
    When the core-review writing LLM agent runs against the fixture
    Then the task status is "Succeeded"
    And the model received a tool error naming the relative path "input.txt/child.txt"
    And no tool result sent to the model contains an absolute host path

  @R2 @R8 @ch03 @ch09 @ch13
  Scenario: An expected file path containing a NUL byte is rejected at submission
    When I submit a task expecting a file path containing a NUL byte
    Then the command fails with "invalid_request"
    And there is exactly 0 tasks in the store

  @R2 @R5 @R8 @ch03 @ch09 @ch13
  Scenario: A succeeded attempt whose artifact vanished fails acceptance without stopping settlement
    Given a fresh demo worker with 2 slots
    And two demo tasks have produced valid results awaiting settlement
    And the first awaiting task's result artifact has vanished
    When the manager reconciles
    Then the first awaiting task fails acceptance
    And the second awaiting task is accepted
    And 0 shared slots are reserved

  @R4 @R5 @R6 @ch01 @ch04 @ch11
  Scenario: A cancel between retention phases cannot block task deletion
    Given retention has recorded deletion intent for an expired task
    And retention is interrupted right after its dependents phase
    And a cancel with a new key arrives for the retained terminal task
    When a new manager resumes retention
    Then the task is deleted in foreign-key order
    And no orphan attempt, event or artifact record remains

  @R5 @ch01 @ch11
  Scenario: A store that cannot be opened releases its database connection
    Given the store records an unsupported future schema version
    When a process tries to open that store while connections are tracked
    Then the command fails with "storage_error"
    And every tracked database connection is closed
