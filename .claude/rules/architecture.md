# Architecture

`model.py` validates immutable public task input. `store.py` owns SQLite transactions, commands, task state, attempts, and events. `manager.py` reserves capacity and reconciles state. `worker.py` delivers assignments; `runner.py` alone executes an attempt and emits observations. `artifacts.py` validates and publishes accepted results. `providers.py` is an operator-configured HTTP boundary. `cli.py` and `api.py` are adapters over `Store`; they must not create an alternate state transition path.

Preserve these boundaries. A submit receipt means durable intent, not completed work. An ambiguous remote call remains `Unknown` and retains its reservation until independently resolved.
