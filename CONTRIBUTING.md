# Behavior-first changes

Use the executable features and [architecture contract](docs/architecture.md) as the acceptance map. Requirements precede implementation changes.

1. Identify the requirement and chapter tags. Write or revise an observable Given/When/Then example.
2. Run the example. For a defect, preserve its meaningful failure before the fix; for existing behavior, record the passing characterization.
3. Correct the responsible production layer and add a focused regression where needed. Do not change expected behavior merely to accommodate a defect.
4. Keep steps reusable and call production interfaces. Share infrastructure fixtures, not unittest test methods. Isolate stores/workspaces and use demo/loopback providers instead of paid models.
5. Run `python tools/pipeline.py` in the development environment. Acceptance must pass before regressions, packaging and installed-wheel verification. Failed, undefined, skipped or filtered scenarios cannot be waived for a release build.
6. Update the feature and operating documentation when a contract changes. Put unsupported capabilities in the backlog rather than skipped release scenarios.

Install tools with `python -m pip install -e '.[dev]'`. Linux and localhost socket access are required. The build pipeline writes feature and regression evidence to `reports/`.
