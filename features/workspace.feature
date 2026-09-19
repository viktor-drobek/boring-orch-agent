@R1 @R3 @ch02 @ch03 @ch05
Feature: Enforce task tool permissions at the workspace boundary
  A model may only use explicitly permitted tools on accessible workspace files.
  Source: notes/AGENT-DESIGN-BRIEF.md sections 3 and 4; notes/IMPLEMENTATION.md tool policy.

  Background:
    Given an isolated local agent
    And a workspace containing a visible note and a hidden credential

  Scenario: Read-only tasks can inspect visible text
    When the read-only agent reads "note.txt"
    Then the tool returns the note contents

  Scenario Outline: Restricted paths are rejected
    When the read-only agent reads "<path>"
    Then the command fails with "invalid_request"

    Examples:
      | path           |
      | .env           |
      | ../outside.txt |
      | /etc/passwd    |
      | linked.txt     |

  Scenario: A permitted write creates its missing parent directories
    When the writing agent writes "generated/api/schema.json"
    Then the workspace file "generated/api/schema.json" holds the written text

  Scenario: A permitted write still cannot leave the workspace
    When the writing agent writes "../escaped/schema.json"
    Then the command fails with "invalid_request"

  Scenario: An unpermitted write cannot change a file
    When the read-only agent tries to overwrite the note
    Then the command fails with "invalid_request"
    And the note is unchanged
