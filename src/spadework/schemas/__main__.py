"""Regenerate the JSON Schemas: ``uv run python -m spadework.schemas``."""

from spadework.schemas import write

for path in write():
    print(f"wrote {path.name}")
