"""Permission policy for Coddy prompts raised during an unattended native job.

Fail closed: a prompt is allowed only when its command or edit can be read and
matches the allowlist. Everything else, including prompts whose arguments cannot
be parsed, is rejected with a reason.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

READ_ONLY_GIT = {"status", "diff", "log", "rev-parse", "show", "branch", "ls-files", "worktree"}
READ_ONLY_PROGRAMS = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "sha256sum", "echo", "pwd",
    "sort", "uniq", "diff", "stat", "file", "tree", "test", "true", "cut", "tr", "basename", "dirname",
}
PROJECT_PYTHON_ARGS = re.compile(r"^(scripts/check_[a-z_]+\.py|scripts/validate_[a-z_]+\.py|-m (behave|unittest)\b)")
OPERATOR_CHARS = set(";&|<>()")
EDIT_TOOLS = ("write", "edit", "patch", "create", "replace", "move")


class Policy:
    def __init__(self, workspace: str | Path, python: str):
        self.workspace = Path(workspace).resolve()
        self.python = python

    def inside(self, path: str | Path) -> bool:
        return (self.workspace / Path(path).expanduser()).resolve().is_relative_to(self.workspace)

    # ---------- commands ----------
    def split(self, command: str) -> list[list[str]] | str:
        """Split on unquoted operators; return a rejection reason instead when unsafe."""
        cleaned = command.replace("2>/dev/null", "").replace("2>&1", "")
        if "`" in cleaned or "$(" in cleaned:
            return "command substitution"
        try:
            lexer = shlex.shlex(cleaned, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            lexer.whitespace = " \t\r"
            tokens = list(lexer)
        except ValueError:
            return "unparsable command"
        segments, current = [], []
        for token in tokens:
            if token in {"&&", "||", ";", "|"}:
                if current:
                    segments.append(current)
                current = []
            elif token and set(token) <= OPERATOR_CHARS:
                return f"operator {token!r} (redirection, background or subshell)"
            else:
                # A newline always ends a command, even inside quotes: over-splitting only rejects more.
                parts = token.split("\n")
                if parts[0]:
                    current.append(parts[0])
                for part in parts[1:]:
                    if current:
                        segments.append(current)
                    current = [part] if part else []
        if current:
            segments.append(current)
        return segments or "empty command"

    def command_allowed(self, command: str) -> tuple[bool, str]:
        segments = self.split(command)
        if isinstance(segments, str):
            return False, segments
        for words in segments:
            program = words[0]
            if program == "cd":
                if len(words) < 2 or not self.inside(words[1]):
                    return False, "cd outside workspace"
            elif program == "git":
                args = words[3:] if words[1:2] == ["-C"] else words[1:]
                if words[1:2] == ["-C"] and (len(words) < 3 or not self.inside(words[2])):
                    return False, "git -C outside workspace"
                sub = [w for w in args if not w.startswith("-")]
                if not sub or sub[0] not in READ_ONLY_GIT \
                        or (sub[0] == "worktree" and sub[1:2] != ["list"]) \
                        or (sub[0] == "branch" and len(sub) > 1):
                    return False, f"git {sub[:1]} is not read-only"
            elif program == "sed":
                if any(w.startswith("-i") or w == "--in-place" for w in words) \
                        or any(re.search(r"(^|;)\s*[wW]\s|/w\s", w) for w in words[1:]):
                    return False, "sed that writes files"
            elif program in (self.python, ".venv/bin/python"):
                rest = " ".join(words[1:])
                if rest.startswith("-c"):
                    return False, "inline python is not allowed"
                if not PROJECT_PYTHON_ARGS.match(rest):
                    return False, f"python {rest[:40]} is not a project check"
                if "--out" in words or "--write" in words:
                    return False, "check with write flag"
            elif program in ("python", "python3"):
                return False, "only the project venv may run project checks"
            elif program in READ_ONLY_PROGRAMS:
                if program == "find" and any(w in words for w in ("-delete", "-exec", "-execdir", "-fprint")):
                    return False, "find that executes or writes"
            else:
                return False, f"program {program} not on the allowlist"
        return True, "allowlisted"

    # ---------- prompts ----------
    @staticmethod
    def tool_arguments(call: dict) -> dict:
        raw = call.get("args") or call.get("arguments") or call.get("rawInput") or call.get("input")
        if not raw:
            # Coddy permission prompts carry the call as "Arguments: {json}" text content.
            for part in call.get("content") or []:
                inner = part.get("content") if isinstance(part, dict) else None
                text = inner.get("text") if isinstance(inner, dict) else None
                if isinstance(text, str) and text.startswith("Arguments:"):
                    raw = text.partition(":")[2].strip()
                    break
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return {}
        return raw if isinstance(raw, dict) else {}

    def decide(self, request: dict) -> tuple[str, str]:
        call = request.get("toolCall") or request
        tool = (call.get("kind") or call.get("name") or call.get("toolName") or call.get("title") or "").lower()
        args = self.tool_arguments(call)
        if "run_command" in tool or "command" in args:
            command = str(args.get("command") or "").strip()
            if not command:
                return "reject", "command could not be read from the prompt"
            ok, why = self.command_allowed(command)
            return ("allow" if ok else "reject"), why
        if "delete" in tool or "remove" in tool:
            return "reject", "deletes are not auto-approved"
        paths = [str(v) for k, v in args.items() if k in {"path", "file_path", "file", "target", "destination"}]
        if paths and any(k in tool for k in EDIT_TOOLS):
            if all(self.inside(p) for p in paths):
                return "allow", "edit inside workspace"
            return "reject", "edit outside workspace"
        return "reject", f"tool {tool or '?'} not covered by the policy"
