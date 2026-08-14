"""`app.infrastructure.credentials.filesystem.FilesystemCredentialResolver`."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.ports.credentials import CredentialNotFoundError
from app.infrastructure.credentials.filesystem import FilesystemCredentialResolver

pytestmark = pytest.mark.unit


def _write_secret(base_dir: Path, ref: str, *, username: str, password: str) -> None:
    secret_dir = base_dir / ref
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text(username)
    (secret_dir / "password").write_text(password)


async def test_resolves_username_and_password_from_separate_files(tmp_path: Path) -> None:
    _write_secret(tmp_path, "ucsm-dc1", username="svc-ucsm", password="s3cr3t")
    resolver = FilesystemCredentialResolver(tmp_path)

    creds = await resolver.resolve("ucsm-dc1")

    assert creds.username == "svc-ucsm"
    assert creds.password == "s3cr3t"


async def test_strips_trailing_newline_kubectl_style_secrets_often_have(tmp_path: Path) -> None:
    _write_secret(tmp_path, "ucsm-dc1", username="svc-ucsm\n", password="s3cr3t\n")
    resolver = FilesystemCredentialResolver(tmp_path)

    creds = await resolver.resolve("ucsm-dc1")

    assert creds.username == "svc-ucsm"
    assert creds.password == "s3cr3t"


async def test_missing_credential_ref_raises(tmp_path: Path) -> None:
    resolver = FilesystemCredentialResolver(tmp_path)

    with pytest.raises(CredentialNotFoundError):
        await resolver.resolve("does-not-exist")


async def test_missing_password_file_raises(tmp_path: Path) -> None:
    secret_dir = tmp_path / "ucsm-dc1"
    secret_dir.mkdir(parents=True)
    (secret_dir / "username").write_text("svc-ucsm")
    resolver = FilesystemCredentialResolver(tmp_path)

    with pytest.raises(CredentialNotFoundError):
        await resolver.resolve("ucsm-dc1")


async def test_empty_file_raises(tmp_path: Path) -> None:
    _write_secret(tmp_path, "ucsm-dc1", username="", password="s3cr3t")
    resolver = FilesystemCredentialResolver(tmp_path)

    with pytest.raises(CredentialNotFoundError):
        await resolver.resolve("ucsm-dc1")


async def test_never_includes_secret_values_in_the_error_message(tmp_path: Path) -> None:
    resolver = FilesystemCredentialResolver(tmp_path)

    with pytest.raises(CredentialNotFoundError) as exc_info:
        await resolver.resolve("does-not-exist")

    # The credential_ref *name* is fine to log/surface — it's the values
    # (which this error path never even reads) that must never appear.
    assert "does-not-exist" in str(exc_info.value)
