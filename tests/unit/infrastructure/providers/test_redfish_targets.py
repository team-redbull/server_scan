"""`app.infrastructure.providers.redfish.targets`.

The whole point of this module is that it fails *before* the network, so
most of these tests assert on error messages. A fan-out collector that
discovers a typo on host 380 of 400 has already spent 40 minutes, and one
that resolves a typo'd group to the default credential has already
presented a shared account to a machine it was never meant for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.infrastructure.providers.redfish.targets import InventoryError, load_targets

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def _load(tmp_path: Path, inventory: str, credentials: str = "", **kwargs: object) -> object:
    inv = _write(tmp_path, "inventory.toml", inventory)
    creds = _write(tmp_path, "credentials.toml", credentials) if credentials else None
    return load_targets(
        inventory_path=str(inv),
        credentials_path=str(creds) if creds else "",
        fallback_login=kwargs.get("fallback_login", ("svc", "secret")),  # type: ignore[arg-type]
    )


class TestCredentialResolution:
    def test_a_host_uses_a_credential_named_after_it(self, tmp_path: Path) -> None:
        """The rung that keeps an estate where every BMC has its own
        account from repeating the name on all 400 entries.
        """
        targets = _load(
            tmp_path,
            '[[hosts]]\nhost = "10.0.0.1"\n',
            '[credentials."10.0.0.1"]\nusername = "u1"\npassword = "p1"\n',
        )
        assert targets[0].credential.name == "10.0.0.1"  # type: ignore[index]
        assert targets[0].credential.username == "u1"  # type: ignore[index]

    def test_an_explicit_credential_wins_over_a_host_named_one(self, tmp_path: Path) -> None:
        targets = _load(
            tmp_path,
            '[[hosts]]\nhost = "10.0.0.1"\ncredential = "shared"\n',
            '[credentials."10.0.0.1"]\nusername = "u1"\npassword = "p1"\n'
            '[credentials.shared]\nusername = "us"\npassword = "ps"\n',
        )
        assert targets[0].credential.name == "shared"  # type: ignore[index]

    def test_the_fleet_wide_login_is_the_last_resort(self, tmp_path: Path) -> None:
        targets = _load(tmp_path, '[[hosts]]\nhost = "10.0.0.1"\n')
        assert targets[0].credential.name == "default"  # type: ignore[index]
        assert targets[0].credential.username == "svc"  # type: ignore[index]

    def test_a_host_with_no_resolvable_credential_names_what_to_set(self, tmp_path: Path) -> None:
        with pytest.raises(InventoryError) as exc:
            _load(tmp_path, '[[hosts]]\nhost = "10.0.0.1"\n', fallback_login=None)
        assert "10.0.0.1" in str(exc.value)
        assert "INVENTORY_REDFISH_USERNAME" in str(exc.value)

    def test_an_undefined_credential_name_is_a_load_error(self, tmp_path: Path) -> None:
        with pytest.raises(InventoryError, match="nope"):
            _load(tmp_path, '[[hosts]]\nhost = "10.0.0.1"\ncredential = "nope"\n')

    def test_a_credential_missing_a_password_is_rejected(self, tmp_path: Path) -> None:
        """Same rule `CredentialResolver.resolve` already holds, and it
        matters more here: a blank password reaches the BMC as a real
        login attempt, and that attempt counts toward lockout.
        """
        with pytest.raises(InventoryError, match="needs both"):
            _load(
                tmp_path,
                '[[hosts]]\nhost = "10.0.0.1"\ncredential = "half"\n',
                '[credentials.half]\nusername = "u"\npassword = ""\n',
            )


class TestFailClosed:
    def test_a_typod_group_fails_rather_than_falling_through(self, tmp_path: Path) -> None:
        """The most important test in the file. Falling through to the
        default credential here is exactly how a typo sprays a shared
        account across machines it was never meant for.
        """
        with pytest.raises(InventoryError) as exc:
            _load(
                tmp_path,
                '[groups.site-one]\ncredential = "default"\n\n'
                '[[hosts]]\nhost = "10.0.0.1"\ngroup = "site-onee"\n',
            )
        assert "site-onee" in str(exc.value)
        assert "site-one" in str(exc.value)  # names the known groups

    def test_an_empty_inventory_is_a_configuration_error(self, tmp_path: Path) -> None:
        """Distinct from "every host is down", and must not print the
        same thing — almost always a ConfigMap that failed to mount.
        """
        with pytest.raises(InventoryError, match="failed to mount"):
            _load(tmp_path, "version = 1\n")

    def test_a_duplicate_host_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(InventoryError, match="already defined"):
            _load(tmp_path, '[[hosts]]\nhost = "10.0.0.1"\n\n[[hosts]]\nhost = "10.0.0.1"\n')

    def test_a_host_carrying_credentials_is_rejected(self, tmp_path: Path) -> None:
        """A credential in the address would reach `bmc_address_raw`, and
        from there MongoDB, the API and the dry-run print.
        """
        with pytest.raises(InventoryError, match="embeds credentials"):
            _load(tmp_path, '[[hosts]]\nhost = "user:pw@10.0.0.1"\n')

    def test_a_url_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(InventoryError, match="bare hostname"):
            _load(tmp_path, '[[hosts]]\nhost = "https://10.0.0.1/redfish/v1"\n')

    def test_disabling_tls_without_a_reason_is_rejected(self, tmp_path: Path) -> None:
        """The reason is what makes the exception visible in review, and
        the collector sends this BMC's password in the clear.
        """
        with pytest.raises(InventoryError, match="verify_tls_reason"):
            _load(tmp_path, '[[hosts]]\nhost = "10.0.0.1"\nverify_tls = false\n')

    def test_a_missing_inventory_names_the_variable(self, tmp_path: Path) -> None:
        with pytest.raises(InventoryError, match="INVENTORY_REDFISH_INVENTORY_FILE"):
            load_targets(inventory_path="", credentials_path="", fallback_login=("svc", "secret"))

    def test_a_malformed_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(InventoryError, match="not valid TOML"):
            _load(tmp_path, "[[hosts]\nhost = broken\n")


class TestSettingsLadder:
    def test_group_settings_apply_and_a_host_can_override_them(self, tmp_path: Path) -> None:
        targets = _load(
            tmp_path,
            "[defaults]\nport = 443\n\n"
            '[groups.lab]\nverify_tls = false\nverify_tls_reason = "factory certs"\n\n'
            '[[hosts]]\nhost = "10.0.0.1"\ngroup = "lab"\n\n'
            '[[hosts]]\nhost = "10.0.0.2"\ngroup = "lab"\nport = 8443\n',
        )
        assert targets[0].verify_tls is False  # type: ignore[index]
        assert targets[0].verify_tls_reason == "factory certs"  # type: ignore[index]
        assert targets[0].base_url == "https://10.0.0.1"  # type: ignore[index]
        assert targets[1].base_url == "https://10.0.0.2:8443"  # type: ignore[index]

    def test_a_directory_of_files_is_merged(self, tmp_path: Path) -> None:
        """What lets a large estate shard per site without a format
        change.
        """
        shard = tmp_path / "inventory.d"
        shard.mkdir()
        (shard / "a.toml").write_text('[[hosts]]\nhost = "10.0.0.1"\n')
        (shard / "b.toml").write_text('[[hosts]]\nhost = "10.0.0.2"\n')
        targets = load_targets(
            inventory_path=str(shard), credentials_path="", fallback_login=("svc", "secret")
        )
        assert sorted(t.host for t in targets) == ["10.0.0.1", "10.0.0.2"]

    def test_a_duplicate_across_shards_is_still_rejected(self, tmp_path: Path) -> None:
        shard = tmp_path / "inventory.d"
        shard.mkdir()
        (shard / "a.toml").write_text('[[hosts]]\nhost = "10.0.0.1"\n')
        (shard / "b.toml").write_text('[[hosts]]\nhost = "10.0.0.1"\n')
        with pytest.raises(InventoryError, match="already defined"):
            load_targets(
                inventory_path=str(shard), credentials_path="", fallback_login=("svc", "secret")
            )


def test_a_credential_never_prints_its_password(tmp_path: Path) -> None:
    """`repr` is what a traceback frame and a debugger session both
    reach for.
    """
    targets = _load(
        tmp_path,
        '[[hosts]]\nhost = "10.0.0.1"\n',
        '[credentials."10.0.0.1"]\nusername = "u1"\npassword = "hunter2"\n',
    )
    rendered = repr(targets[0])  # type: ignore[index]
    assert "hunter2" not in rendered
    assert "***" in rendered
