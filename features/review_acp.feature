@R3 @R6 @ch03 @ch06
Feature: ACP launch plans keep agent state and host secrets on the right side of the sandbox
  A prepared ACP launch must point the agent home at the state directory the
  agent can actually reach, and must hand the agent only allowlisted variables.
  Source: docs/isolation.md, "Launch environment".

  Scenario: A bubblewrap launch points the agent home at the mounted state directory
    Given an ACP task with a private workspace and state directory
    And bubblewrap isolation is available for the ACP launch
    When the ACP launch plan is prepared
    Then the ACP state directory is bound at "/.acp-state" in the sandbox
    And the ACP environment sets "HOME" to "/.acp-state"
    And every ACP XDG directory lies under "/.acp-state"
    And no ACP environment value names the host state directory

  Scenario: A trusted-operator launch points the agent home at the host state directory
    Given an ACP task with a private workspace and state directory
    And the ACP task requests workspace-write
    And bubblewrap isolation is unavailable for the ACP launch
    When the ACP launch plan is prepared
    Then the ACP launch is the unwrapped agent command
    And the ACP environment sets "HOME" to the host state directory
    And every ACP XDG directory lies under the host state directory

  Scenario Outline: Host secrets and unlisted variables never reach the agent
    Given an ACP task with a private workspace and state directory
    And the ACP task requests workspace-write
    And bubblewrap isolation is <capability> for the ACP launch
    And the ACP host environment contains:
      | name                  | value           |
      | PATH                  | /usr/bin:/bin   |
      | LANG                  | C.UTF-8         |
      | LC_ALL                | C.UTF-8         |
      | TERM                  | xterm           |
      | BOA_API_KEY           | sk-secret       |
      | OPENAI_API_KEY        | sk-openai       |
      | GITHUB_TOKEN          | ghp-secret      |
      | AWS_SECRET_ACCESS_KEY | aws-secret      |
      | SSH_AUTH_SOCK         | /run/agent.sock |
      | RANDOM_HOST_SETTING   | leak            |
    When the ACP launch plan is prepared
    Then the ACP environment keeps "PATH", "LANG", "LC_ALL" and "TERM" unchanged
    And the ACP environment omits "BOA_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK" and "RANDOM_HOST_SETTING"
    And no ACP environment value contains "secret"

    Examples:
      | capability  |
      | available   |
      | unavailable |

  Scenario: A declared pass-through variable reaches the agent
    Given an ACP task with a private workspace and state directory
    And bubblewrap isolation is available for the ACP launch
    And the ACP host environment contains:
      | name          | value         |
      | NODE_OPTIONS  | --no-warnings |
      | OTHER_SETTING | leak          |
    And the ACP task declares environment pass-through "NODE_OPTIONS"
    When the ACP launch plan is prepared
    Then the ACP environment sets "NODE_OPTIONS" to "--no-warnings"
    And the ACP environment omits "OTHER_SETTING"

  Scenario Outline: A pass-through declaration cannot reopen a secret
    Given an ACP task with a private workspace and state directory
    And bubblewrap isolation is available for the ACP launch
    And the ACP host environment contains:
      | name   | value     |
      | <name> | sk-secret |
    And the ACP task declares environment pass-through "<name>"
    When the ACP launch plan is prepared
    Then the ACP launch is refused with a message mentioning "pass-through"

    Examples:
      | name           |
      | BOA_API_KEY    |
      | OPENAI_API_KEY |
      | GITHUB_TOKEN   |
      | DB_PASSWORD    |
