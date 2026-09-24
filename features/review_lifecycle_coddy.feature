@R3 @R5 @R6 @R8 @ch03 @ch06 @ch09 @ch11 @coddy
Feature: Native sessions and Coddy authority fail closed on unobserved or uncertain evidence
  A session's permission, preparation and dependency state is adopted only from
  durable, observed evidence. An uncertain warm-up is never replayed without an
  operator, and a job never runs on a session that has not been prepared.
  Source: AGENTS.md "Native session lifecycle" and "Legacy worker provider boundary"; docs/architecture.md.

  Background:
    Given an isolated local agent

  Scenario: A resumed Coddy session that reports no permission mode grants its child only ask
    Given a local HTTP provider fixture
    And the Coddy fixture resumes a session without a reported permission mode for a bypass mention
    When a Coddy task reuses that current session
    Then the task status is "Succeeded"
    And the subagent session permission was set to "ask" and never to bypass
    And the lifecycle records resumed session permission mode "ask"

  Scenario: A warm-up command whose stream ends without DONE leaves the session recovering
    Given a registered native job whose Coddy warm-up stream for "/compact" ends without DONE
    When the native job is started with the Coddy warm-up executor
    Then starting the native job is refused
    And the native job session is "recovering" with retry key for "/compact"
    And an unconfirmed native warm-up retry is refused without sending a command
    And an operator-confirmed native warm-up retry reuses the "/compact" key and prepares the session

  Scenario: A job is never started on a session whose warm-up failed
    Given a registered native job whose Coddy warm-up "/compact" was rejected with HTTP 400
    When the native job is started again with a recording warm-up executor
    Then starting the native job is refused
    And no native run was claimed and no warm-up command was recorded

  Scenario: Dependents that mention an explicit session become ready and reuse it only sequentially
    Given a warmed native root job and two dependents that mention the root session
    When the native root job succeeds
    Then both dependent native jobs are ready on the root session with delivered transfers
    And the second dependent cannot start while the first dependent is running
    And the second dependent starts on the root session after the first completes

  Scenario: Lifecycle schema setup stays inside one serialized store transaction
    When the lifecycle schema is created in a store transaction that fails after setup
    Then the store transaction was still open after lifecycle schema setup
    And the failed transaction left no lifecycle table behind
