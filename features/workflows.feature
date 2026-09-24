@R1 @R2 @R3 @R4 @R7 @R8 @ch02 @ch04 @ch05 @ch07 @ch09 @ch11
Feature: Execute approved plans as bounded durable workflows
  A workflow owns planning, child expansion, dependency transfer and cumulative authority without giving any child a second lifecycle.
  Source: PLAN.md, "Milestone 2 — Workflows"; docs/workflows.md.

  Background:
    Given an isolated local agent
    And a fresh demo worker with 4 slots

  Scenario: A workflow root, its planner and its execution children keep separate lifecycles
    Given a workflow root whose planner returns a plan with 2 children
    When the planner task runs and the manager settles it
    Then the planner task status is "Succeeded"
    And the root records plan revision 1 with 2 children
    And each execution child is a pending ordinary task distinct from the planner

  Scenario Outline: An invalid plan creates no execution children
    Given a workflow root awaiting a plan
    When the workflow settles a plan with <defect>
    Then the plan is rejected
    And the workflow has 0 execution children

    Examples:
      | defect                   |
      | a dependency cycle       |
      | an unknown dependency    |
      | duplicate child order    |
      | an empty child list      |
      | more children than limit |

  Scenario: Child expansion is atomic with plan settlement
    Given a valid plan is awaiting workflow settlement
    And storage fails while inserting one child
    When the workflow settles the plan
    Then the plan settlement fails as a storage conflict
    And no partial plan or child record exists

  Scenario Outline: A child can only narrow authority inherited from its workflow root
    Given a read-only workflow root with a pinned model and a bounded budget
    When planner output requests <broadening>
    Then the plan is rejected
    And the workflow has 0 execution children

    Examples:
      | broadening                      |
      | a writable sandbox              |
      | a null token ceiling            |
      | a longer deadline               |
      | a model outside the root policy |
      | tools outside the root          |

  Scenario: Dependencies transfer declared validated data, not ordering alone
    Given a plan where "extract" delivers its result and a named file to "review"
    When "extract" completes with a verified result
    And the workflow delivers dependencies for "review"
    Then the consumer receives the declared result and the named file
    And the delivered bytes are counted against the consumer

  Scenario: Invalid dependency output blocks its consumer
    Given a plan where "extract" delivers its result and a named file to "review"
    When "extract" finishes without the result it declared
    And the workflow delivers dependencies for "review"
    Then the consumer does not start
    And the workflow records the failed dependency transfer

  Scenario: Workflow budgets are decremented across children and replans
    Given a workflow with 3 attempts and 1000 tokens of budget
    And a completed child consumed 1 attempt and 400 tokens
    When the workflow accepts a replan with one new child
    Then remaining attempts and tokens are not reset by the new revision

  Scenario: Replanning carries verified completed work and gives unfinished work a fresh task
    Given revision 1 has a succeeded child "done" and a pending child "keep"
    When the workflow accepts revision 2 containing both children
    Then "done" is carried over with its verified result
    And "keep" gets a fresh pending task and its obsolete task is cancelled

  Scenario: Planning remains opt-in and read-only by default
    Given a plain task submitted without workflow planning
    Then no workflow root exists
    Given a workflow root whose task tools include writing
    Then its planner task is read-only with only inspection tools
