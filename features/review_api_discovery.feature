@R4 @R6 @R7 @ch01 @ch06 @ch08 @ch10
Feature: The HTTP API cannot grant discovery consent or drop malformed requests
  Discovery approval is operator consent and cannot be carried by a request body.
  The API answers every request with its documented JSON error shape.
  Source: docs/api-v1.md; docs/discovery.md.

  Scenario: An HTTP caller cannot approve a shell command for discovery
    Given a token-less loopback API over a fresh review store
    When an HTTP caller posts a shell command route to the discovery approve route
    Then the review API answers 403 with error "operator_only"
    And the review store holds no discovery approval
    When an HTTP caller asks the review API to handshake that shell command route
    Then the review API answers 409 with error "conflict"
    And the review store holds no discovery evidence

  Scenario: An operator-approved credential cannot be redirected to a caller-chosen base URL
    Given a token-less loopback API over a fresh review store
    And the operator approves a generative review route that sends its credential to the approved listener
    When an HTTP caller runs that generative review probe against its own base URL
    Then the review API answers 409 with error "conflict"
    And neither review listener has received a request
    When an HTTP caller runs that generative review probe exactly as approved
    Then the review API answers 202 with probe status "completed"
    And only the approved review listener received the credential, exactly once

  Scenario: A token-less loopback API refuses rebinding-shaped mutating requests
    Given a token-less loopback API over a fresh review store
    When a review task is posted with Host header "attacker.example"
    Then the review API answers 403 with error "forbidden"
    When a review task is posted with Content-Type "text/plain"
    Then the review API answers 415 with error "unsupported_media_type"
    And the review store holds no task
    When a review task is posted as loopback JSON
    Then the review API answers 202 with a task receipt

  Scenario: A failed approval audit leaves no orphan approval behind
    Given a fresh review store whose discovery audit insert fails
    When the operator approves a handshake review route despite the failing audit
    Then the review approval fails with a storage error
    And the review store holds no discovery approval

  Scenario: Malformed requests and internal failures still receive a JSON error
    Given a review API that requires the bearer token "review-token"
    When an HTTP caller sends a non-ASCII bearer token to the review API
    Then the review API answers 401 with error "unauthorized"
    Given the operator approves a handshake review route
    When an HTTP caller asks the review API to handshake that route with max_output_bytes "x"
    Then the review API answers 400 with error "invalid_request"
    When an HTTP caller reads an unknown review workflow
    Then the review API answers 404 with error "not_found"
    When the review capacity read fails with an internal error
    Then the review API answers 500 with error "internal_error"
    And the review error message does not reveal the internal failure
