"""Build the non-coordinating config used by the job-driver helper server."""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Mapping

import yaml


def primary_config_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Return the active primary config according to Coddy's config precedence."""
    env = os.environ if environ is None else environ
    if env.get("CODDY_CONFIG"):
        return Path(env["CODDY_CONFIG"]).expanduser()
    if env.get("CODDY_HOME"):
        return Path(env["CODDY_HOME"]).expanduser() / "config.yaml"
    return (Path.home() if home is None else Path(home)) / ".coddy" / "config.yaml"


def _mapping(config: dict, key: str) -> dict:
    value = config.setdefault(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping")
    return value


def _disable_gateways(config: dict) -> None:
    gateways = config.get("gateways")
    if gateways is None:
        return
    if isinstance(gateways, dict):
        entries = gateways.values()
    elif isinstance(gateways, list):
        entries = gateways
    else:
        raise ValueError("gateways must be a mapping or list")
    for gateway in entries:
        if isinstance(gateway, dict):
            gateway["enable"] = False


def write_isolated_config(source: Path, destination: Path) -> None:
    """Derive and atomically install a config that cannot join or schedule."""
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    config = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")

    swarm = _mapping(config, "swarm")
    swarm["enable"] = False
    swarm["join"] = []
    _mapping(config, "scheduler")["enable"] = False
    _disable_gateways(config)

    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, sort_keys=False, default_flow_style=False)
        os.replace(temporary, destination)
        destination.chmod(0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
