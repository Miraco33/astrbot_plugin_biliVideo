"""Scan tracked publication text without printing sensitive matched values.

This is a second check, not a substitute for review before pushing. Historical
commits, images, release descriptions and attachments require separate review.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

EXPECTED_METADATA = {
    "name": "astrbot_plugin_bilivideo",
    "author": "storyAura",
    "repo": "https://github.com/Miraco33/astrbot_plugin_biliVideo",
}
REQUIRED_LICENSES = (
    "LICENSE",
    "fonts/NotoSansSC-LICENSE.txt",
    "fonts/JetBrainsMono-LICENSE.txt",
)
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".sh", ".ps1"}
RUNTIME_PARTS = {"data", "plugin_data", "logs", "runtime", "backups", "search_results"}
RUNTIME_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".p12", ".pfx",
    ".mp3", ".m4a", ".wav", ".flac", ".mp4", ".webm",
}
SAFE_ENV_NAMES = {".env.example", ".env.sample", ".env.template"}
TOKENS = re.compile(
    r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{36,}|"
    r"github_pat_[A-Za-z0-9_]{50,}|sk-(?:proj-|org-)?[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})(?![\w-])"
)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
CREDENTIAL = re.compile(
    r"""(?ix)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|
    password|passwd|token|cookie|authorization|SESSDATA|bili_jct)
    ["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']"""
)
URL = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)
AUTH_QUERY_KEYS = {
    "token", "access_token", "refresh_token", "auth_key", "authorization", "signature",
    "sign", "sig", "wssecret", "w_rid", "sessdata", "bili_jct", "qrcode_key",
    "x-amz-signature", "x-amz-credential", "x-amz-security-token", "x-goog-signature",
}
UMO = re.compile(r"(?<![\w])[\w.-]+:(?:GroupMessage|FriendMessage|PrivateMessage):\d{5,}(?![\w])")
PLACEHOLDER = re.compile(
    r"(?i)^(?:<[^>]+>|\$\{[^}]+\}|\{\{[^}]+\}\}|"
    r"(?:fake|dummy|mock|test|example|sample|placeholder|redacted|changeme|your)"
    r"(?:[_-][a-z0-9]+)*|[xX*._-]+)$"
)


def concrete(value: str) -> bool:
    """Avoid flagging field names, short fixtures and explicit placeholders."""
    value = value.strip()
    return (
        len(value) >= 8
        and PLACEHOLDER.fullmatch(value) is None
        and len(set(value)) >= 4
        and not any(marker in value for marker in ("{", "}", "<", ">"))
    )


def authenticated_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.rstrip(").,;"))
        return bool(parsed.username and parsed.password) or any(
            key.lower() in AUTH_QUERY_KEYS and concrete(item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        )
    except ValueError:
        return False


def scan_text(path: str, content: str) -> set[tuple[str, int, str]]:
    findings = set()
    if content.startswith("\ufeff"):
        findings.add((path, 1, "utf8-bom"))
    for number, line in enumerate(content.splitlines(), 1):
        rules = set()
        if PRIVATE_KEY.search(line):
            rules.add("private-key")
        if TOKENS.search(line):
            rules.add("credential-token")
        if any(concrete(match[1]) for match in CREDENTIAL.finditer(line)):
            rules.add("credential-value")
        if any(authenticated_url(match[0]) for match in URL.finditer(line)):
            rules.add("authenticated-url")
        if UMO.search(line):
            rules.add("real-umo")
        findings.update((path, number, rule) for rule in rules)
    return findings


def runtime_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        bool(set(path.parts[:-1]) & RUNTIME_PARTS)
        or name == ".env"
        or (name.startswith(".env.") and name not in SAFE_ENV_NAMES)
        or name in {"id_rsa", "id_ed25519", "id_ecdsa", "bili_cookies.json"}
        or name.endswith(("-wal", "-shm"))
        or path.suffix.lower() in RUNTIME_SUFFIXES
        or bool(re.fullmatch(r"(?:subscriptions|push_targets|credentials|secrets?)\.(?:json|txt)", name))
    )


def scan_repository(root: Path) -> set[tuple[str, int, str]]:
    root = root.resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "-z"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return {(".", 0, "tracked-files-unavailable")}
    paths = sorted({value.decode("utf-8") for value in result.stdout.split(b"\0") if value})
    findings = set()
    documents = {}
    for relative in paths:
        target = root / relative
        if runtime_path(Path(relative)):
            findings.add((relative, 0, "runtime-file"))
            continue
        try:
            target.resolve().relative_to(root)
            if target.is_symlink():
                findings.add((relative, 0, "symlink-review-required"))
                continue
            raw = target.read_bytes()
        except (OSError, ValueError):
            findings.add((relative, 0, "unreadable-tracked-file"))
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            if target.suffix.lower() in TEXT_SUFFIXES:
                findings.add((relative, 0, "invalid-utf8-text"))
            continue
        if b"\0" in raw:
            continue
        documents[relative] = content
        findings.update(scan_text(relative, content))
    metadata = documents.get("metadata.yaml", "")
    for key, expected in EXPECTED_METADATA.items():
        matches = re.findall(rf"^{key}:\s*(.*?)\s*$", metadata, re.MULTILINE)
        if len(matches) != 1 or matches[0].strip("'\"") != expected:
            findings.add(("metadata.yaml", 0, f"metadata-{key}"))
    for license_path in REQUIRED_LICENSES:
        if not documents.get(license_path):
            findings.add((license_path, 0, "license-missing"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    findings = scan_repository(parser.parse_args(argv).repo_root)
    for path, line, rule in sorted(findings):
        print(f"{json.dumps(path, ensure_ascii=True)}:{line}:{rule}")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
