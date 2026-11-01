import re
from pathlib import Path

import pytest

from dfm.secrets import (
    RULE_NAMES,
    Profile,
    contains_redaction,
    is_secret_path,
    redact,
    redact_deep,
    redact_secret_file,
    redact_text,
)

REFLEX_REDACT = Path(__file__).resolve().parents[2] / "reflex-tui/packages/safety/src/redact.ts"

PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAA\n"
    "-----END OPENSSH PRIVATE KEY-----"
)
SEED = "test test test test test test test test test test test junk"

# (input, secret substring that must disappear) -- CONTEXT profile.
SECRETS: list[tuple[str, str]] = [
    (PEM, "b3BlbnNzaC1rZXktdjEAAAAA"),
    ("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx", "sk-abcdefghijklmnopqrstuvwx"),
    ('DATABASE_URL="postgres://u:hunter2@db:5432/app"', "hunter2"),
    ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
    ("aws configure set aws_secret_access_key abc123SECRET", "abc123SECRET"),
    ('{"password": "correct-horse"}', "correct-horse"),
    ("api_key: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
    ("client_secret=shh-its-secret", "shh-its-secret"),
    ("Authorization: Bearer abc.def.ghi.jkl", "abc.def.ghi.jkl"),
    ("curl -H 'X-Api-Key: k3y-v4lue' https://x", "k3y-v4lue"),
    ("Cookie: session=abcdef; csrftoken=xyz", "session=abcdef"),
    ("redis://:pa55@cache:6379/0", "pa55"),
    ("https://user:pa55word@example.com/repo.git", "pa55word"),
    ("sk-ant-api03-abcdefghijklmnopqrstu", "sk-ant-api03-abcdefghijklmnopqrstu"),
    ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("ghp_" + "a" * 36, "ghp_" + "a" * 36),
    ("github_pat_" + "B" * 30, "github_pat_" + "B" * 30),
    ("glpat-" + "c" * 20, "glpat-" + "c" * 20),
    ("xoxb-1234567890-abcdef", "xoxb-1234567890-abcdef"),
    ("AIza" + "D" * 35, "AIza" + "D" * 35),
    ("sk_live_" + "e" * 24, "sk_live_" + "e" * 24),
    ("npm_" + "f" * 36, "npm_" + "f" * 36),
    ("hf_" + "g" * 34, "hf_" + "g" * 34),
    ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "SflKxwRJSMeKKF2QT4"),
    ("cast send 0x1 --private-key 0x" + "a" * 64, "a" * 64),
    ("PRIVATE_KEY=0x" + "b" * 64, "b" * 64),
    ('privateKey: "0x' + "c" * 64 + '"', "c" * 64),
    (f'MNEMONIC="{SEED}"', "junk"),
    (f'mnemonic: "{SEED}"', "junk"),
    (f'const words = "{SEED}";', "junk"),
    ("client-key-data: LS0tLS1CRUdJTiBSU0E=", "LS0tLS1CRUdJTiBSU0E="),
    ("token: eyJhbGciOiJSUzI1NiIs.eyJpc3MiOiJrdWJlcm5ldGVz.abcdefghijklmnop", "abcdefghijklmnop"),
    ("aB3" + "x" * 40 + "Z9", "aB3" + "x" * 40 + "Z9"),
    # review 2026-10-31: shapes that passed through
    ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("Private key: 0x" + "4c" * 32, "4c" * 32),  # `cast wallet new`, `anvil`
    ("Secret key: 0x" + "5d" * 32, "5d" * 32),
    (f"mnemonic: {SEED}", "junk"),  # unquoted: the whole line, not the first word
    (f"Seed phrase: {SEED}", "junk"),
    ("whsec_" + "h" * 32, "whsec_" + "h" * 32),
    ("dop_v1_" + "a1" * 32, "dop_v1_" + "a1" * 32),
    ("SK" + "b2" * 16, "SK" + "b2" * 16),
    ("AGE-SECRET-KEY-1" + "Q" * 58, "AGE-SECRET-KEY-1" + "Q" * 58),
]


@pytest.mark.parametrize(("text", "secret"), SECRETS)
def test_context_profile_removes_secret(text: str, secret: str) -> None:
    result = redact(text)
    assert secret not in result.text, result
    assert result.count >= 1
    assert contains_redaction(result.text)


@pytest.mark.parametrize(
    "text",
    [
        "def add(a, b):\n    return a + b\n",
        "CC=gcc\nCFLAGS=-O2\n",
        "author = 'Ada <ada@example.com>'",
        "path = '/Users/ada/code/app'",
        "commit 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f",
        "bytes32 constant SLOT = 0x" + "d" * 64 + ";",
        "tx hash 0x" + "e" * 64,  # a hash is not a key: only a labelled value is redacted
        "# check that each item from list has valid name and type before using it",
        "let tokens = lexer.tokenize(source);",
        "password_field = form.get_field('password')",
        "the mnemonic for the ordering is: adults never trust",  # prose, no secret
        "-".join(["aB"] * 40),  # long run of address/token characters without a digit
    ],
)
def test_context_profile_leaves_code_alone(text: str) -> None:
    result = redact(text)
    assert result.text == text
    assert result.count == 0, result.rules


def test_redaction_is_linear_in_the_input() -> None:
    """A long line of token characters with `-`/`_` boundaries (minified JS, base64 tables)
    used to make `high-entropy` and `email` rescan from every boundary."""
    import time

    def seconds(text: str) -> float:
        start = time.perf_counter()
        redact_text(text, Profile.TRACE)
        return time.perf_counter() - start

    small, large = "aB-" * 10_000, "aB-" * 40_000
    assert seconds(large) < 8 * seconds(small) + 0.05  # ~4x input: linear, with slack


def test_redact_diff_truncates_oversized_diffs_at_a_file_boundary() -> None:
    from dfm.secrets import redact_diff

    file_a = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
    file_b = (
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n" + "+" + "z" * 200 + "\n"
    )
    shown = redact_diff(file_a + file_b, limit=len(file_a) + 20)
    assert shown.startswith(file_a)
    assert "b/b.py" not in shown and "[diff truncated: " in shown and "1 file(s)" in shown


def test_trace_profile_also_redacts_identity() -> None:
    text = "ada@example.com edited /Users/ada/app\nFOO=bar"
    traced = redact_text(text, Profile.TRACE)
    assert "ada@example.com" not in traced
    assert "/Users/ada" not in traced
    assert "FOO=[REDACTED]" in traced


def test_seed_phrase_word_counts() -> None:
    eleven = " ".join(["word"] * 11)
    assert redact_text(f'x = "{eleven}"') == f'x = "{eleven}"'
    twelve = " ".join(["word"] * 12)
    assert "[REDACTED_SEED_PHRASE]" in redact_text(f'x = "{twelve}"')


def test_redact_deep() -> None:
    value = {"cmd": ["cast", "--private-key", "0xabc"], "n": 3, "env": {"GH_TOKEN": "ghp_x"}}
    out = redact_deep(value)
    assert out["cmd"] == ["cast", "--private-key", "[REDACTED]"]
    assert out["env"]["GH_TOKEN"] == "[REDACTED]"
    joined = redact_deep({"cmd": "cast send --private-key 0xabc"})
    assert "0xabc" not in joined["cmd"]
    assert out["n"] == 3


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        (".env.local", True),
        (".env.example", True),
        ("config/.env.production", True),
        ("deploy/key.pem", True),
        ("certs/server.key", True),
        ("id_rsa", True),
        ("id_ed25519.pub", False),
        ("home/.ssh/config", True),
        ("x/.aws/credentials", True),
        ("x/.kube/config", True),
        ("infra/prod.tfvars", True),
        ("infra/terraform.tfstate", True),
        (".npmrc", True),
        ("keystores/UTC--2024", True),
        ("service-account-prod.json", True),
        ("src/config.py", False),
        ("src/env.rs", False),
        ("README.md", False),
        ("tests/test_keys.py", False),
    ],
)
def test_is_secret_path(path: str, secret: bool) -> None:
    assert is_secret_path(path) is secret


def test_redact_secret_file_keeps_keys() -> None:
    env = "# db\nDATABASE_URL=postgres://u:p@h/db\nexport DEBUG=1\n\nAPI_KEY = 'abc'\n"
    out = redact_secret_file(".env", env)
    assert "DATABASE_URL=[REDACTED]" in out
    assert "export DEBUG=[REDACTED]" in out
    assert "API_KEY = [REDACTED]" in out
    assert "# db" in out
    assert "postgres" not in out
    assert "abc" not in out


def test_redact_secret_file_hides_key_material_entirely() -> None:
    out = redact_secret_file("id_rsa", PEM)
    assert out.startswith("[REDACTED: secret file id_rsa")
    assert "b3Bl" not in out


def test_contains_redaction() -> None:
    assert contains_redaction("x = [REDACTED_TOKEN]")
    assert contains_redaction("by <user>")
    assert not contains_redaction("x = 1")


@pytest.mark.skipif(not REFLEX_REDACT.exists(), reason="reflex-tui sibling checkout not present")
def test_rule_parity_with_reflex() -> None:
    reflex_rules = set(re.findall(r'name:\s*"([a-z0-9-]+)"', REFLEX_REDACT.read_text()))
    missing = reflex_rules - RULE_NAMES - {"home", "username"}
    assert not missing, f"Reflex redacts {missing}; port those rules to dfm/secrets/redact.py"


def test_redact_diff_keeps_keys_of_secret_files_and_redacts_the_rest() -> None:
    from dfm.secrets import redact_diff

    diff = (
        "diff --git a/.env b/.env\n"
        "index 1..2 100644\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -1 +1,2 @@\n"
        "-MY_SERVICE=old\n"
        "+MY_SERVICE=hunter2\n"
        "+DEBUG=1\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    shown = redact_diff(diff)
    assert "hunter2" not in shown and "old" not in shown.split("app.py")[0]
    assert "+MY_SERVICE=[REDACTED]" in shown and "+DEBUG=[REDACTED]" in shown
    assert "+x = 2" in shown  # ordinary code is untouched


def test_redact_argv_handles_attached_and_program_specific_passwords() -> None:
    from dfm.secrets import redact_argv

    assert redact_argv(["mysql", "-u", "root", "-phunter2"])[3] == "-p[REDACTED]"
    assert redact_argv(["redis-cli", "-a", "s3cret", "get", "k"])[2] == "[REDACTED]"
    assert redact_argv(["curl", "-u", "me:pw", "https://x"])[2] == "me:[REDACTED]"
    assert redact_argv(["tool", "--password=abc"])[1] == "--password=[REDACTED]"
    assert redact_argv(["psql", "-p", "5432"]) == ["psql", "-p", "5432"]  # a port, not a secret
