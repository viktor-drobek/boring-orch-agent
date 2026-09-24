@R3 @R4 @R6 @R7 @ch01 @ch03 @ch06 @ch08 @ch10
Feature: Discover executable environments only with the requested level of consent
  Inventory can record bounded evidence about a route, but it must not activate an agent or disclose credentials without explicit approval.
  Source: PLAN.md, "Milestone 3 — Inventory and approval".

  Scenario: Passive initialization performs no process or network activity
    Given an installation with unknown available agent runtimes
    When I initialize the store with passive inventory
    Then no subprocess is started
    And no network request is made
    And the inventory records only locally available passive metadata

  Scenario: Handshake discovery is isolated and cleans up its entire process group
    Given an operator approves handshake discovery for one agent route
    When the handshake reports its capabilities
    Then the agent uses isolated state
    And the orchestrator terminates the handshake process group
    And no handshake child process remains

  Scenario: A hard probe timeout kills the process group instead of abandoning a future
    Given an approved handshake probe stops responding
    When its hard timeout elapses
    Then the process group is killed
    And the probe result records a bounded timeout outcome
    And no descendant remains running

  Scenario: Generative probing requires explicit consent and a cost policy
    Given generative discovery is not approved
    When I request a bounded model completion probe
    Then the probe request is refused
    And no provider request is sent
    Given generative discovery is approved with a stated cost policy
    When I request the same probe
    Then exactly one bounded completion request is sent

  Scenario: Probe evidence is bounded and sanitized before storage
    Given an approved probe returns a long response containing a credential-like value and a URL
    When the orchestrator records probe evidence
    Then recorded output is capped at the configured bound
    And the credential-like value is redacted
    And the URL and version metadata remain attributable to the probe

  Scenario: Route approval binds resolved environment overrides and executable identity
    Given an operator approves a resolved provider route and executable identity
    When an environment override or executable identity changes
    Then the old approval is rejected
    And the route requires explicit re-approval

  Scenario: An unlisted route escape hatch is one invocation only
    Given an operator invokes one unlisted route with explicit approval
    When that invocation completes
    Then the exception is recorded in the audit history
    And a later invocation requires a new explicit approval

  Scenario: Discovery stores credentials only by reference
    Given a discovery route requires an API credential
    When the route is recorded
    Then the inventory contains a credential reference
    And it contains neither the credential value nor parsed YAML credential content
