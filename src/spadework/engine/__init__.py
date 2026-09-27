"""The engine: turns a project and its playbook into a pack.

See docs/spec/04-engine.md. The modules, from the bottom up:

- ``prompts``: the base prompts, ``ENGINE_PROMPT_VERSION``, and the prompt
  of each kind of session
- ``planning``: task states from the keys and ``state.json``, and the plan
- ``session``: one session through the executor, with its staging, limits
  and retries
- ``store``: records and outputs under ``.spadework/``
- ``verification``: the pluggable source verifier, and the network one
- ``documents``: the pack's documents, and what is rendered from them
- ``pack``: rendering ``out/``
- ``review``: the review rounds (critic, fixes, repairs) and the summary
- ``run``: the pipeline, the lock and the manifest
- ``reverify``: ``spadework verify``
- ``events`` and ``display``: progress, as a live view, lines or NDJSON

Importing this package imports none of them.
"""
