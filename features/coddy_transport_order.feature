@R4 @R6 @ch01 @ch08
Feature: Every agent prefers the Coddy API, then ACP, then plain CLI
  Agents that use Coddy choose the same transport in the same order, and the
  operator learns the choice on the first run in a new project.
  Source: AGENTS.md project instructions and the synchronized rule trees.

  Scenario Outline: An agent instruction file carries the Coddy transport order
    Given the agent instruction file "<path>"
    Then it contains the canonical Coddy transport order section
    And the transport order is API, then ACP, then plain CLI
    And the first run in a new project tells the operator the selected transport
    And the first run in a new project asks the operator for the permission mode and model
    And an Unknown outcome is never retried on another transport

    Examples:
      | path                            |
      | AGENTS.md                       |
      | CLAUDE.md                       |
      | .cursor/rules/workflow.mdc      |
      | .claude/rules/workflow.md       |
      | .coddy/rules/boring-agent.md    |
      | .coddy/agents/boring-agent.md   |
      | .claude/agents/boring-agent.md  |
      | .claude/agents/exec.md          |
      | SKILL.md                        |

  Scenario: The Coddy integration guide documents the transport order
    Given the agent instruction file "examples/coddy/README.md"
    Then the transport order is API, then ACP, then plain CLI
    And the first run in a new project tells the operator the selected transport
    And the first run in a new project asks the operator for the permission mode and model
