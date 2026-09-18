"""Fail closed on incomplete acceptance runs, including filtered or skipped scenarios."""
from collections import Counter
import json
from pathlib import Path

from behave.parser import parse_file


def inventory(root):
    root = Path(root)
    expected, tags = Counter(), set()
    for path in sorted((root / "features").glob("*.feature")):
        feature = parse_file(str(path))
        scenarios = list(feature.walk_scenarios())
        if not scenarios:
            raise ValueError(f"Feature has no executable scenarios: {path.name}")
        for scenario in scenarios:
            inherited = set(feature.tags) | set(scenario.effective_tags)
            if not any(f"R{i}" in inherited for i in range(1, 9)) or not any(f"ch{i:02}" in inherited for i in range(1, 14)):
                raise ValueError(f"Missing requirement/chapter trace: {path.name}: {scenario.name}")
            tags.update(inherited)
            expected[(path.name, scenario.name)] += 1
    required = {f"R{i}" for i in range(1, 9)} | {f"ch{i:02}" for i in range(1, 14)}
    if not expected or not required <= tags:
        raise ValueError(f"Incomplete feature inventory; missing tags: {sorted(required - tags)}")
    return expected


def validate_report(report, expected):
    observed = Counter()
    if not isinstance(report, list) or not report:
        raise ValueError("Acceptance report is empty or malformed")
    for feature in report:
        if feature.get("status") != "passed":
            raise ValueError(f"Feature did not pass: {feature.get('name')}")
        filename = Path(feature["location"].rsplit(":", 1)[0]).name
        for element in feature.get("elements", []):
            if element.get("type") != "scenario":
                continue
            if element.get("status") != "passed" or not element.get("steps"):
                raise ValueError(f"Scenario was skipped, empty or unsuccessful: {element.get('name')}")
            for step in element["steps"]:
                if step.get("result", {}).get("status") != "passed":
                    raise ValueError(f"Step was not executed successfully: {step.get('name')}")
            observed[(filename, element["name"])] += 1
    if observed != expected:
        raise ValueError(f"Incomplete acceptance run; missing={list((expected-observed).elements())}, extra={list((observed-expected).elements())}")
    return sum(observed.values())


def check(root, path):
    return validate_report(json.loads(Path(path).read_text()), inventory(root))
