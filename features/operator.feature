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
