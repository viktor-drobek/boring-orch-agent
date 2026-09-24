@R4 @R6 @ch01 @ch08 @ch12 @ch13
Feature: Operators can follow a task through the public CLI
  Receipts, status, results and events must retain their meaning across process restarts.
  Source: chapter notes 1, 8, 12 and 13; design brief stage 7.

  Scenario: Submit work before startup and retrieve its result after restart
    Given an isolated local agent
    When I submit a demo task through the CLI
    Then the CLI returns a durable receipt and the task is "Pending"
    When separate manager and worker processes execute the task
    And those processes stop and a new manager reconciles the store
    And I retrieve the result through the CLI
    Then the CLI returns the validated demo result
    And the event history includes "task.finished"
    And the worker kept a launch log for the attempt

  Scenario: Use the renamed command without breaking the legacy entry point
    Given the installed command metadata
    Then "boring-agent" is the primary console command
    And "boring-orch-agent" remains a compatibility console command
    And CLI help names the program "boring-agent"

  Scenario: The canonical project agent delegates execution to exec
    Given the canonical boring-agent definition
    Then the agent requires the "exec" subagent for implementation and verification
    And the agent refuses to execute directly when "exec" is unavailable
    And installation requires nested subagent depth 2
