@R3 @R6 @R7 @R8 @ch03 @ch06 @ch09
Feature: Provider uncertainty cannot become free capacity or safe replay
  An HTTP response describes transport; it is not always proof that model execution stopped.
  Source: notes/IMPLEMENTATION.md cancellation and limits; design brief R6 through R8.

  Background:
    Given an isolated local agent
    And a local HTTP provider fixture

  Scenario Outline: Compatible providers execute a bounded file-reading agent
    Given the "<provider>" fixture requests a file read then returns a final answer
    When the LLM agent runs against the fixture
    Then the task status is "Succeeded"
    And the accepted result contains answer 42
    And the provider received the permitted file contents
    And the task reports 60 known consumed tokens
    And the provider received exactly 2 requests

    Examples:
      | provider  |
      | openai    |
      | anthropic |
      | ollama    |

  Scenario: The OpenAI-compatible request asks the server for a JSON object
    Given the "openai" fixture requests a file read then returns a final answer
    When the LLM agent runs against the fixture
    Then the task status is "Succeeded"
    And the provider was asked for a JSON object response

  Scenario: An operator can disable JSON mode for a server that rejects it
    Given the worker disables provider JSON mode
    And the "openai" fixture requests a file read then returns a final answer
    When the LLM agent runs against the fixture
    Then the task status is "Succeeded"
    And the provider was not asked for a JSON object response

  Scenario: A confirmed rate-limit rejection is eligible for manager-owned retry
    Given the provider rejects the request with HTTP 429
    When the replay-safe LLM agent runs against the fixture
    Then the task status is "Pending"
    And 0 shared slots are reserved
    And the provider received exactly 1 request

  Scenario Outline: Ambiguous remote errors keep their reservation
    Given the provider rejects the request with HTTP <status>
    When the replay-safe LLM agent runs against the fixture
    Then the observation condition is "Unknown"
    And 1 shared slot is reserved
    And the provider received exactly 1 request
    And no task result is accepted

    Examples:
      | status |
      | 500    |
      | 502    |
      | 503    |
      | 504    |

  Scenario Outline: A completion cut off before any content is a clear permanent failure
    Given the "<provider>" fixture returns an empty completion cut off at the output limit
    When the replay-safe LLM agent runs against the fixture
    Then the task status is "Failed"
    And the failure reason mentions "truncated"
    And 0 shared slots are reserved

    Examples:
      | provider  |
      | openai    |
      | anthropic |
      | ollama    |

  Scenario: A remote timeout cannot be automatically replayed
    Given the provider accepts the request but does not reply before its deadline
    When the replay-safe LLM agent runs against the fixture
    Then the observation condition is "Unknown"
    And 1 shared slot is reserved
    And the provider received exactly 1 request
