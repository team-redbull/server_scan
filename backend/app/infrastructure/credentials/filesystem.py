"""Resolves `Manager.credential_ref` against a mounted directory of secret
files — the standard Kubernetes "Secret as a volume" shape: each key in a
`Secret` becomes a file named after that key, under a directory named
after the mount. Concretely, for `credential_ref="ucsm-dc1-svc"`:

    {base_dir}/ucsm-dc1-svc/username
    {base_dir}/ucsm-dc1-svc/password

`base_dir` (`Settings.credentials_dir`) is one configurable mount point
that every manager's Secret gets projected under (via a K8s `projected`
volume combining one `secret` source per manager, or one Secret per
manager type with one key pair per manager — either way, this resolver
doesn't care how the directory got populated, only that it's there by the
time a collector runs). Chosen over reading environment variables per
`credential_ref` specifically because a CronJob's pod spec is written
once and doesn't need editing every time a new manager is added — new
managers just need their Secret added to the projected volume, not a new
env var wired into the Job/CronJob manifest.

Never logs a credential value, and the one place a value could leak
(an exception message) is deliberately avoided: read failures below only
ever mention the `credential_ref` and file path, never file contents.
"""

from __future__ import annotations

from pathlib import Path

from app.domain.ports.credentials import CredentialNotFoundError, ManagerCredentials


class FilesystemCredentialResolver:
    """`resolve()` is declared `async` (matching the `CredentialResolver`
    Protocol every implementation must satisfy) but reads synchronously
    underneath — deliberately, not an oversight: a Kubernetes Secret
    volume is tmpfs-backed (an in-memory filesystem, not real disk I/O),
    and this method is called a handful of times per collector run at
    most, so offloading to a thread pool would add complexity for a read
    that's already effectively memory-speed.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir)

    async def resolve(self, credential_ref: str) -> ManagerCredentials:
        secret_dir = self._base_dir / credential_ref
        username = self._read_required(secret_dir / "username", credential_ref)
        password = self._read_required(secret_dir / "password", credential_ref)
        return ManagerCredentials(username=username, password=password)

    def _read_required(self, path: Path, credential_ref: str) -> str:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CredentialNotFoundError(
                f"Could not read {path} for credential_ref {credential_ref!r}: "
                f"{exc.__class__.__name__}"
            ) from exc
        # Kubernetes Secret-volume files commonly carry a trailing
        # newline (however the Secret's value was authored/kubectl-
        # created) — strip it so a credential's real value is never
        # silently wrong by one invisible character.
        value = raw.strip()
        if not value:
            raise CredentialNotFoundError(f"{path} is empty for credential_ref {credential_ref!r}.")
        return value
