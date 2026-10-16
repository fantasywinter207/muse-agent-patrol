# Scheduling component extension boundary

The scheduler policy and SQLite state owner live under `../policy` and are exported via `../index.py`. Add future policies as peer internal modules. Do not import other owners' private implementations; use the published SQLite schema as shared transaction contract. No host-side timer or model executor is part of this component.
