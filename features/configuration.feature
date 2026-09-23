@R1 @R4 @R7 @ch03 @ch05 @ch08
Feature: Keep operational configuration consistent across durable roles
  Operators need visible tunables without allowing configuration to weaken durable or workspace boundaries.
  Source: PLAN.md, "Milestone 0 — Configuration".

  Background:
    Given an isolated local agent with a configuration file

  Scenario: Config-only initialization does not overwrite an existing file without force
    Given the configuration file contains an operator-selected poll interval
    When I initialize configuration only without force
    Then configuration-only initialization refuses to overwrite the file
    And the existing configuration file is unchanged

  Scenario: The effective configuration preserves store-owned authority
    Given the configuration file tries to set the workspace root and active-task limit
    When I resolve the effective configuration
    Then the effective workspace root comes from durable settings
    And the effective active-task limit comes from durable settings
    And the effective configuration records its revision hash

  Scenario: A detached runner receives the worker's exact effective configuration
    Given a worker has resolved a configuration revision
    When the worker launches an assigned runner
    Then the runner receives that configuration path and revision hash
    And the effective configuration is written for operator inspection

  Scenario: Configuration drift between worker and runner fails before runtime effects
    Given an assigned runner resolves a different effective configuration revision
    When the runner starts
    Then the attempt fails with error kind "permanent"
    And the runtime has not started
    And no retry is consumed

  Scenario: Configured store-managed paths remain outside the task workspace
    Given artifact, log and lock paths are configured below the store home
    When a workspace tool resolves each configured path
    Then every path is rejected as outside the accessible workspace

  Scenario: A recoverable store error in a long-lived loop is logged and retried
    Given the manager loop encounters a transient storage error
    When the manager continues running
    Then it emits one structured error record with bounded backoff
    And it performs a later reconciliation

  Scenario: A one-shot manager command exposes an unrecovered tick failure
    Given the manager reconciliation raises a storage error
    When I run the manager once command
    Then the command exits non-zero
    And the failure is reported as structured JSON
