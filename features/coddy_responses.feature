@R3 @R6 @R7 @R8 @ch03 @ch06 @ch09 @coddy
Feature: Coddy Responses sessions preserve execution context and authority
  A Coddy-backed worker uses the session-aware Responses API without weakening
  the durable retry, permission, or warm-up contracts.

  Background:
    Given an isolated local agent
    And a local HTTP provider fixture

  Scenario: A Coddy worker warms one session and consumes its streamed Responses events
    Given the Coddy fixture warms a session then requests a file read and returns a final answer
    When the Coddy LLM agent runs against the fixture
    Then the task status is "Succeeded"
    And the accepted result contains answer 42
    And Coddy qualified the configured model before using the Responses API
    And every Coddy request used the same session connection
    And the session was warmed with compact then rpa-init exactly once
    And the Coddy requests asked for streaming responses
    And direct Coddy work turns carry the output token cap

  Scenario: A subagent mention carries the complete call while preserving inherited permissions
    Given a Coddy session whose parent permission mode is "accept_edits"
    When I invoke the "exec" subagent through a mention with every supported call option
    Then Coddy receives the complete subagent mention call
    And the subagent inherits permission mode "accept_edits"
    And the mention uses the parent session connection and context

  Scenario: An already prepared current session is reused without duplicate warm-up
    Given the Coddy fixture reports a prepared current session then returns a final answer
    When a Coddy task reuses that current session
    Then the task status is "Succeeded"
    And the current Coddy session is inspected once
    And no warm-up command is repeated
    And the work turn uses the current session context

  Scenario: A resumed session without explicit successful preparation is warmed
    Given the Coddy fixture reports an unproven current session then returns a final answer
    When a Coddy task reuses that current session
    Then the task status is "Succeeded"
    And the session was warmed with compact then rpa-init exactly once

  Scenario: A missing resumed session cannot grant inherited bypass permission
    Given the Coddy fixture cannot find a requested bypass session
    When a Coddy task reuses that current session
    Then the task status is "Failed"
    And no warm-up or work turn was sent

  Scenario: A missing resumed session cannot reuse persisted bypass permission
    Given the Coddy fixture loses a previously inherited bypass session
    When a Coddy task reuses that current session
    Then the task status is "Failed"
    And no warm-up or work turn was sent

  Scenario: Unrelated history cannot prove that warm-up commands succeeded
    Given the Coddy fixture reports nonadjacent warm-up replies then returns a final answer
    When a Coddy task reuses that current session
    Then the task status is "Succeeded"
    And the session was warmed with compact then rpa-init exactly once

  Scenario: Conflicting status sources and command-shaped replies cannot prove warm-up
    Given the Coddy fixture reports conflicting and command-shaped warm-up evidence then returns a final answer
    When a Coddy task reuses that current session
    Then the task status is "Succeeded"
    And the session was warmed with compact then rpa-init exactly once

  Scenario: Malformed Coddy terminal evidence leaves completion unknown
    Given the Coddy fixture warms a session then returns a malformed terminal reason
    When the Coddy LLM agent runs against the fixture
    Then the observation condition is "Unknown"

  Scenario: A busy Coddy session is a confirmed temporary rejection
    Given the Coddy fixture warms a session then rejects the work turn with HTTP 409
    When the replay-safe Coddy LLM agent runs against the fixture
    Then the task status is "Pending"
    And 0 shared slots are reserved
    And the provider received exactly 4 requests
