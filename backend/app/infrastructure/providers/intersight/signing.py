"""HTTP Signature (`hs2019`) request signing for the Cisco Intersight API.

Intersight has no username/password path: every request carries an
`Authorization: Signature ...` header proving possession of an API key's
private half. See docs/adr/0017-intersight-collector.md, "Decision 2".

Pure and I/O-free on purpose — `sign()` takes a clock, so the whole
construction is reproducible in a test against a fixed key and a frozen
timestamp. That matters more here than usual: a signature this module
builds wrongly fails as an indistinguishable 401, the same symptom as an
expired key, a revoked key and a drifted clock.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from email.utils import formatdate

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

# The two key types Intersight issues: RSA for an API key v2, EC for a
# v3. Named so the signing path is typed rather than `Any`, which is what
# lets mypy check that each branch signs with a compatible algorithm.
PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey

# Signed on every request, in this order. Taken from Cisco's own
# canonical example rather than from the draft standard's default: the
# SDK defaults to signing `(created)` alone, which Intersight rejects.
_SIGNED_HEADERS = ("(request-target)", "host", "date", "digest")

_SCHEME = "hs2019"


class IntersightKeyError(ValueError):
    """An API key that cannot be used, detected before any request.

    Separate from the transport errors in `.client` because it is a
    configuration fault with a fix an operator can act on, and because it
    is knowable without a network round trip — which is what lets
    `health_check()` tell a malformed key apart from a rejected one.
    """


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """
    The headers one signed request must carry.

    Attributes:
        headers (dict[str, str]): `Host`, `Date`, `Digest` and
            `Authorization`, ready to merge into the outgoing request.
    """

    headers: dict[str, str]


def load_private_key(pem: str) -> PrivateKey:
    """
    Parse an API key's PEM private half.

    Args:
        pem (str): The PEM text, unencrypted. RSA (API key v2) and EC
            (API key v3) are both accepted.

    Returns:
        PrivateKey: An RSA or EC private key object.

    Raises:
        IntersightKeyError: If the text is not a PEM key, is encrypted,
            or is of an algorithm Intersight does not sign with.
    """
    text = pem.strip()
    if not text.startswith("-----BEGIN"):
        raise IntersightKeyError(
            "INVENTORY_INTERSIGHT_API_KEY_PEM is not a PEM private key — it must hold "
            "the whole key including its '-----BEGIN ... PRIVATE KEY-----' line. "
            "Intersight has no password login for its API; the credential is an API key."
        )
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except TypeError as exc:
        raise IntersightKeyError(
            "The Intersight private key is passphrase-protected, which this collector "
            "does not support — supply an unencrypted PEM."
        ) from exc
    except ValueError as exc:
        raise IntersightKeyError(f"The Intersight private key could not be parsed: {exc}") from exc
    if not isinstance(key, (rsa.RSAPrivateKey, ec.EllipticCurvePrivateKey)):
        raise IntersightKeyError(
            f"Unsupported Intersight private key type {type(key).__name__} — Intersight "
            "issues RSA (API key v2) and EC (API key v3) keys only."
        )
    return key


def _sign_digest(key: PrivateKey, message: bytes) -> bytes:
    """
    Sign the signing string with whichever algorithm the key implies.

    The algorithm is chosen by key type rather than configured, matching
    Cisco's own published example: RSA keys are v2 and sign
    RSASSA-PKCS1-v1_5, EC keys are v3 and sign ECDSA. Relying on a
    library default would sign RSA-PSS here, which Intersight rejects for
    a v2 key. See docs/adr/0017-intersight-collector.md.

    Args:
        key (PrivateKey): The loaded private key.
        message (bytes): The signing string.

    Returns:
        bytes: The raw signature.
    """
    if isinstance(key, rsa.RSAPrivateKey):
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    # DER-encoded, which is what `cryptography` produces and what the
    # scheme expects. RFC 6979's deterministic nonce is not reproduced —
    # it changes only how `k` is chosen, and a verifier cannot tell.
    return key.sign(message, ec.ECDSA(hashes.SHA256()))


class IntersightSigner:
    """
    Signs Intersight requests with one API key.

    See docs/adr/0017-intersight-collector.md, "Decision 2".
    """

    def __init__(self, *, key_id: str, private_key_pem: str) -> None:
        """
        Args:
            key_id (str): The API Key ID, as Intersight displays it.
            private_key_pem (str): The unencrypted PEM private half.

        Raises:
            IntersightKeyError: If either value is missing or the PEM
                cannot be used.
        """
        if not key_id.strip():
            raise IntersightKeyError(
                "No Intersight API Key ID — set INVENTORY_INTERSIGHT_API_KEY_ID to the "
                "key id shown beside the key in Intersight's Settings > API Keys."
            )
        self._key_id = key_id.strip()
        self._key = load_private_key(private_key_pem)

    def sign(self, *, method: str, path: str, query: str, host: str, now: float) -> SignedRequest:
        """
        Build the headers one request must carry to be accepted.

        Args:
            method (str): The HTTP method, e.g. `"GET"`.
            path (str): The absolute request path, e.g.
                `"/api/v1/compute/PhysicalSummaries"`.
            query (str): The already-encoded query string without its
                leading `?`, or `""`. It must be byte-identical to what
                is put on the wire — the signature covers it.
            host (str): The request's `Host`, without scheme or port
                unless the port is part of the authority.
            now (float): Seconds since the epoch, passed in so the
                construction is reproducible in a test.

        Returns:
            SignedRequest: The headers to merge into the request.
        """
        request_target = f"{method.lower()} {path}"
        if query:
            request_target += f"?{query}"

        date = formatdate(timeval=now, localtime=False, usegmt=True)
        # Every request this collector makes is a GET with no body, so
        # the digest is always that of the empty string. Computed rather
        # than hardcoded so a future POST cannot silently sign a stale
        # digest of a body it did not send.
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(b"").digest()).decode("ascii")

        values = {
            "(request-target)": request_target,
            "host": host,
            "date": date,
            "digest": digest,
        }
        signing_string = "\n".join(f"{name}: {values[name]}" for name in _SIGNED_HEADERS)
        signature = base64.b64encode(_sign_digest(self._key, signing_string.encode())).decode(
            "ascii"
        )
        authorization = (
            f'Signature keyId="{self._key_id}",algorithm="{_SCHEME}",'
            f'headers="{" ".join(_SIGNED_HEADERS)}",signature="{signature}"'
        )
        return SignedRequest(
            headers={
                "Host": host,
                "Date": date,
                "Digest": digest,
                "Authorization": authorization,
            }
        )
