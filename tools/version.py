"""Update and verify the one package version used by releases.

Usage:
  python tools/version.py check v0.1.0
  python tools/version.py set 0.1.1
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")


def version_from(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    if not match:
        raise ValueError(f"Could not find version in {path.relative_to(ROOT)}")
    return match.group(1)


def current() -> tuple[str, str]:
    project = version_from(ROOT / "pyproject.toml", r'^version\s*=\s*"([^"]+)"\s*$')
    package = version_from(ROOT / "boring_agent" / "__init__.py", r'^__version__\s*=\s*"([^"]+)"\s*$')
    return project, package


def check(tag: str | None = None) -> str:
    project, package = current()
    if not VERSION.fullmatch(project):
        raise ValueError(f"Version is not valid SemVer: {project}")
    if project != package:
        raise ValueError(f"Version mismatch: pyproject={project}, package={package}")
    if tag and tag != "v" + project:
        raise ValueError(f"Release tag {tag!r} must equal v{project}")
    return project


def set_version(value: str) -> str:
    if not VERSION.fullmatch(value):
        raise ValueError("Version must be SemVer, for example 0.1.1")
    paths = ((ROOT / "pyproject.toml", r'^(version\s*=\s*")[^"]+("\s*)$', r"\g<1>" + value + r"\g<2>"),
             (ROOT / "boring_agent" / "__init__.py", r'^(__version__\s*=\s*")[^"]+("\s*)$', r"\g<1>" + value + r"\g<2>"))
    for path, pattern, replacement in paths:
        old = path.read_text(encoding="utf-8")
        new, count = re.subn(pattern, replacement, old, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"Could not update exactly one version in {path.relative_to(ROOT)}")
        path.write_text(new, encoding="utf-8")
    return check()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("check", help="Verify metadata and optional vX.Y.Z tag")
    verify.add_argument("tag", nargs="?")
    update = sub.add_parser("set", help="Set the package SemVer in both metadata files")
    update.add_argument("version")
    args = parser.parse_args(argv)
    try:
        value = check(args.tag) if args.command == "check" else set_version(args.version)
    except ValueError as exc:
        print(f"Version check failed: {exc}", file=sys.stderr)
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
