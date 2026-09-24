@R1 @R2 @R3 @R4 @R7 @R8 @ch02 @ch04 @ch05 @ch07 @ch09 @ch11
Feature: Execute approved plans as bounded durable workflows
  A workflow owns planning, child expansion, dependency transfer and cumulative authority without giving any child a second lifecycle.
  Source: PLAN.md, "Milestone 2 — Workflows".

  Background:
    Given an isolated local agent with workflow support

  Scenario: A workflow root, its planner and its execution children keep separate lifecycles
    Given a workflow root with an opt-in planning task
    When planning produces a valid child plan
    Then the root records its workflow state, iteration and plan revision
    And the planning task has its own ordinary terminal result
    And each execution child has one ordinary task lifecycle

  Scenario: Workflow child identity cannot collide with a caller idempotency key
    Given a workflow plan creates child index 2 at revision 1
    And a caller submits work using the same visible key text
    When both records are accepted
    Then the workflow child has an internal workflow identity
    And the caller command keeps its independent idempotency receipt

  Scenario Outline: An invalid plan creates no execution children
    Given planner output has <defect>
    When the workflow validates and settles the plan
    Then the plan is rejected
    And the workflow has 0 execution children

    Examples:
      | defect                  |
      | a dependency cycle      |
      | an unknown dependency   |
      | duplicate child order   |
      | an empty child list     |
      | more children than limit|

  Scenario: Child expansion is atomic with plan settlement
    Given a valid plan is awaiting workflow settlement
    And storage fails while inserting one child
    When the workflow settles the plan
    Then the plan task is not accepted
    And the workflow has 0 execution children
    And no partial child reservation exists

  Scenario: A child can only narrow authority inherited from its workflow root
    Given a read-only workflow root with a restrictive budget and model policy
    When planner output requests a writable child with broader tools, budget or model policy
    Then the plan is rejected
    And no writable child is created

  Scenario: Dependencies transfer declared validated data, not ordering alone
    Given a child depends on another child's result and named output file
    When the dependency succeeds
    Then the dependent child receives only the declared validated result and named file
    And those delivered bytes are counted in the dependent context estimate

  Scenario: Invalid dependency output blocks its consumer
    Given a dependency finishes without its declared result or named file
    When the workflow evaluates its consumer
    Then the consumer does not start
    And the workflow records the failed dependency transfer

  Scenario: Workflow budgets are decremented across children and replans
    Given a workflow has a finite token and attempt budget
    And completed children have consumed part of both budgets
    When the workflow accepts a replan
    Then the replacement children inherit only the remaining budget
    And a new plan revision does not reset any allowance

  Scenario: Replanning carries verified completed work and cancels obsolete children first
    Given revision 1 has a succeeded child, a queued obsolete child and measured context evidence
    When the workflow accepts revision 2
    Then revision 2 receives the succeeded child's verified output and measurements
    And the obsolete queued child is cancelled before replacement work starts
    And completed work is not regenerated

  Scenario: Planning remains opt-in and read-only by default
    Given a single-shot model task without workflow planning enabled
    When its context preflight succeeds
    Then the manager executes the task without creating a workflow
    Given a workflow planning task without an explicit write grant
    When the planner runs
    Then its workspace policy is read-only

  Scenario: Planner context threshold is an admission policy, not a quality claim
    Given a workflow exceeds the configured planner-context threshold
    When the manager chooses between direct execution and planning
    Then it compares the threshold with the effective model limit
    And it records the scheduling reason without asserting result quality
