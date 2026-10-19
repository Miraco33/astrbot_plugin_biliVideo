"""The publication gate reports locations, never disclosed values."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_publication.py"
_SPEC = importlib.util.spec_from_file_location("publication_check", _SCRIPT)
assert _SPEC is not None
assert _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)


def rules(content: str) -> set[str]:
    return {finding[2] for finding in check.scan_text("example.md", content)}


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def repository(root: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is needed for the tracked-file integration check")
    git(root, "init")
    (root / "metadata.yaml").write_text(
        "\n".join(f"{key}: {value}" for key, value in check.EXPECTED_METADATA.items()) + "\n",
        encoding="utf-8",
    )
    for relative in check.REQUIRED_LICENSES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Synthetic license fixture\n", encoding="utf-8")
    git(root, "add", ".")


def test_source_terms_and_placeholders_are_allowed() -> None:
    text = "\n".join((
        'jar.save({"SESSDATA": "abc", "bili_jct": "xyz"})',
        'api_key = "<API_KEY>"',
        'token = "fake_token_for_test"',
        'headers = {"Authorization": f"Bearer {api_key}"}',
        'origin = "aiocqhttp:GroupMessage:1"',
        "https://example.com/video?id=123",
    ))
    assert rules(text) == set()


@pytest.mark.parametrize(("text", "expected"), [
    ("ghp_" + "A" * 36, "credential-token"),
    ("sk-proj-" + "A1b2C3d4" * 5, "credential-token"),
    ("-----BEGIN " + "OPENSSH PRIVATE KEY-----", "private-key"),
    ('api_key = "' + "aB3dE6gH9jK2mN5p" + '"', "credential-value"),
    (
        "https://media.example.com/a.m4a?auth_key=" + "aB3dE6gH9jK2mN5p",
        "authenticated-url",
    ),
    ("aiocqhttp:GroupMessage:" + "987654321", "real-umo"),
])
def test_detects_disclosures(text: str, expected: str) -> None:
    assert expected in rules(text)


def test_only_tracked_files_are_scanned_and_values_are_not_printed(tmp_path: Path, capsys) -> None:
    repository(tmp_path)
    (tmp_path / ".env").write_text("untracked runtime fixture\n", encoding="utf-8")
    assert check.scan_repository(tmp_path) == set()
    secret = "ghp_" + "Z" * 36
    (tmp_path / "note.md").write_text(secret + "\n", encoding="utf-8")
    git(tmp_path, "add", "note.md")
    assert check.main(["--repo-root", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert '"note.md":1:credential-token' in output.out
    assert secret not in output.out
    assert output.err == ""
    git(tmp_path, "add", ".env")
    assert (".env", 0, "runtime-file") in check.scan_repository(tmp_path)


def test_repository_identity_and_licenses_are_required(tmp_path: Path) -> None:
    repository(tmp_path)
    (tmp_path / "metadata.yaml").write_text("name: renamed_plugin\n", encoding="utf-8")
    (tmp_path / "LICENSE").unlink()
    findings = check.scan_repository(tmp_path)
    assert ("metadata.yaml", 0, "metadata-name") in findings
    assert ("LICENSE", 0, "license-missing") in findings
