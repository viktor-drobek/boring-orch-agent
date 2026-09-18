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

  Scenario: A remote timeout cannot be automatically replayed
    Given the provider accepts the request but does not reply before its deadline
    When the replay-safe LLM agent runs against the fixture
    Then the observation condition is "Unknown"
    And 1 shared slot is reserved
    And the provider received exactly 1 request
