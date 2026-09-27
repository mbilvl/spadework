"""Table-driven guard decisions, one row per attack or legitimate call.

Row ids start with the invariant they test (docs/spec/07-security.md):
I1 tool allowlist, I2 read confinement, I3 one exact writable path, I4 the
control plane, I5 no web in writing sessions, T4 server-side request forgery
through WebFetch. In paths, <w> is the staging directory, <W> the same in
upper case, <c> the control directory, <root> the project and <outside> a
directory outside the project.
"""

from __future__ import annotations

import socket
import threading
import unicodedata
from typing import Any

import pytest

from spadework.sandbox import guard
from spadework.sandbox.guard import GuardError
from spadework.sandbox.policy import Policy
from tests.security.conftest import NOW, OUTPUT, Box, MakeBox

ALLOW = True
DENY = False

OUTSIDE = "inside the working directory"
NOT_AVAILABLE = "is not available in this session"
RELATIVE = "must be relative"
ONLY_ON = "is allowed only on"
PRIVATE = "cannot fetch local or private network addresses"
CONTROL = "contains a control character"

NFD_OUTPUT = unicodedata.normalize("NFD", OUTPUT)

# A draft session: Read, Glob, Grep, Write and Edit, one writable output.
WRITING: list[Any] = [
    # I1: only the kind's tools, never shell, notebook, subagent or MCP tools.
    pytest.param(
        "Bash", {"command": "cat /etc/passwd"}, DENY, NOT_AVAILABLE, id="I1-bash"
    ),
    pytest.param(
        "BashOutput", {"bash_id": "1"}, DENY, NOT_AVAILABLE, id="I1-bash-output"
    ),
    pytest.param(
        "KillShell", {"shell_id": "1"}, DENY, NOT_AVAILABLE, id="I1-kill-shell"
    ),
    pytest.param(
        "Task",
        {
            "description": "x",
            "prompt": "read ~/.ssh",
            "subagent_type": "general-purpose",
        },
        DENY,
        NOT_AVAILABLE,
        id="I1-task",
    ),
    pytest.param("Agent", {"prompt": "x"}, DENY, NOT_AVAILABLE, id="I1-agent"),
    pytest.param(
        "NotebookEdit",
        {"notebook_path": "<w>/n.ipynb", "new_source": "import os"},
        DENY,
        NOT_AVAILABLE,
        id="I1-notebook-edit",
    ),
    pytest.param("TodoWrite", {"todos": []}, DENY, NOT_AVAILABLE, id="I1-todo-write"),
    pytest.param("Skill", {"skill": "x"}, DENY, NOT_AVAILABLE, id="I1-skill"),
    pytest.param(
        "ToolSearch", {"query": "x"}, DENY, NOT_AVAILABLE, id="I1-tool-search"
    ),
    pytest.param("read", {"file_path": "brief.md"}, DENY, NOT_AVAILABLE, id="I1-case"),
    pytest.param(
        "mcp__github__create_issue",
        {"title": "x"},
        DENY,
        "MCP tools are not available",
        id="I1-mcp",
    ),
    pytest.param(
        "mcp__spadework__read", {}, DENY, "MCP tools are not available", id="I1-mcp-2"
    ),
    pytest.param("StructuredOutput", {"status": "ok"}, ALLOW, "", id="I1-structured"),
    # I5: writing sessions have no web tools.
    pytest.param(
        "WebFetch",
        {"url": "https://example.com/", "prompt": "x"},
        DENY,
        NOT_AVAILABLE,
        id="I5-web-fetch",
    ),
    pytest.param(
        "WebSearch", {"query": "acme"}, DENY, NOT_AVAILABLE, id="I5-web-search"
    ),
    # I2: reads stay in the staging directory.
    pytest.param("Read", {"file_path": "<w>/brief.md"}, ALLOW, "", id="I2-read"),
    pytest.param("Read", {"file_path": "brief.md"}, ALLOW, "", id="I2-read-relative"),
    pytest.param(
        "Read",
        {"file_path": "inputs/../brief.md"},
        ALLOW,
        "",
        id="I2-read-dotdot-inside",
    ),
    pytest.param("Read", {"file_path": "<w>"}, ALLOW, "", id="I2-read-workdir"),
    pytest.param(
        "Read",
        {"file_path": "<w>/inputs/notes.md", "offset": 1, "limit": 5, "pages": "1"},
        ALLOW,
        "",
        id="I2-read-options",
    ),
    pytest.param(
        "Read",
        {"file_path": "<outside>/secret.txt"},
        DENY,
        OUTSIDE,
        id="I2-read-outside",
    ),
    pytest.param("Read", {"file_path": "/etc/passwd"}, DENY, OUTSIDE, id="I2-read-etc"),
    pytest.param(
        "Read", {"file_path": "<root>/brief.md"}, DENY, OUTSIDE, id="I2-read-project"
    ),
    pytest.param(
        "Read",
        {"file_path": "../../../../outside/secret.txt"},
        DENY,
        OUTSIDE,
        id="I2-read-relative-dotdot",
    ),
    pytest.param(
        "Read",
        {"file_path": "<w>/../../../../outside/secret.txt"},
        DENY,
        OUTSIDE,
        id="I2-read-absolute-dotdot",
    ),
    pytest.param(
        "Read",
        {"file_path": "<w>/missing/../../../../../outside/secret.txt"},
        DENY,
        OUTSIDE,
        id="I2-read-dotdot-through-missing-dir",
    ),
    pytest.param(
        "Read", {"file_path": "<w>-evil/secret.txt"}, DENY, OUTSIDE, id="I2-read-prefix"
    ),
    pytest.param(
        "Read", {"file_path": "<W>/brief.md"}, DENY, OUTSIDE, id="I2-read-case"
    ),
    pytest.param(
        "Read", {"file_path": "link-file"}, DENY, OUTSIDE, id="I2-symlink-file"
    ),
    pytest.param(
        "Read",
        {"file_path": "<w>/link-dir/secret.txt"},
        DENY,
        OUTSIDE,
        id="I2-symlink-dir",
    ),
    pytest.param(
        "Read", {"file_path": "~/.ssh/id_rsa"}, DENY, "'~'", id="I2-read-home"
    ),
    pytest.param("Read", {"file_path": "~"}, DENY, "'~'", id="I2-read-tilde"),
    pytest.param(
        "Read", {"file_path": "<w>/brief.md\x00.png"}, DENY, CONTROL, id="I2-read-nul"
    ),
    pytest.param(
        "Read", {"file_path": ""}, DENY, "non-empty string", id="I2-read-empty"
    ),
    pytest.param(
        "Read",
        {"file_path": "brief\ud800.md"},
        DENY,
        "not a valid path",
        id="I2-surrogate",
    ),
    pytest.param(
        "Read", {"file_path": 7}, DENY, "non-empty string", id="I2-read-number"
    ),
    pytest.param(
        "Read", {}, DENY, "needs the argument 'file_path'", id="I2-read-missing"
    ),
    pytest.param(
        "Read",
        {"file_path": "brief.md", "path": "/etc"},
        DENY,
        "does not take the argument 'path'",
        id="I2-read-unknown-argument",
    ),
    pytest.param("Glob", {"pattern": "*.md"}, ALLOW, "", id="I2-glob"),
    pytest.param(
        "Glob",
        {"pattern": "**/*.md", "path": "<w>/inputs"},
        ALLOW,
        "",
        id="I2-glob-path",
    ),
    pytest.param("Glob", {"pattern": "*.{md,txt}"}, ALLOW, "", id="I2-glob-braces"),
    pytest.param(
        "Glob", {"pattern": "*.md", "path": None}, ALLOW, "", id="I2-glob-null"
    ),
    pytest.param("Glob", {"pattern": "/etc/*"}, DENY, RELATIVE, id="I2-glob-absolute"),
    pytest.param(
        "Glob", {"pattern": "<outside>/*.txt"}, DENY, RELATIVE, id="I2-glob-absolute-2"
    ),
    pytest.param(
        "Glob", {"pattern": "../../outside/*.txt"}, DENY, RELATIVE, id="I2-glob-dotdot"
    ),
    pytest.param(
        "Glob", {"pattern": "inputs/../../*"}, DENY, RELATIVE, id="I2-glob-dotdot-inner"
    ),
    pytest.param("Glob", {"pattern": "~/.ssh/*"}, DENY, RELATIVE, id="I2-glob-home"),
    pytest.param("Glob", {"pattern": "!/etc/*"}, DENY, RELATIVE, id="I2-glob-negated"),
    pytest.param(
        "Glob", {"pattern": "{/etc,x}/*"}, DENY, RELATIVE, id="I2-glob-brace-abs"
    ),
    pytest.param(
        "Glob", {"pattern": ".{.,x}/secret"}, DENY, RELATIVE, id="I2-glob-brace-dotdot"
    ),
    pytest.param(
        "Glob", {"pattern": "{..}/x"}, DENY, RELATIVE, id="I2-glob-brace-single"
    ),
    pytest.param("Glob", {"pattern": "{a,{b,c}}/*.md"}, ALLOW, "", id="I2-glob-nested"),
    pytest.param(
        "Glob", {"pattern": "{a,{..,c}}/x"}, DENY, RELATIVE, id="I2-glob-nested-dotdot"
    ),
    pytest.param("Glob", {"pattern": "*.md /etc/*"}, DENY, RELATIVE, id="I2-glob-two"),
    pytest.param(
        "Glob", {"pattern": "*.md,/etc/*"}, DENY, RELATIVE, id="I2-glob-comma"
    ),
    pytest.param(
        "Glob", {"pattern": "\\.\\./x"}, DENY, "backslashes", id="I2-glob-escaped-dots"
    ),
    pytest.param(
        "Glob", {"pattern": "{a,b"}, DENY, "unbalanced", id="I2-glob-unbalanced"
    ),
    pytest.param(
        "Glob",
        {"pattern": "{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}"},
        DENY,
        "too many braces",
        id="I2-glob-brace-bomb",
    ),
    pytest.param("Glob", {"pattern": ""}, DENY, "non-empty string", id="I2-glob-empty"),
    pytest.param(
        "Glob",
        {"pattern": "*.md", "path": "<outside>"},
        DENY,
        OUTSIDE,
        id="I2-glob-path-out",
    ),
    pytest.param(
        "Glob", {"pattern": "*", "path": "/"}, DENY, OUTSIDE, id="I2-glob-root"
    ),
    pytest.param(
        "Glob",
        {"pattern": "*", "path": "link-dir"},
        DENY,
        OUTSIDE,
        id="I2-glob-symlink-dir",
    ),
    pytest.param("Grep", {"pattern": "hello"}, ALLOW, "", id="I2-grep"),
    pytest.param(
        "Grep", {"pattern": "hello", "path": "<w>"}, ALLOW, "", id="I2-grep-path"
    ),
    pytest.param(
        "Grep",
        {"pattern": "/etc/passwd|\\.\\./"},
        ALLOW,
        "",
        id="I2-grep-regex-not-a-path",
    ),
    pytest.param(
        "Grep",
        {
            "pattern": "x",
            "glob": "*.{ts,tsx}",
            "output_mode": "content",
            "-i": True,
            "-n": True,
            "-C": 2,
            "head_limit": 5,
            "multiline": False,
            "type": "md",
        },
        ALLOW,
        "",
        id="I2-grep-options",
    ),
    pytest.param(
        "Grep", {"pattern": "x", "path": "/etc"}, DENY, OUTSIDE, id="I2-grep-path-out"
    ),
    pytest.param(
        "Grep",
        {"pattern": "x", "path": "../.."},
        DENY,
        OUTSIDE,
        id="I2-grep-path-dotdot",
    ),
    pytest.param(
        "Grep",
        {"pattern": "SECRET", "glob": "../../outside/*.txt", "output_mode": "content"},
        DENY,
        RELATIVE,
        id="I2-grep-glob-dotdot",
    ),
    pytest.param(
        "Grep",
        {"pattern": "x", "glob": "/etc/*"},
        DENY,
        RELATIVE,
        id="I2-grep-glob-abs",
    ),
    pytest.param(
        "Grep",
        {"pattern": "x", "paths": ["/etc"]},
        DENY,
        "does not take the argument 'paths'",
        id="I2-grep-unknown-argument",
    ),
    pytest.param(
        "Grep", {"pattern": 5}, DENY, "'pattern' as a string", id="I2-grep-type"
    ),
    # I3: Write and Edit only on the one writable path, matched exactly.
    pytest.param(
        "Write",
        {"file_path": f"<w>/{OUTPUT}", "content": "x"},
        ALLOW,
        "",
        id="I3-write",
    ),
    pytest.param(
        "Write",
        {"file_path": OUTPUT, "content": "x"},
        ALLOW,
        "",
        id="I3-write-relative",
    ),
    pytest.param(
        "Write",
        {"file_path": f"<w>/inputs/../{OUTPUT}", "content": "x"},
        ALLOW,
        "",
        id="I3-write-normalised",
    ),
    pytest.param(
        "Edit",
        {"file_path": f"<w>/{OUTPUT}", "old_string": "a", "new_string": "b"},
        ALLOW,
        "",
        id="I3-edit",
    ),
    pytest.param(
        "Edit",
        {
            "file_path": f"<w>/{OUTPUT}",
            "old_string": "a",
            "new_string": "b",
            "replace_all": True,
        },
        ALLOW,
        "",
        id="I3-edit-replace-all",
    ),
    pytest.param(
        "Write",
        {"file_path": "<w>/deps/01-scope.md", "content": "x"},
        DENY,
        ONLY_ON,
        id="I3-write-other-document",
    ),
    pytest.param(
        "Edit",
        {"file_path": "<w>/deps/01-scope.md", "old_string": "a", "new_string": "b"},
        DENY,
        ONLY_ON,
        id="I3-edit-other-document",
    ),
    pytest.param(
        "Write",
        {"file_path": "<w>/brief.md", "content": "x"},
        DENY,
        ONLY_ON,
        id="I3-write-staged-input",
    ),
    pytest.param(
        "Write",
        {"file_path": "<w>/new.md", "content": "x"},
        DENY,
        ONLY_ON,
        id="I3-new-file",
    ),
    pytest.param(
        "Write",
        {"file_path": f"<w>/{OUTPUT.upper()}", "content": "x"},
        DENY,
        ONLY_ON,
        id="I3-write-case-variant",
    ),
    pytest.param(
        "Write",
        {"file_path": f"<W>/{OUTPUT}", "content": "x"},
        DENY,
        OUTSIDE,
        id="I3-write-case-variant-dir",
    ),
    pytest.param(
        "Write",
        {"file_path": f"<w>/{NFD_OUTPUT}", "content": "x"},
        DENY,
        ONLY_ON,
        id="I3-write-unicode-nfd",
    ),
    pytest.param(
        "Write",
        {"file_path": f"<w>/{OUTPUT}\x00", "content": "x"},
        DENY,
        CONTROL,
        id="I3-write-nul",
    ),
    pytest.param(
        "Write",
        {"file_path": "<w>/link-file", "content": "x"},
        DENY,
        OUTSIDE,
        id="I3-write-symlink-file",
    ),
    pytest.param(
        "Write",
        {"file_path": "<w>/link-dir/secret.txt", "content": "x"},
        DENY,
        OUTSIDE,
        id="I3-write-symlink-dir",
    ),
    pytest.param(
        "Write",
        {"file_path": "/tmp/x.md", "content": "x"},
        DENY,
        OUTSIDE,
        id="I3-write-tmp",
    ),
    pytest.param(
        "Write",
        {"file_path": "~/.bashrc", "content": "x"},
        DENY,
        "'~'",
        id="I3-write-home",
    ),
    pytest.param(
        "Write",
        {"file_path": "<root>/brief.md", "content": "x"},
        DENY,
        OUTSIDE,
        id="I3-write-project",
    ),
    pytest.param(
        "Write",
        {"file_path": f"<w>/{OUTPUT}"},
        DENY,
        "needs the argument 'content'",
        id="I3-write-missing-content",
    ),
    pytest.param(
        "Edit",
        {"file_path": f"<w>/{OUTPUT}", "old_string": "a"},
        DENY,
        "needs the argument 'new_string'",
        id="I3-edit-missing-argument",
    ),
    # I4: the control plane can be neither read nor written.
    pytest.param(
        "Write",
        {"file_path": "<c>/001-scope-draft.policy.json", "content": "{}"},
        DENY,
        OUTSIDE,
        id="I4-write-policy",
    ),
    pytest.param(
        "Write",
        {"file_path": "<c>/001-scope-draft.settings.json", "content": "{}"},
        DENY,
        OUTSIDE,
        id="I4-write-settings",
    ),
    pytest.param(
        "Write",
        {"file_path": "<c>/001-scope-draft.counters.json", "content": "{}"},
        DENY,
        OUTSIDE,
        id="I4-write-counters",
    ),
    pytest.param(
        "Write",
        {"file_path": "<c>/001-scope-draft.denials.jsonl", "content": ""},
        DENY,
        OUTSIDE,
        id="I4-write-denials",
    ),
    pytest.param(
        "Edit",
        {
            "file_path": "<c>/001-scope-draft.policy.json",
            "old_string": "80",
            "new_string": "8000",
        },
        DENY,
        OUTSIDE,
        id="I4-edit-policy",
    ),
    pytest.param(
        "Write",
        {"file_path": "../../control/001-scope-draft.policy.json", "content": "{}"},
        DENY,
        OUTSIDE,
        id="I4-write-policy-relative",
    ),
    pytest.param(
        "Write",
        {"file_path": "link-control/001-scope-draft.policy.json", "content": "{}"},
        DENY,
        OUTSIDE,
        id="I4-write-policy-symlink",
    ),
    pytest.param(
        "Write",
        {"file_path": "<root>/.spadework/state.json", "content": "{}"},
        DENY,
        OUTSIDE,
        id="I4-write-state",
    ),
    pytest.param(
        "Read",
        {"file_path": "<c>/001-scope-draft.policy.json"},
        DENY,
        OUTSIDE,
        id="I4-read-policy",
    ),
    pytest.param(
        "Read",
        {"file_path": "<c>/001-scope-draft.settings.json"},
        DENY,
        OUTSIDE,
        id="I4-read-settings",
    ),
    pytest.param(
        "Read",
        {"file_path": "link-control/001-scope-draft.counters.json"},
        DENY,
        OUTSIDE,
        id="I4-read-counters-symlink",
    ),
    pytest.param("Glob", {"pattern": "*", "path": "<c>"}, DENY, OUTSIDE, id="I4-glob"),
    pytest.param(
        "Grep",
        {"pattern": "workdir", "path": "<root>/.spadework"},
        DENY,
        OUTSIDE,
        id="I4-grep",
    ),
]

# A research session: WebSearch and WebFetch only, an empty workdir.
RESEARCH: list[Any] = [
    pytest.param(
        "WebSearch", {"query": "stainless steel fabricators"}, ALLOW, "", id="search"
    ),
    pytest.param(
        "WebSearch",
        {"query": "catalogue sites", "allowed_domains": ["example.com"]},
        ALLOW,
        "",
        id="search-domains",
    ),
    pytest.param(
        "WebSearch", {"query": "  "}, DENY, "non-empty string", id="search-blank"
    ),
    pytest.param("WebSearch", {"query": 5}, DENY, "non-empty string", id="search-type"),
    pytest.param(
        "WebSearch", {}, DENY, "needs the argument 'query'", id="search-missing"
    ),
    pytest.param("StructuredOutput", {"notes": "x"}, ALLOW, "", id="I1-structured"),
    pytest.param("Read", {"file_path": "brief.md"}, DENY, NOT_AVAILABLE, id="I5-read"),
    pytest.param("Glob", {"pattern": "*"}, DENY, NOT_AVAILABLE, id="I5-glob"),
    pytest.param(
        "Write",
        {"file_path": "notes.md", "content": "x"},
        DENY,
        NOT_AVAILABLE,
        id="I5-write",
    ),
    pytest.param("Bash", {"command": "curl"}, DENY, NOT_AVAILABLE, id="I1-bash"),
    pytest.param("mcp__fetch__fetch", {"url": "x"}, DENY, "MCP", id="I1-mcp"),
]


def fetch(url: str, expected: bool, fragment: str, id: str) -> Any:
    return pytest.param(
        "WebFetch",
        {"url": url, "prompt": "Summarise the page"},
        expected,
        fragment,
        id=id,
    )


# T4: WebFetch can reach only public http(s) addresses.
RESEARCH += [
    fetch("https://example.com/", ALLOW, "", "T4-public"),
    fetch("http://example.com/a/b?q=1#top", ALLOW, "", "T4-public-path"),
    fetch("https://EXAMPLE.com/", ALLOW, "", "T4-public-upper-case"),
    fetch("https://example.com./", ALLOW, "", "T4-public-trailing-dot"),
    fetch("https://example.com:8443/", ALLOW, "", "T4-public-port"),
    fetch("https://v6.example.com/", ALLOW, "", "T4-public-v6-dns"),
    fetch("https://[2606:4700::1111]/", ALLOW, "", "T4-public-v6-literal"),
    fetch("https://8.8.8.8/", ALLOW, "", "T4-public-v4-literal"),
    fetch("https://[::ffff:8.8.8.8]/", ALLOW, "", "T4-public-v4-mapped"),
    fetch("http://localhost/", DENY, PRIVATE, "T4-localhost"),
    fetch("http://LOCALHOST:8080/", DENY, PRIVATE, "T4-localhost-upper-port"),
    fetch("http://localhost./", DENY, PRIVATE, "T4-localhost-dot"),
    fetch("http://api.localhost/", DENY, PRIVATE, "T4-localhost-subdomain"),
    fetch("http://127.0.0.1/", DENY, PRIVATE, "T4-loopback"),
    fetch("http://127.0.0.1:22/", DENY, PRIVATE, "T4-loopback-port"),
    fetch("http://127.1/", DENY, PRIVATE, "T4-short-ipv4"),
    fetch("http://127.0.1/", DENY, PRIVATE, "T4-short-ipv4-3"),
    fetch("http://2130706433/", DENY, PRIVATE, "T4-decimal-ipv4"),
    fetch("http://0x7f000001/", DENY, PRIVATE, "T4-hex-ipv4"),
    fetch("http://0x7F.0.0.1/", DENY, PRIVATE, "T4-hex-octet"),
    fetch("http://0177.0.0.1/", DENY, PRIVATE, "T4-octal-ipv4"),
    fetch("http://0/", DENY, PRIVATE, "T4-zero"),
    fetch("http://0x/", DENY, PRIVATE, "T4-hex-empty"),
    fetch(f"http://0x{'f' * 70}/", DENY, "not a valid IP", "T4-huge-number"),
    fetch(f"http://{'9' * 200}/", DENY, "not a valid IP", "T4-long-decimal"),
    fetch(f"http://{'a' * 250}.com/", DENY, "host name is not valid", "T4-long-host"),
    fetch("http://0.0.0.0/", DENY, PRIVATE, "T4-unspecified"),
    fetch("http://10.0.0.1/", DENY, PRIVATE, "T4-rfc1918-10"),
    fetch("http://172.16.5.4/", DENY, PRIVATE, "T4-rfc1918-172"),
    fetch("http://192.168.1.1/", DENY, PRIVATE, "T4-rfc1918-192"),
    fetch("http://169.254.169.254/latest/meta-data/", DENY, PRIVATE, "T4-metadata"),
    fetch("http://100.64.0.1/", DENY, PRIVATE, "T4-cgnat"),
    fetch("http://224.0.0.1/", DENY, PRIVATE, "T4-multicast"),
    fetch("http://255.255.255.255/", DENY, PRIVATE, "T4-broadcast"),
    fetch("http://240.0.0.1/", DENY, PRIVATE, "T4-reserved"),
    fetch("http://168.63.129.16/?comp=versions", DENY, PRIVATE, "T4-azure-wireserver"),
    fetch("http://intranet/", DENY, "full domain name", "T4-single-label"),
    fetch("http://intranet./", DENY, "full domain name", "T4-single-label-dot"),
    fetch("http://WIKI:8080/", DENY, "full domain name", "T4-single-label-port"),
    fetch("http://[::1]/", DENY, PRIVATE, "T4-v6-loopback"),
    fetch("http://[::]/", DENY, PRIVATE, "T4-v6-unspecified"),
    fetch("http://[::ffff:127.0.0.1]/", DENY, PRIVATE, "T4-v4-mapped-loopback"),
    fetch("http://[::ffff:7f00:1]/", DENY, PRIVATE, "T4-v4-mapped-hex"),
    fetch("http://[::ffff:169.254.169.254]/", DENY, PRIVATE, "T4-v4-mapped-metadata"),
    fetch("http://[::ffff:10.0.0.1]/", DENY, PRIVATE, "T4-v4-mapped-private"),
    fetch("http://[::7f00:1]/", DENY, PRIVATE, "T4-v4-compatible"),
    fetch("http://[64:ff9b::a00:1]/", DENY, PRIVATE, "T4-nat64-private"),
    fetch("http://[2002:7f00:1::]/", DENY, PRIVATE, "T4-6to4-loopback"),
    fetch("http://[fe80::1]/", DENY, PRIVATE, "T4-v6-link-local"),
    fetch("http://[fc00::1]/", DENY, PRIVATE, "T4-v6-unique-local"),
    fetch("http://[fec0::1]/", DENY, PRIVATE, "T4-v6-site-local"),
    fetch("http://[ff02::1]/", DENY, PRIVATE, "T4-v6-multicast"),
    fetch("http://[fe80::1%25en0]/", DENY, "percent", "T4-v6-zone"),
    fetch("http://internal.example.com/", DENY, PRIVATE, "T4-dns-private"),
    fetch("http://mixed.example.com/", DENY, PRIVATE, "T4-dns-one-private"),
    fetch("http://metadata.example.com/", DENY, PRIVATE, "T4-dns-metadata"),
    fetch("http://mapped.example.com/", DENY, PRIVATE, "T4-dns-v4-mapped"),
    fetch("http://scoped.example.com/", DENY, PRIVATE, "T4-dns-scoped-v6"),
    fetch("http://127.0.0.1.nip.io/", DENY, PRIVATE, "T4-dns-rebind-style"),
    fetch("http://garbage.example.com/", DENY, "did not resolve", "T4-dns-garbage"),
    fetch("http://unknown.example.com/", DENY, "could not be resolved", "T4-dns-fails"),
    fetch(
        "http://user:pass@example.com/", DENY, "user name or password", "T4-userinfo"
    ),
    fetch("http://user@example.com/", DENY, "user name or password", "T4-user"),
    fetch("http://example.com@127.0.0.1/", DENY, "user name or password", "T4-at-host"),
    fetch("http://example.com\\@127.0.0.1/", DENY, "backslashes", "T4-backslash"),
    fetch("file:///etc/passwd", DENY, "only http and https", "T4-file"),
    fetch("ftp://example.com/x", DENY, "only http and https", "T4-ftp"),
    fetch("data:text/html,<script>x</script>", DENY, "only http and https", "T4-data"),
    fetch("javascript:alert(1)", DENY, "only http and https", "T4-javascript"),
    fetch("gopher://example.com/", DENY, "only http and https", "T4-gopher"),
    fetch("https:example.com", DENY, "no host", "T4-no-slashes"),
    fetch("https:///127.0.0.1/", DENY, "no host", "T4-three-slashes"),
    fetch("http://exa mple.com/", DENY, "spaces", "T4-space"),
    fetch("http://example.com/\n", DENY, "control characters", "T4-newline"),
    fetch("http://%6c%6f%63%61%6c%68%6f%73%74/", DENY, "percent", "T4-percent-host"),
    fetch(
        "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/",
        DENY,
        "ASCII",
        "T4-fullwidth-host",
    ),
    fetch("http://1.2.3.4.5/", DENY, "not a valid IP", "T4-five-parts"),
    fetch("http://256.0.0.1/", DENY, "not a valid IP", "T4-octet-too-big"),
    fetch("http://1.2.3.08/", DENY, "not a valid IP", "T4-bad-octal"),
    fetch("http://example.123/", DENY, "not a valid IP", "T4-ends-in-number"),
    fetch("http://a..b.example.com/", DENY, "host name is not valid", "T4-empty-label"),
    fetch("http://a!b.example.com/", DENY, "host name is not valid", "T4-bad-char"),
    fetch("http://[127.0.0.1]/", DENY, "not valid", "T4-bracketed-ipv4"),
    fetch("http://[::1::2]/", DENY, "not valid", "T4-bad-ipv6"),
    fetch("http://example.com:99999/", DENY, "not valid", "T4-bad-port"),
    fetch("", DENY, "non-empty string", "T4-empty"),
    pytest.param(
        "WebFetch",
        {"url": 5, "prompt": "x"},
        DENY,
        "non-empty string",
        id="T4-url-type",
    ),
    pytest.param(
        "WebFetch",
        {"url": "https://example.com/"},
        DENY,
        "needs the argument 'prompt'",
        id="T4-missing-prompt",
    ),
    pytest.param(
        "WebFetch",
        {"url": "https://example.com/", "prompt": "x", "headers": {"A": "b"}},
        DENY,
        "does not take the argument 'headers'",
        id="T4-unknown-argument",
    ),
]


# Regressions from the adversarial review. R1: the CLI trims Glob and Grep
# paths (JS String.prototype.trim) and expands `~` after the hook has seen
# them, so " /etc" was read as relative by the guard and as /etc by the CLI.
NBSP, IDEOGRAPHIC_SPACE, BOM, NEL = chr(0xA0), chr(0x3000), chr(0xFEFF), chr(0x85)
WHITESPACE = "must not start or end with whitespace"

WRITING += [
    pytest.param(
        "Grep",
        {"pattern": "SECRET", "path": " /etc"},
        DENY,
        WHITESPACE,
        id="R1-grep-space",
    ),
    pytest.param(
        "Grep",
        {"pattern": "SECRET", "path": " <outside>", "output_mode": "content"},
        DENY,
        WHITESPACE,
        id="R1-grep-space-outside",
    ),
    pytest.param(
        "Grep", {"pattern": ".", "path": " ~/.aws"}, DENY, WHITESPACE, id="R1-aws"
    ),
    pytest.param(
        "Grep", {"pattern": ".", "path": "\t/etc"}, DENY, WHITESPACE, id="R1-tab"
    ),
    pytest.param(
        "Grep", {"pattern": ".", "path": "\n~"}, DENY, WHITESPACE, id="R1-newline"
    ),
    pytest.param(
        "Grep",
        {"pattern": ".", "path": IDEOGRAPHIC_SPACE + "/etc"},
        DENY,
        WHITESPACE,
        id="R1-ideographic-space",
    ),
    pytest.param(
        "Grep",
        {"pattern": ".", "path": BOM + "<outside>"},
        DENY,
        WHITESPACE,
        id="R1-bom",
    ),
    pytest.param(
        "Grep",
        {"pattern": ".", "path": "<outside> "},
        DENY,
        WHITESPACE,
        id="R1-trailing",
    ),
    pytest.param(
        "Glob", {"pattern": "*", "path": "\t/etc"}, DENY, WHITESPACE, id="R1-glob"
    ),
    pytest.param(
        "Glob", {"pattern": "**/*", "path": " ~"}, DENY, WHITESPACE, id="R1-home"
    ),
    pytest.param(
        "Glob", {"pattern": "*", "path": " <c>"}, DENY, WHITESPACE, id="R1-glob-control"
    ),
    pytest.param(
        "Glob", {"pattern": NBSP + "/etc/*"}, DENY, WHITESPACE, id="R1-pattern-nbsp"
    ),
    pytest.param(
        "Grep",
        {"pattern": ".", "glob": "*.md" + IDEOGRAPHIC_SPACE + "/etc/*"},
        DENY,
        RELATIVE,
        id="R1-glob-inner-ideographic-space",
    ),
    pytest.param(
        "Grep",
        {"pattern": ".", "glob": "*.md" + BOM + "/etc/*"},
        DENY,
        RELATIVE,
        id="R1-glob-inner-bom",
    ),
    pytest.param(
        "Read", {"file_path": "<w>/brief.md" + NEL}, DENY, CONTROL, id="R1-c1-control"
    ),
    pytest.param("Read", {"file_path": "brief\x7f.md"}, DENY, CONTROL, id="R1-delete"),
    pytest.param(
        "Grep", {"pattern": ".", "path": "inputs\x1b/x"}, DENY, CONTROL, id="R1-escape"
    ),
    pytest.param(
        "Read", {"file_path": "inputs/a b.md"}, ALLOW, "", id="R1-inner-space-ok"
    ),
]
# R2: the same for file_path, for callers that pass raw model input.
for _tool, _extra in [
    ("Read", {}),
    ("Write", {"content": "x"}),
    ("Edit", {"old_string": "a", "new_string": "b"}),
]:
    WRITING += [
        pytest.param(
            _tool,
            {"file_path": raw, **_extra},
            DENY,
            WHITESPACE,
            id=f"R2-{_tool.lower()}-{name}",
        )
        for raw, name in [
            (" ~/.ssh/id_rsa", "space-home"),
            (" /etc/passwd", "space-etc"),
            ("\n<outside>/secret.txt", "newline-outside"),
            (" " + OUTPUT, "space-output"),
        ]
    ]
# R4: size and nesting limits, so no argument can make the guard slow.
WRITING += [
    pytest.param(
        "Read", {"file_path": "x/" * 2048}, ALLOW, "", id="R4-path-4096-bytes"
    ),
    pytest.param(
        "Read",
        {"file_path": "x/" * 2048 + "y"},
        DENY,
        "longer than 4096",
        id="R4-path-4097",
    ),
    pytest.param(
        "Read",
        {"file_path": chr(0xE9) * 2049},
        DENY,
        "longer than 4096",
        id="R4-path-utf8",
    ),
    pytest.param(
        "Glob",
        {"pattern": "*" * 4097},
        DENY,
        "longer than 4096",
        id="R4-pattern-length",
    ),
    pytest.param(
        "Grep",
        {"pattern": "x", "glob": "*" * 4097},
        DENY,
        "longer than 4096",
        id="R4-glob",
    ),
    pytest.param(
        "Glob",
        {"pattern": "{" * 16 + "a" + "}" * 16},
        ALLOW,
        "",
        id="R4-braces-depth-16",
    ),
    pytest.param(
        "Glob",
        {"pattern": "{" * 17 + "a" + "}" * 17},
        DENY,
        "too deeply nested",
        id="R4-braces-depth-17",
    ),
    pytest.param(
        "Glob", {"pattern": "{a}" * 300}, DENY, "too many braces", id="R4-brace-steps"
    ),
]

# R1 for URLs, R5 the IANA special-purpose registries, and WHATWG ports.
RESEARCH += [
    fetch(NBSP + "https://example.com/", DENY, "control characters", "R1-url-nbsp"),
    fetch(
        "https://example.com/" + IDEOGRAPHIC_SPACE, DENY, "spaces", "R1-url-trailing"
    ),
    fetch("https://example.com/" + NEL + "x", DENY, "control characters", "R1-url-c1"),
    fetch(
        "https://example.com/" + "a" * 8200, DENY, "longer than 8192", "R4-url-length"
    ),
    fetch("https://example.com:+80/", DENY, "not valid", "port-plus"),
    fetch(
        "https://example.com:" + chr(0x668) + chr(0x660) + "/",
        DENY,
        "not valid",
        "port",
    ),
    fetch("https://example.com:0080/", ALLOW, "", "port-leading-zeros"),
    fetch("https://[2606:4700:4700::1111]:443/", ALLOW, "", "port-v6"),
    fetch("https://1.1.1.1/", ALLOW, "", "R5-public-v4"),
    fetch("https://[2606:4700:4700::1111]/", ALLOW, "", "R5-public-v6"),
    fetch("https://[64:ff9b::808:808]/", ALLOW, "", "R5-nat64-public"),
    fetch("http://0.1/", DENY, PRIVATE, "R5-this-network"),
    fetch("http://192.0.0.8/", DENY, PRIVATE, "R5-ietf-dummy"),
    fetch("http://192.0.0.9/", DENY, PRIVATE, "R5-pcp-anycast"),
    fetch("http://192.0.0.170/", DENY, PRIVATE, "R5-nat64-discovery"),
    fetch("http://192.0.2.1/", DENY, PRIVATE, "R5-test-net-1"),
    fetch("http://192.31.196.1/", DENY, PRIVATE, "R5-as112"),
    fetch("http://192.52.193.1/", DENY, PRIVATE, "R5-amt"),
    fetch("http://192.88.99.1/", DENY, PRIVATE, "R5-6to4-relay"),
    fetch("http://192.175.48.1/", DENY, PRIVATE, "R5-as112-delegation"),
    fetch("http://198.19.255.255/", DENY, PRIVATE, "R5-benchmarking"),
    fetch("http://198.51.100.1/", DENY, PRIVATE, "R5-test-net-2"),
    fetch("http://203.0.113.1/", DENY, PRIVATE, "R5-test-net-3"),
    fetch("http://239.255.255.250/", DENY, PRIVATE, "R5-multicast-ssdp"),
    fetch("http://[2001:1::1]/", DENY, PRIVATE, "R5-pcp-anycast-v6"),
    fetch("http://[2001:4:112::1]/", DENY, PRIVATE, "R5-as112-v6"),
    fetch("http://[2001:20::1]/", DENY, PRIVATE, "R5-orchid"),
    fetch("http://[2001:30::1]/", DENY, PRIVATE, "R5-drip"),
    fetch("http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/", DENY, PRIVATE, "R5-teredo"),
    fetch("http://[2001:db8::1]/", DENY, PRIVATE, "R5-documentation-v6"),
    fetch("http://[2002:808:808::]/", DENY, PRIVATE, "R5-6to4-public"),
    fetch("http://[2002:a9fe:a9fe::]/", DENY, PRIVATE, "R5-6to4-metadata"),
    fetch("http://[2620:4f:8000::1]/", DENY, PRIVATE, "R5-as112-delegation-v6"),
    fetch("http://[3fff::1]/", DENY, PRIVATE, "R5-documentation-3fff"),
    fetch("http://[5f00::1]/", DENY, PRIVATE, "R5-srv6"),
    fetch("http://[100::1]/", DENY, PRIVATE, "R5-discard"),
    fetch("http://[100:0:0:1::1]/", DENY, PRIVATE, "R5-dummy-prefix"),
    fetch("http://[64:ff9b:1::a00:1]/", DENY, PRIVATE, "R5-nat64-local"),
    fetch("http://[64:ff9b::127.0.0.1]/", DENY, PRIVATE, "R5-nat64-loopback"),
    fetch("http://[::ffff:0:127.0.0.1]/", DENY, PRIVATE, "R5-siit"),
    fetch("http://[0:0:0:0:0:ffff:127.0.0.1]/", DENY, PRIVATE, "R5-mapped-long-form"),
    fetch("http://[::ffff:100.64.0.1]/", DENY, PRIVATE, "R5-mapped-cgnat"),
    fetch("http://[fd00:ec2::254]/", DENY, PRIVATE, "R5-aws-metadata-v6"),
    fetch("http://[4000::1]/", DENY, PRIVATE, "R5-outside-global-unicast"),
]


def check(
    box: Box, tool: str, tool_input: dict[str, Any], allowed: bool, fragment: str
) -> None:
    decision = box.decide(tool, tool_input)
    assert decision.allow is allowed, decision.reason
    assert decision.reason.startswith("spadework: ")
    assert "\n" not in decision.reason
    assert fragment in decision.reason


@pytest.mark.parametrize(("tool", "tool_input", "allowed", "fragment"), WRITING)
def test_writing_session(
    writer: Box, tool: str, tool_input: dict[str, Any], allowed: bool, fragment: str
) -> None:
    check(writer, tool, tool_input, allowed, fragment)


@pytest.mark.parametrize(("tool", "tool_input", "allowed", "fragment"), RESEARCH)
def test_research_session(
    researcher: Box, tool: str, tool_input: dict[str, Any], allowed: bool, fragment: str
) -> None:
    check(researcher, tool, tool_input, allowed, fragment)


@pytest.mark.parametrize(
    ("kind", "tool", "tool_input", "allowed", "fragment"),
    [
        pytest.param(
            "critic", "Read", {"file_path": "brief.md"}, ALLOW, "", id="critic-read"
        ),
        pytest.param(
            "critic",
            "Write",
            {"file_path": "notes.md", "content": "x"},
            DENY,
            NOT_AVAILABLE,
            id="I3-critic-write",
        ),
        pytest.param(
            "critic",
            "Edit",
            {"file_path": "brief.md", "old_string": "a", "new_string": "b"},
            DENY,
            NOT_AVAILABLE,
            id="I3-critic-edit",
        ),
        pytest.param(
            "summary",
            "Write",
            {"file_path": OUTPUT, "content": "x"},
            ALLOW,
            "",
            id="summary-write",
        ),
        pytest.param(
            "summary",
            "Edit",
            {"file_path": OUTPUT, "old_string": "a", "new_string": "b"},
            DENY,
            NOT_AVAILABLE,
            id="summary-no-edit",
        ),
        pytest.param(
            "fix",
            "Edit",
            {"file_path": OUTPUT, "old_string": "a", "new_string": "b"},
            ALLOW,
            "",
            id="fix-edit",
        ),
        pytest.param(
            "repair",
            "WebSearch",
            {"query": "x"},
            DENY,
            NOT_AVAILABLE,
            id="I5-repair-search",
        ),
    ],
)
def test_other_kinds(
    make_box: MakeBox,
    kind: str,
    tool: str,
    tool_input: dict[str, Any],
    allowed: bool,
    fragment: str,
) -> None:
    check(make_box(kind), tool, tool_input, allowed, fragment)


def test_deadline_denies_everything(make_box: MakeBox) -> None:
    """I6, I7: after the deadline every call is denied, even the result."""
    box = make_box("draft", deadline=1_790_000_000.0)

    for tool, tool_input in [
        ("Read", {"file_path": "brief.md"}),
        ("StructuredOutput", {"status": "ok"}),
    ]:
        decision = box.decide(tool, tool_input)
        assert not decision.allow
        assert "time is up" in decision.reason


def test_deadline_is_checked_before_the_tool(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=1_790_000_000.0)

    assert "time is up" in box.decide("Bash", {"command": "ls"}).reason


def test_denial_reasons_do_not_echo_credentials(researcher: Box) -> None:
    decision = researcher.decide(
        "WebFetch",
        {"url": "https://alice:s3cret-token@example.com/x?key=abc", "prompt": "x"},
    )

    assert not decision.allow
    assert "s3cret" not in decision.reason
    assert "s3cret" not in decision.target
    assert "key=abc" not in decision.target
    assert decision.target == "https://example.com/x"


def test_denial_reasons_are_one_line(writer: Box) -> None:
    decision = writer.decide("Evil\nTool\x1b[31m", {})

    assert not decision.allow
    assert "\n" not in decision.reason
    assert "\x1b" not in decision.reason


def test_a_policy_cannot_allow_a_tool_the_guard_cannot_check(writer: Box) -> None:
    """I1: allowlisting needs a checker too, so the guard's set is closed."""
    object.__setattr__(writer.policy, "allowed_tools", ("Read", "Bash"))

    decision = writer.decide("Bash", {"command": "ls"})

    assert not decision.allow
    assert NOT_AVAILABLE in decision.reason


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"tool_input": {}}, id="no-tool-name"),
        pytest.param({"tool_name": "", "tool_input": {}}, id="empty-tool-name"),
        pytest.param({"tool_name": "Read"}, id="no-tool-input"),
        pytest.param({"tool_name": "Read", "tool_input": ["x"]}, id="list-tool-input"),
    ],
)
def test_malformed_tool_calls_are_errors(writer: Box, payload: dict[str, Any]) -> None:
    """I6: a call the guard cannot read is blocked, never allowed."""
    with pytest.raises(GuardError):
        guard.decide(payload, writer.policy, now=0.0)


def test_relative_paths_resolve_against_the_workdir_not_the_process_cwd(
    writer: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(writer.outside)

    assert writer.decide("Read", {"file_path": "secret.txt"}).allow
    assert writer.decide("Read", {"file_path": "brief.md"}).allow
    assert not writer.decide("Read", {"file_path": "../outside/secret.txt"}).allow


def test_write_with_no_writable_path(writer: Box) -> None:
    """I3: a policy may allow Write yet list nothing to write."""
    data = writer.policy.to_dict()
    data["allowed_tools"] = ["Read", "Write"]
    data["writable"] = []
    policy = Policy(**data)
    payload = writer.payload("Write", {"file_path": OUTPUT, "content": "x"})

    decision = guard.decide(payload, policy, now=NOW)

    assert not decision.allow
    assert "this session writes no files" in decision.reason


def test_web_fetch_uses_the_system_resolver_by_default(
    researcher: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        assert host == "example.org"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    payload = researcher.payload(
        "WebFetch", {"url": "https://example.org/", "prompt": "x"}
    )

    decision = guard.decide(payload, researcher.policy, now=NOW)

    assert not decision.allow
    assert PRIVATE in decision.reason


def test_system_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 0)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 0, 0, 0)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: infos)

    assert guard.system_resolve("example.com") == ["93.184.215.14", "2606:4700::1111"]


def test_system_resolve_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr(socket, "getaddrinfo", fail)

    with pytest.raises(OSError, match="could not be resolved"):
        guard.system_resolve("nowhere.example.com")


def test_system_resolve_gives_up_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def hang(*args: Any, **kwargs: Any) -> list[Any]:
        release.wait(5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    monkeypatch.setattr(guard, "DNS_TIMEOUT_S", 0.05)
    try:
        with pytest.raises(OSError, match="could not be resolved"):
            guard.system_resolve("slow.example.com")
    finally:
        release.set()
