@R1 @R2 @R4 @R5 @R6 @R7 @R8 @ch01 @ch04 @ch05 @ch07 @ch09 @ch11 @ch13
Feature: Preserve safety while retaining and recovering durable orchestration data
  Cleanup, settlement and schema evolution must not weaken ownership, idempotency or result integrity.
  Source: PLAN.md, "Milestone H — Hardening from the code reviews".

  Scenario Outline: Loop subcommands refuse unsupported process platforms before opening the store
    Given the host lacks <capability>
    When I start the "<command>" loop subcommand
    Then the command refuses to start
    And the error mentions "runs on Linux only"
    And no process lock is acquired

    Examples:
      | capability                    | command |
      | /proc process identity         | manager |
      | flock advisory locking         | worker  |

  Scenario: A locked pathname is never removed by age-based retention
    Given a live manager holds its process lock
    And the lock file is older than the retention threshold
    When retention runs
    Then the lock file remains linked to the live lock inode
    And a second manager cannot acquire the lock

  Scenario: An expired task retains an idempotency tombstone
    Given a completed task is eligible for retention deletion
    And its original submit command has not reached the idempotency horizon
    When retention deletes the task payload and I repeat the original submission
    Then I receive the original receipt marked as a duplicate
    And no new task is scheduled

  Scenario: Interrupted retention resumes its durable deletion intent
    Given retention has recorded deletion intent for an expired task
    And retention stops after deleting a dependent record
    When a new manager resumes retention
    Then the task is deleted in foreign-key order
    And no orphan attempt, event or artifact record remains

  Scenario: Retention keeps artifacts still required by a live workflow
    Given a live workflow has a succeeded child with a delivered result artifact
    When retention runs
    Then the child artifact remains available to the dependent child

  Scenario: Result retrieval after retained artifact expiry has a distinct outcome
    Given a succeeded task whose result artifact has expired under retention policy
    When I retrieve its result
    Then the command fails with "gone"
    And the task history remains queryable

  Scenario: Task and event listings use a stable opaque cursor
    Given more tasks and events exist than the requested page size
    When I retrieve consecutive pages using the returned cursor
    Then each item appears exactly once in documented order
    And an invalid cursor is rejected without changing the listing order

  Scenario: Success finished before its deadline wins over a later deadline sweep
    Given an attempt produced a valid result before its task deadline
    And the deadline passes before the manager settles the result
    When the manager reconciles
    Then the task status is "Succeeded"
    And the event history includes "task.finished"

  Scenario: Artifact validation does not hold the store write transaction
    Given settlement is validating a large result artifact outside the write transaction
    When another caller submits work and a worker records a heartbeat
    Then both concurrent store operations complete
    And settlement applies only its pre-validated verdict

  Scenario: A stale validation verdict cannot settle changed artifact data
    Given settlement has pre-validated an attempt's artifact checksum and schema
    When the artifact, result path or task specification changes before settlement
    Then the pre-validated verdict is rejected
    And the task does not become "Succeeded"

  Scenario: Relaunch backoff survives a worker restart
    Given a runner exits before claiming and the outbox records a future relaunch time
    When the worker restarts before that time
    Then it does not relaunch the runner early
    When the durable relaunch time elapses
    Then it launches the runner with the next bounded backoff

  Scenario Outline: Schema opening honors supported versioned migrations
    Given a store at schema state "<state>"
    When a process opens the store
    Then the schema outcome is "<outcome>"

    Examples:
      | state                       | outcome                         |
      | fresh                       | initialized at current version  |
      | previous supported release  | migrated once to current version|
      | unsupported future release  | rejected without mutation       |
      | interrupted current migration| resumed from durable migration intent |

  Scenario: Concurrent openers cannot apply the same migration twice
    Given two processes open a store requiring one migration
    When they race to open the store
    Then exactly one migration is applied
    And both processes observe the same supported schema version
