"""ACP runtime contracts that are enforceable without pretending to be a sandbox.

This module is deliberately a policy and evidence boundary.  It prepares an ACP
launch, validates callbacks, negotiates advertised capabilities and records
cancellation/usage evidence.  It does not start an agent or route native jobs
through the legacy manager/worker runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import signal
import time
from typing import Any, Callable, Mapping, Sequence

from .model import Invalid, canonical


ISOLATION_AVAILABLE = "available"
ISOLATION_UNAVAILABLE = "unavailable"
ISOLATION_UNKNOWN = "unknown"
TERMINAL_STOP_REASONS = frozenset({
    "cancelled", "canceled", "stopped", "terminated", "killed", "timeout", "exit",
})


class ACPError(Invalid):
    """A request cannot be executed under the ACP contract."""


class IsolationError(ACPError):
    """The requested isolation guarantee cannot be enforced."""


@dataclass(frozen=True)
class IsolationDecision:
    capability: str
    tier: str
    allowed: bool
    contract: str
    reason: str
    command: tuple[str, ...] = ()
    state_path: str | None = None
    network_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "tier": self.tier,
            "allowed": self.allowed,
            "contract": self.contract,
            "reason": self.reason,
            "command": list(self.command),
            "state_path": self.state_path,
            "network_enabled": self.network_enabled,
        }


class IsolationPlanner:
    """Evaluate and construct a Linux bubblewrap launch without starting it.

    Tier A is the only protected contract.  If bubblewrap is unavailable or its
    capability is unknown, read-only work fails closed.  A writable request may
    be represented as Tier B only with the explicit trusted-operator contract;
    it must never be described as isolated.
    """

    def __init__(self, capability: str | None = None, which: Callable[[str], str | None] = shutil.which):
        self._which = which
        self.capability = capability if capability is not None else self._detect()

    def _detect(self) -> str:
        if os.name != "posix" or not Path("/proc").is_dir():
            return ISOLATION_UNAVAILABLE
        try:
            return ISOLATION_AVAILABLE if self._which("bwrap") else ISOLATION_UNAVAILABLE
        except (OSError, TypeError):
            return ISOLATION_UNKNOWN

    @staticmethod
    def _sandbox(task: Mapping[str, Any]) -> str:
        sandbox = task.get("sandbox", "read-only")
        if sandbox not in ("read-only", "workspace-write"):
            raise ACPError("sandbox must be read-only or workspace-write")
        return sandbox

    def evaluate(self, task: Mapping[str, Any]) -> IsolationDecision:
        sandbox = self._sandbox(task)
        network = bool(task.get("allow_network", task.get("network", False)))
        workspace = task.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            raise ACPError("ACP workspace is required")
        state_path = task.get("state_path", task.get("agent_state_dir"))
        if state_path is not None and (not isinstance(state_path, str) or not state_path):
            raise ACPError("ACP state_path must be a nonempty path")
        if self.capability == ISOLATION_AVAILABLE:
            return IsolationDecision(
                capability=self.capability, tier="A", allowed=True,
                contract="enforced_os_isolation",
                reason="bubblewrap can enforce workspace, state, temporary and network boundaries",
                state_path=state_path, network_enabled=network,
            )
        if self.capability not in (ISOLATION_UNAVAILABLE, ISOLATION_UNKNOWN):
            raise ACPError("unknown isolation capability value")
        if sandbox == "read-only":
            detail = "unavailable" if self.capability == ISOLATION_UNAVAILABLE else "unknown"
            raise IsolationError(
                f"ACP read-only task refused: enforceable isolation is {detail}; "
                "the weakened trusted-operator contract cannot protect this task"
            )
        return IsolationDecision(
            capability=self.capability, tier="B", allowed=True,
            contract="trusted_operator",
            reason="OS isolation is not enforceable; agent is trusted as the operator",
            state_path=state_path, network_enabled=network,
        )

    def command(self, agent_command: Sequence[str], task: Mapping[str, Any],
                decision: IsolationDecision | None = None) -> list[str]:
        if not isinstance(agent_command, Sequence) or isinstance(agent_command, (str, bytes)) or \
                not agent_command or any(not isinstance(item, str) for item in agent_command):
            raise ACPError("ACP agent command must be a nonempty sequence of strings")
        decision = decision or self.evaluate(task)
        if not decision.allowed:
            raise IsolationError(decision.reason)
        if decision.tier != "A":
            return list(agent_command)
        workspace = str(Path(task["workspace"]).resolve())
        state_path = task.get("state_path", task.get("agent_state_dir"))
        if not isinstance(state_path, str) or not state_path:
            raise ACPError("Tier A requires a private state_path")
        state_path = str(Path(state_path).resolve())
        command = [self._which("bwrap") or "bwrap", "--die-with-parent", "--new-session",
                   "--unshare-pid", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                   "--tmpfs", "/run", "--tmpfs", "/home"]
        if not decision.network_enabled:
            command.append("--unshare-net")
        # A tmpfs root is important: merely omitting a bind does not hide an
        # absolute store path because bubblewrap otherwise exposes the host root.
        command += ["--tmpfs", "/", "--ro-bind", workspace, workspace]
        if task.get("sandbox", "read-only") == "workspace-write":
            command[-2:] = ["--bind", workspace, workspace]
        command += ["--bind", state_path, "/.acp-state"]
        for system_path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"):
            if Path(system_path).exists():
                command += ["--ro-bind", system_path, system_path]
        command += ["--chdir", workspace, "--"] + list(agent_command)
        return command


@dataclass(frozen=True)
class AgentSecurity:
    permission_mode: str
    home: str
    restrictions: Mapping[str, bool]

    def as_dict(self) -> dict[str, Any]:
        return {"permission_mode": self.permission_mode, "home": self.home,
                "restrictions": dict(self.restrictions)}


def secure_agent(route: Mapping[str, Any], state_path: str) -> AgentSecurity:
    """Return an isolated ACP permission policy; no bypass mode is accepted."""
    if not isinstance(state_path, str) or not state_path:
        raise ACPError("isolated ACP home is required")
    mode = route.get("permission_mode", "default")
    if mode == "bypass" or route.get("bypass_permissions"):
        raise ACPError("ACP bypass permission mode is refused")
    restrictions = {
        "hooks": False,
        "mcp_servers": False,
        "subagents": False,
        "skills": False,
    }
    return AgentSecurity(mode, str(Path(state_path).resolve()), restrictions)


def secure_environment(base: Mapping[str, str] | None, security: AgentSecurity) -> dict[str, str]:
    """Build an environment whose home and extension discovery are private."""
    env = dict(base or os.environ)
    home = security.home
    env.update({
        "HOME": home,
        "XDG_CONFIG_HOME": home + "/config",
        "XDG_DATA_HOME": home + "/data",
        "XDG_CACHE_HOME": home + "/cache",
        "ACP_DISABLE_PROJECT_HOOKS": "1",
        "ACP_DISABLE_MCP_SERVERS": "1",
        "ACP_DISABLE_SUBAGENTS": "1",
        "ACP_DISABLE_SKILL_DISCOVERY": "1",
    })
    return env


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    unenforceable: tuple[str, ...] = ()
    approximate: tuple[str, ...] = ()
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "unenforceable": list(self.unenforceable),
                "approximate": list(self.approximate), "reason": self.reason}


@dataclass(frozen=True)
class BudgetCapabilities:
    wall_clock: bool = True
    process_group_kill: bool = True
    per_call: bool = False
    step_count: bool = False
    token_reporting: bool = False


def _weaker_opt_in(task: Mapping[str, Any]) -> bool:
    return bool(task.get("weaker_contract_opt_in", task.get("allow_weaker_contract", False))) or \
        task.get("budget_contract") == "weaker"


def evaluate_budgets(task: Mapping[str, Any], capabilities: BudgetCapabilities) -> BudgetDecision:
    budget = task.get("budget", {})
    if not isinstance(budget, Mapping):
        raise ACPError("ACP budget must be an object")
    requested = []
    if budget.get("max_steps") is not None:
        requested.append(("max_steps", capabilities.step_count))
    if budget.get("per_call_seconds", budget.get("request_seconds")) is not None:
        requested.append(("per_call_seconds", capabilities.per_call))
    if budget.get("max_tokens") is not None and not capabilities.token_reporting:
        requested.append(("max_tokens", False))
    if not capabilities.wall_clock and any(budget.get(k) is not None for k in ("deadline_seconds", "attempt_seconds")):
        requested.append(("wall_clock", False))
    unenforceable = tuple(name for name, enforceable in requested if not enforceable)
    approximate = ("max_tokens",) if budget.get("max_tokens") is not None and capabilities.token_reporting else ()
    if unenforceable and not _weaker_opt_in(task):
        return BudgetDecision(False, unenforceable, approximate,
                              "ACP adapter cannot enforce: " + ", ".join(unenforceable))
    reason = "weaker ACP budget contract accepted" if unenforceable else ""
    return BudgetDecision(True, unenforceable, approximate, reason)


@dataclass
class TokenAccounting:
    """Usage accounting for adapters that report cumulative opaque-turn totals."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reports: int = 0
    inconsistent: bool = False

    def record(self, usage: Mapping[str, Any] | int | None) -> int:
        if usage is None:
            return self.total_tokens
        if isinstance(usage, int):
            values = {"total_tokens": usage}
        elif isinstance(usage, Mapping):
            values = usage
        else:
            raise ACPError("ACP usage must be an object, integer or null")
        numbers = {}
        for name in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens"):
            value = values.get(name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ACPError(f"ACP usage {name} must be a nonnegative integer")
                numbers[name] = value
        total = numbers.get("total_tokens")
        if total is None and ("input_tokens" in numbers or "output_tokens" in numbers):
            total = numbers.get("input_tokens", numbers.get("prompt_tokens", 0)) + \
                    numbers.get("output_tokens", numbers.get("completion_tokens", 0))
        if total is not None and total < self.total_tokens:
            self.inconsistent = True
        self.input_tokens = max(self.input_tokens, numbers.get("input_tokens", numbers.get("prompt_tokens", 0)))
        self.output_tokens = max(self.output_tokens, numbers.get("output_tokens", numbers.get("completion_tokens", 0)))
        self.total_tokens = max(self.total_tokens, total or 0)
        self.reports += 1
        return self.total_tokens

    @property
    def cumulative_total(self) -> int:
        return self.total_tokens

    def as_dict(self) -> dict[str, Any]:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens, "reports": self.reports,
                "inconsistent": self.inconsistent, "accounting": "latest_cumulative_max"}


class WorkspaceCallback:
    """Map ACP absolute callback paths into the same safe workspace namespace."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ACPError("ACP workspace must be an existing directory")

    def relative(self, path: str) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ACPError("invalid_request: callback path is invalid")
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else self.workspace / candidate).resolve()
        try:
            relative = target.relative_to(self.workspace)
        except ValueError as exc:
            raise ACPError("invalid_request: callback path is outside the workspace") from exc
        if any(part.startswith(".") for part in relative.parts):
            raise ACPError("invalid_request: hidden callback path is not visible")
        return relative.as_posix() or "."

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping) or "path" not in request:
            raise ACPError("invalid_request: filesystem callback requires path")
        result = dict(request)
        result["path"] = self.relative(request["path"])
        return result


def map_callback_path(workspace: str | Path, path: str) -> str:
    return WorkspaceCallback(workspace).relative(path)


@dataclass
class ProgressRecorder:
    records: list[dict[str, Any]] = field(default_factory=list)

    def record_plan(self, notification: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(notification, Mapping):
            raise ACPError("ACP plan notification must be an object")
        evidence = {
            "kind": "progress",
            "notification": dict(notification),
            "children_created": 0,
            "workflow_specification": False,
        }
        self.records.append(evidence)
        return evidence

    def as_dict(self) -> dict[str, Any]:
        return {"records": list(self.records), "children_created": 0}


@dataclass(frozen=True)
class NegotiatedRoute:
    mode: str | None
    model: str | None
    advertised: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "model": self.model, "advertised": self.advertised}


def _advertised_values(advertisement: Mapping[str, Any], key: str) -> list[str]:
    values = advertisement.get(key, [])
    if isinstance(values, Mapping):
        values = list(values)
    if not isinstance(values, (list, tuple, set)) or any(not isinstance(item, str) for item in values):
        raise ACPError(f"ACP advertisement {key} must be a list of strings")
    return list(values)


def negotiate(advertisement: Mapping[str, Any], requested_mode: str | None = None,
              requested_model: str | None = None) -> NegotiatedRoute:
    if not isinstance(advertisement, Mapping):
        raise ACPError("ACP capability advertisement must be an object")
    modes = _advertised_values(advertisement, "modes")
    models = _advertised_values(advertisement, "models")
    if requested_mode is not None and requested_mode not in modes:
        raise ACPError(f"ACP mode is not advertised: {requested_mode}")
    if requested_model is not None and requested_model not in models:
        raise ACPError(f"ACP model is not advertised: {requested_model}")
    mode = requested_mode if requested_mode is not None else (advertisement.get("default_mode") or (modes[0] if modes else None))
    model = requested_model if requested_model is not None else (advertisement.get("default_model") or (models[0] if models else None))
    if mode is not None and mode not in modes:
        raise ACPError(f"ACP default mode is not advertised: {mode}")
    if model is not None and model not in models:
        raise ACPError(f"ACP default model is not advertised: {model}")
    return NegotiatedRoute(mode, model)


# Descriptive aliases make the policy boundary convenient for adapters without
# adding a second implementation or importing the legacy provider code.
IsolationPolicy = IsolationPlanner
CancellationEvidence = dict
BudgetPolicy = evaluate_budgets
ModeModelNegotiator = negotiate


@dataclass
class CancellationSupervisor:
    """Drive cooperative cancellation and retain capacity until evidence agrees."""
    process_group: int
    cooperative_cancel: Callable[[], Any]
    grace_seconds: float = 2.0
    now: Callable[[], float] = time.monotonic
    group_exists: Callable[[int], bool] | None = None
    send_signal: Callable[[int, int], Any] = os.killpg
    requested_at: float | None = None
    term_sent_at: float | None = None
    kill_sent_at: float | None = None
    adapter_stop_reason: str | None = None
    signals: list[int] = field(default_factory=list)

    def __post_init__(self):
        if self.process_group <= 0:
            raise ACPError("ACP process group must be positive")
        if self.grace_seconds <= 0:
            raise ACPError("ACP cancellation grace must be positive")
        if self.group_exists is None:
            self.group_exists = self._default_group_exists

    def _default_group_exists(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def request(self) -> dict[str, Any]:
        if self.requested_at is None:
            self.requested_at = self.now()
            self.cooperative_cancel()
        return self.poll()

    request_cancellation = request

    def adapter_stopped(self, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ACPError("ACP adapter stop reason is required")
        normalized = reason.strip().lower()
        self.adapter_stop_reason = normalized
        return self.poll()

    def _send(self, value: int):
        self.send_signal(self.process_group, value)
        self.signals.append(value)

    def poll(self) -> dict[str, Any]:
        if self.requested_at is None:
            return self.as_dict("Running")
        current = self.now()
        exists = bool(self.group_exists(self.process_group))
        if not exists and self.adapter_stop_reason in TERMINAL_STOP_REASONS:
            return self.as_dict("Cancelled", process_group_gone=True)
        if self.term_sent_at is None and current - self.requested_at >= self.grace_seconds:
            if exists:
                self._send(signal.SIGTERM)
            self.term_sent_at = current
        elif self.term_sent_at is not None and self.kill_sent_at is None and \
                current - self.term_sent_at >= self.grace_seconds:
            if exists:
                self._send(signal.SIGKILL)
            self.kill_sent_at = current
        return self.as_dict("Unknown", process_group_gone=not exists)

    tick = poll

    def as_dict(self, status: str | None = None, process_group_gone: bool | None = None) -> dict[str, Any]:
        gone = bool(process_group_gone) if process_group_gone is not None else bool(
            self.requested_at is not None and not self.group_exists(self.process_group))
        confirmed = status == "Cancelled" or (gone and self.adapter_stop_reason in TERMINAL_STOP_REASONS)
        return {
            "status": "Cancelled" if confirmed else (status or "Unknown"),
            "cooperative_requested": self.requested_at is not None,
            "process_group_gone": gone,
            "terminal_adapter_stop_reason": self.adapter_stop_reason,
            "confirmed_cessation": confirmed,
            "reserved": not confirmed,
            "signals": list(self.signals),
        }


@dataclass(frozen=True)
class LaunchPlan:
    isolation: IsolationDecision
    security: AgentSecurity
    budget: BudgetDecision
    negotiated: NegotiatedRoute
    command: tuple[str, ...]
    environment: Mapping[str, str]


def prepare_launch(task: Mapping[str, Any], agent_command: Sequence[str],
                   advertisement: Mapping[str, Any],
                   capability: str | None = None,
                   base_environment: Mapping[str, str] | None = None) -> LaunchPlan:
    planner = IsolationPlanner(capability)
    isolation = planner.evaluate(task)
    state_path = task.get("state_path", task.get("agent_state_dir"))
    security = secure_agent(task, state_path) if isinstance(state_path, str) else \
        (_raise("Tier A requires state_path") if isolation.tier == "A" else secure_agent(task, "/tmp/acp-state"))
    budget = evaluate_budgets(task, BudgetCapabilities(**task.get("budget_capabilities", {})))
    if not budget.allowed:
        raise ACPError(budget.reason)
    negotiated = negotiate(advertisement, task.get("mode"), task.get("model"))
    command = tuple(planner.command(agent_command, task, isolation))
    return LaunchPlan(isolation, security, budget, negotiated, command,
                      secure_environment(base_environment, security))


def _raise(message: str):
    raise ACPError(message)
