"""Atomic result publication, bounded reads and content identity checks."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError
from referencing.exceptions import Unresolvable

from .model import Gone, Invalid, canonical, digest, strict_json


@dataclass(frozen=True)
class ResultVerdict:
    """A result validation bound to the execution and contract it inspected."""

    attempt_id: str
    result_path: str | None
    result_sha256: str | None
    spec_sha256: str
    desired_action: str
    result: object = None
    error: str | None = None


def _artifact_data(store, attempt, spec):
    expected = f"artifacts/{attempt['id']}.json"
    if attempt["result_path"] != expected:
        raise Invalid("Missing or unexpected artifact path")
    path = store.home / expected
    if path.is_symlink() or not path.resolve().is_relative_to((store.home / "artifacts").resolve()):
        raise Invalid("Artifact escapes the artifact directory")
    try:
        with path.open("rb") as file:
            data = file.read(spec["budget"]["max_output_bytes"] + 1)
    except FileNotFoundError as exc:
        raise Gone("Result artifact has expired") from exc
    except OSError as exc:
        raise Invalid(f"Result validation failed: {exc}") from exc
    if len(data) > spec["budget"]["max_output_bytes"]:
        raise Invalid("Result exceeds max_output_bytes")
    return data


def publish(store, attempt_id, value, limit):
    data = canonical(value).encode()
    if len(data) > limit:
        raise Invalid("Result exceeds max_output_bytes")
    relative = f"artifacts/{attempt_id}.json"
    path = store.home / relative
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return relative, hashlib.sha256(data).hexdigest()


def artifact_checksum(store, attempt, spec):
    """Read only the bounded artifact bytes and return their current checksum."""
    return hashlib.sha256(_artifact_data(store, attempt, spec)).hexdigest()


def read_result(store, attempt, spec, validate=True):
    try:
        data = _artifact_data(store, attempt, spec)
        if hashlib.sha256(data).hexdigest() != attempt["result_sha256"]:
            raise Invalid("Artifact checksum mismatch")
        result = strict_json(data.decode())
        if validate:
            Draft202012Validator(spec["output_schema"]).validate(result)
        return result
    except (OSError, UnicodeError, ValidationError, Unresolvable, RecursionError) as exc:
        message = exc.message if isinstance(exc, ValidationError) else str(exc)
        raise Invalid(f"Result validation failed: {message}") from exc


def prevalidate_result(store, attempt, spec, desired_action="Run"):
    """Validate outside SQLite and return the evidence needed for settlement."""
    result = read_result(store, attempt, spec)
    return ResultVerdict(attempt_id=attempt["id"], result_path=attempt["result_path"],
                         result_sha256=attempt["result_sha256"], spec_sha256=digest(spec),
                         desired_action=desired_action, result=result)
