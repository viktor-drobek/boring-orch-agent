"""Install the built wheel without network and run it outside the source checkout."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def smoke(wheel):
    with tempfile.TemporaryDirectory(prefix="boa-wheel-") as directory:
        root = Path(directory)
        installed = root / "installed"
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-index", "--no-deps", "--target", str(installed), str(Path(wheel).resolve())],
                       check=True, timeout=120)
        # -I ignores inherited PYTHONPATH; explicit package-origin verification stops an
        # editable checkout from accidentally satisfying the packaging smoke test.
        script = "\n".join([
            "import sys", "from pathlib import Path", "sys.path.insert(0, sys.argv[1])",
            "import boring_agent", "assert Path(boring_agent.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())",
            "from boring_agent.cli import main", "raise SystemExit(main(['--home', sys.argv[2], 'demo']))",
        ])
        result = subprocess.run([sys.executable, "-I", "-c", script, str(installed), str(root / "state")],
                                cwd=root, capture_output=True, text=True, timeout=60, check=True)
        payload = json.loads(result.stdout)
        if payload.get("status") != "Succeeded" or payload.get("attempts") != 2:
            raise ValueError(f"Packaged demo did not complete the expected retry: {payload}")
        print(json.dumps({"wheel": Path(wheel).name, "smoke_test": payload}, indent=2))


if __name__ == "__main__":
    smoke(sys.argv[1])
