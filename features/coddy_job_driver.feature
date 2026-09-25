@R3 @R6 @R7 @R8 @ch03 @ch06 @ch09 @coddy
Feature: The Coddy job driver supervises an unattended native job without widening authority
  An operator launches one native job through the Coddy Responses API and leaves
  it unattended. The driver answers permission prompts fail-closed, never reports
  an unconfirmed turn as success, and tells a lost server from an unknown session.

  Scenario Outline: A child's shell command is allowed only when it is a read or a project check
    Given an unattended driver policy for a job workspace
    When a child asks to run "<command>"
    Then the driver answers "<answer>"

    Examples:
      | command                                                         | answer |
      | git status --short                                              | allow  |
      | git -C WORKSPACE diff --stat                                    | allow  |
      | PROJECT_PYTHON scripts/check_cucumber.py --dry-run              | allow  |
      | grep -rhoE '(Scenario\|Outline): .*' features \| sort -u         | allow  |
      | echo 'a > b' && sed -n '1,5p' README.md                         | allow  |
      | git push                                                        | reject |
      | ls NEWLINE rm -rf features                                      | reject |
      | echo hi > features/x.feature                                    | reject |
      | python3 -c 'import os'                                          | reject |
      | PROJECT_PYTHON -c 'import os'                                   | reject |
      | cd /tmp && ls                                                   | reject |
      | cd WORKSPACE-evil && ls                                         | reject |
      | echo $(id)                                                      | reject |
      | sed -i s/a/b/ README.md                                         | reject |
      | PROJECT_PYTHON scripts/bind_text_quality.py --check             | allow  |
      | PROJECT_PYTHON scripts/bind_text_quality.py                     | reject |
      | PROJECT_PYTHON scripts/bind_claude.py --write                   | reject |

  Scenario: A permission prompt whose arguments cannot be read is rejected
    Given an unattended driver policy for a job workspace
    When a child's permission prompt carries no readable command
    Then the driver answers "reject"

  Scenario: A launched job pins its settings, answers prompts and records the parent's report
    Given a Coddy session fixture whose job turn asks to run "git status --short" and then "git push"
    When the operator launches the native job through the driver
    Then the session was pinned to the job model and permission mode before the job turn
    And the driver answered the prompts "allow" then "reject"
    And the recorded outcome is "parent-reported:SUCCESS"

  Scenario: A job turn that ends without the stream terminator is recorded as unknown
    Given a Coddy session fixture whose job turn stream ends without data: [DONE]
    When the operator launches the native job through the driver
    Then the recorded outcome is "unknown"

  Scenario: A finished child does not trigger the idle watchdog
    Given a Coddy session fixture whose job turn asks to run "git status --short" and then "git push"
    When the operator launches the native job through the driver
    Then no idle watchdog decision was recorded

  Scenario: An HTTP error is not a lost server, but an unreachable server is
    Given a Coddy session fixture whose job turn asks to run "git status --short" and then "git push"
    When the operator launches the native job through the driver
    And the server answers 404 for the session's background tasks
    Then the driver does not consider the server lost
    When the server becomes unreachable for repeated polls
    Then the driver considers the server lost

  Scenario: Attaching to a session the server does not know stops with a clear outcome
    Given a Coddy session fixture whose job turn asks to run "git status --short" and then "git push"
    When the operator attaches the driver to an unknown session
    Then the driver exits with status 3
    And the recorded outcome is "session-unknown"

  Scenario: The helper server's config cannot join a swarm, schedule work or open gateways
    Given a primary Coddy config with providers, a permission mode, swarm joins, the scheduler and two gateways enabled
    When the driver derives the isolated server config from it
    Then the isolated config keeps the providers and the permission mode
    And the isolated config disables the swarm, its joins, the scheduler and every gateway
    And the isolated config directory is private and the file is readable only by its owner

  Scenario: The primary Coddy config is found the way Coddy finds it
    Given the environment variables CODDY_CONFIG and CODDY_HOME
    Then the primary config is CODDY_CONFIG when it is set
    And it is CODDY_HOME/config.yaml when only CODDY_HOME is set
    And it is ~/.coddy/config.yaml otherwise
