@R4 @R6 @ch01 @ch08
Feature: Claude Code runs the project agent with a dedicated exec subagent
  Claude Code has the same coordinator and executor split as Coddy: the project
  agent plans and reviews, and every execution step goes to the exec subagent.
  Source: AGENTS.md project instructions and the Coddy project agent definition.

  Scenario: The Claude project agent delegates execution to the Claude exec subagent
    Given the Claude Code "boring-agent" agent definition
    Then the Claude definition inherits its model and permission mode
    And the Claude coordinator can spawn subagents but cannot edit files or run shell commands
    And the Claude coordinator delegates implementation and verification to "exec"
    And the Claude coordinator reports BLOCKED instead of executing directly when "exec" is unavailable

  Scenario: The Claude project agent shares the Coddy project brief
    Given the Claude Code "boring-agent" agent definition
    Then its project brief paragraphs match the Coddy project agent verbatim

  Scenario: The Claude exec subagent performs a self-contained job within inherited authority
    Given the Claude Code "exec" agent definition
    Then the Claude definition inherits its model and permission mode
    And the Claude exec subagent may use the implementation tools
    And the Claude exec subagent runs the focused checks and the release pipeline
    And the Claude exec subagent never widens authority or replays an Unknown outcome
    And the Claude exec subagent refuses native operational jobs that belong to Coddy exec

  Scenario: The Claude agent definitions ship with the package
    Given the package data configuration
    Then the Claude agent definitions are packaged and checked by the wheel smoke test
