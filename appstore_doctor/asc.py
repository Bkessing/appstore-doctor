"""App Store Connect API client.

Read-only by design. This module never issues POST, PATCH or DELETE — a tool
that mutates someone's live App Store listing on a bug is not a tool people
should trust with an API key.

Auth is an App Store Connect API key (.p8). The key never leaves the machine:
requests go directly from here to Apple. There is no server in the middle and
nothing is uploaded anywhere.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

API = "https://api.appstoreconnect.apple.com/v1"

# App Store Connect intermittently returns 401/404 for requests that are
# perfectly valid, and succeeds on an immediate retry with a fresh token.
# Treat these as transient rather than surfacing a confusing failure.
TRANSIENT = (401, 403, 404, 429, 500, 502, 503)


class ASCError(Exception):
    """Raised when the API is unreachable or credentials are unusable."""


class MissingCredentials(ASCError):
    """Raised when key id / issuer id / .p8 cannot be located."""


def _b64(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


class Client:
    def __init__(self, key_id=None, issuer_id=None, key_path=None):
        self.key_id = key_id or os.environ.get("ASC_KEY_ID")
        self.issuer_id = issuer_id or os.environ.get("ASC_ISSUER_ID")

        if not self.key_id or not self.issuer_id:
            raise MissingCredentials(
                "Set ASC_KEY_ID and ASC_ISSUER_ID (App Store Connect > Users and Access > "
                "Integrations > App Store Connect API). Both are identifiers, not secrets."
            )

        self.key_path = key_path or os.environ.get("ASC_KEY_FILEPATH") or os.path.expanduser(
            f"~/.appstoreconnect/private_keys/AuthKey_{self.key_id}.p8"
        )
        if not os.path.exists(self.key_path):
            raise MissingCredentials(
                f"Private key not found at {self.key_path}. Download the .p8 from App Store "
                "Connect and place it there, or set ASC_KEY_FILEPATH."
            )

        # Imported lazily so the local-only checks work without cryptography installed.
        from cryptography.hazmat.primitives import serialization

        with open(self.key_path, "rb") as handle:
            self._key = serialization.load_pem_private_key(handle.read(), password=None)
        self._token = None
        self._token_exp = 0

    def _mint(self) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        now = int(time.time())
        self._token_exp = now + 1200
        header = _b64(json.dumps(
            {"alg": "ES256", "kid": self.key_id, "typ": "JWT"}, separators=(",", ":")
        ).encode())
        payload = _b64(json.dumps(
            {"iss": self.issuer_id, "iat": now, "exp": self._token_exp,
             "aud": "appstoreconnect-v1"},
            separators=(",", ":"),
        ).encode())
        msg = header + b"." + payload
        der = self._key.sign(msg, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        # Apple requires the raw r||s signature, not the DER encoding. Getting
        # this wrong produces a generic 401 that looks like a bad key.
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return (msg + b"." + _b64(sig)).decode()

    def _auth(self) -> str:
        if not self._token or time.time() > self._token_exp - 60:
            self._token = self._mint()
        return self._token

    def get(self, path, tries=5):
        """GET an API path. Retries transient failures with a fresh token."""
        url = path if path.startswith("http") else API + path
        last = None
        for attempt in range(tries):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self._auth()}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as err:
                last = err
                if err.code in TRANSIENT and attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    self._token = None
                    continue
                raise ASCError(f"{err.code} on {url}: {err.read().decode()[:300]}") from err
            except urllib.error.URLError as err:
                last = err
                if attempt < tries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise ASCError(f"could not reach App Store Connect: {err}") from err
        raise ASCError(str(last))

    # -- convenience lookups -------------------------------------------------

    def apps(self):
        return self.get("/apps?limit=200").get("data", [])

    def find_app(self, bundle_id=None, app_id=None):
        """Resolve an app by bundle id or numeric Apple id."""
        for app in self.apps():
            attrs = app["attributes"]
            if app_id and app["id"] == str(app_id):
                return app
            if bundle_id and attrs.get("bundleId") == bundle_id:
                return app
        return None

    def versions(self, app_id, limit=5):
        return self.get(f"/apps/{app_id}/appStoreVersions?limit={limit}").get("data", [])

    def build_for_version(self, version_id):
        try:
            return (self.get(f"/appStoreVersions/{version_id}/build") or {}).get("data")
        except ASCError:
            return None

    def localizations(self, version_id):
        return self.get(
            f"/appStoreVersions/{version_id}/appStoreVersionLocalizations?limit=50"
        ).get("data", [])

    def screenshot_sets(self, localization_id):
        return self.get(
            f"/appStoreVersionLocalizations/{localization_id}/appScreenshotSets?limit=50"
        ).get("data", [])

    def screenshots(self, set_id):
        return self.get(f"/appScreenshotSets/{set_id}/appScreenshots?limit=50").get("data", [])

    def certificates(self):
        return self.get("/certificates?limit=200").get("data", [])

    def profiles(self):
        return self.get("/profiles?limit=200").get("data", [])
