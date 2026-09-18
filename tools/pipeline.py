"""One build path: complete Gherkin acceptance -> regression tests -> package -> smoke.

Run from any directory: python /path/to/project/tools/pipeline.py
No package is built when acceptance is failed, undefined, skipped, pending or filtered.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

# Direct script invocation and `python -m tools.pipeline` use the same imports.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.check_bdd import check


def command(stage, args, root, reports):
    print(f"[{stage}] {' '.join(str(a) for a in args)}", flush=True)
    result = subprocess.run(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
    (reports / f"{stage}.log").write_text(result.stdout)
    print(result.stdout, end="", flush=True)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args)


def run(root=ROOT, check_only=False, runner=command):
    root = Path(root).resolve()
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    output = root / "dist" / "verified"
    # These are exclusively generated directories. Old green reports/packages must not
    # masquerade as evidence of a new failed build.
    shutil.rmtree(output, ignore_errors=True)
    shutil.rmtree(reports / "behave-junit", ignore_errors=True)
    for filename in ("behave.json", "acceptance.log", "regression.log", "package.log", "wheel-smoke.log", "pipeline.json"):
        (reports / filename).unlink(missing_ok=True)
    completed = []
    stage = "acceptance"
    start = time.time()
    try:
        runner(stage, [sys.executable, "-m", "behave", "features", "--format", "json", "--outfile", str(reports / "behave.json"),
                       "--junit", "--junit-directory", str(reports / "behave-junit")], root, reports)
        count = check(root, reports / "behave.json")
        completed.append(stage)
        print(f"Acceptance gate: all {count} scenarios passed with full notes coverage.", flush=True)
        stage = "regression"
        runner(stage, [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"], root, reports)
        completed.append(stage)
        if not check_only:
            stage = "package"
            runner(stage, [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)], root, reports)
            completed.append(stage)
            wheels = list(output.glob("*.whl"))
            if len(wheels) != 1:
                raise ValueError("Expected exactly one wheel from this build")
            stage = "wheel-smoke"
            runner(stage, [sys.executable, str(root / "tools" / "smoke_wheel.py"), str(wheels[0])], root, reports)
            completed.append(stage)
    except Exception as exc:
        # Failed/partial package output is not eligible for artifact publication.
        shutil.rmtree(output, ignore_errors=True)
        summary = {"status": "failed", "failed_stage": stage, "completed": completed,
                   "error": str(exc), "elapsed_seconds": round(time.time() - start, 3)}
        (reports / "pipeline.json").write_text(json.dumps(summary, indent=2) + "\n")
        raise
    summary = {"status": "passed", "scenarios": count, "completed": completed,
               "elapsed_seconds": round(time.time() - start, 3)}
    (reports / "pipeline.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="Run acceptance then regression; do not package")
    args = parser.parse_args()
    try:
        print(json.dumps(run(check_only=args.check_only), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Pipeline stopped: {exc}. See reports/ for diagnostics.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
