"""Session sandboxing: staging directories, guard policies and the guard.

See docs/spec/07-security.md.
"""

# Keep this module free of imports. The guard runs as
# `python -m spadework.sandbox.guard` on every tool call, which imports this
# package first, and the guard must stay fast and standard-library only.
