"""Cross-check the certificates on your Apple developer account against the
ones actually usable on this Mac. Requires an ASC API key. Read-only.

This catches the most common real-world signing failure there is, and neither
side can see it alone:

  `security find-identity` only lists certificates whose PRIVATE KEY is in the
  keychain. The App Store Connect API lists what your account has. A
  certificate that exists on the account but has no matching private key here
  is invisible to both — the portal says everything is fine, and the build
  fails with:

      No matching codesigning identity found: No codesigning identities
      (i.e. certificate and private key pairs) matching ... were found

That happens whenever a certificate was created on another machine, a Mac was
reinstalled without exporting a .p12, or a teammate holds the key. Diffing the
two sides is the only way to see it.
"""

import base64
import datetime
import re
import subprocess

from ..asc import ASCError

DISTRIBUTION_TYPES = {"DISTRIBUTION", "IOS_DISTRIBUTION", "MAC_APP_DISTRIBUTION"}
EXPIRY_WARN_DAYS = 30


def parse_apple_date(raw):
    """Parse an App Store Connect timestamp, or return None.

    Apple currently returns `2027-08-01T18:29:30.000+00:00`, which
    `fromisoformat` handles on Python 3.9. It has also been observed emitting
    the `+0000` and `Z` spellings, which it does not. Returning None rather
    than swallowing the error lets the caller SAY it could not read the date
    instead of silently deciding nothing is expiring — a format change should
    be visible, not quietly disable the check.
    """
    if not raw:
        return None
    text = str(raw).strip()
    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text[:-1] + "+00:00")
    # +0000 -> +00:00
    if len(text) >= 5 and text[-5] in "+-" and ":" not in text[-5:]:
        candidates.append(text[:-2] + ":" + text[-2:])
    for candidate in candidates:
        try:
            parsed = datetime.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed
    return None


def _local_fingerprints():
    """SHA-1 fingerprints of certs on this Mac that have a usable private key."""
    try:
        proc = subprocess.run(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            capture_output=True, text=True, timeout=20,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return {m.group(1).upper()
            for m in re.finditer(r"\)\s+([0-9A-Fa-f]{40})\s+\"", proc.stdout)}


def run(report, client):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    try:
        certs = client.certificates()
    except ASCError as err:
        report.skip("certs.sync", f"could not list account certificates ({err})")
        return

    if not certs:
        report.fail(
            "certs.sync", "your developer account has NO certificates",
            fix="Release builds need an Apple Distribution certificate. Create one with\n"
                "`fastlane cert`, or in the developer portal under Certificates,\n"
                "Identifiers & Profiles.",
        )
        return

    local = _local_fingerprints()
    now = datetime.datetime.now(datetime.timezone.utc)

    rows, missing_key, expired, expiring, unreadable = [], [], [], [], []

    for cert in certs:
        attrs = cert["attributes"]
        kind = attrs.get("certificateType")
        name = attrs.get("displayName") or kind

        fingerprint = None
        content = attrs.get("certificateContent")
        if content:
            try:
                der = base64.b64decode(content)
                parsed = x509.load_der_x509_certificate(der)
                fingerprint = parsed.fingerprint(hashes.SHA1()).hex().upper()
            except Exception:
                fingerprint = None

        notes = []

        raw_expiry = attrs.get("expirationDate")
        if raw_expiry:
            exp = parse_apple_date(raw_expiry)
            if exp is None:
                unreadable.append(name)
                notes.append(f"unreadable expiry {raw_expiry!r}")
            else:
                days = (exp - now).days
                if days < 0:
                    expired.append(name)
                    notes.append("EXPIRED")
                elif days < EXPIRY_WARN_DAYS:
                    expiring.append((name, days))
                    notes.append(f"expires in {days}d")

        if local is not None and fingerprint and fingerprint not in local:
            missing_key.append((name, kind))
            notes.append("no private key on this Mac")

        rows.append(f"- {kind}: {name}" + (f"  [{', '.join(notes)}]" if notes else ""))

    detail = "\n".join(rows)

    if local is None:
        report.skip("certs.sync", "could not read the local keychain to compare",
                    detail=detail)
        return

    if missing_key:
        blocking = [n for n, k in missing_key if k in DISTRIBUTION_TYPES]
        names = ", ".join(n for n, _ in missing_key)
        finding = report.fail if blocking else report.warn
        finding(
            "certs.sync",
            f"{len(missing_key)} account certificate(s) have no private key here: {names}",
            detail=detail,
            fix="The certificate exists on your account but this Mac cannot sign with it.\n"
                "You will get \"No codesigning identities ... were found\" even though the\n"
                "portal looks correct.\n"
                "Either import the .p12 exported from the Mac that created it, or create a\n"
                "new certificate here (note Apple allows a limited number per type, so you\n"
                "may need to revoke the unusable one first).",
        )
    elif expired:
        report.fail(
            "certs.sync", f"{len(expired)} certificate(s) expired: {', '.join(expired)}",
            detail=detail,
            fix="Expired certificates surface as CSSMERR_TP_CERT_EXPIRED. Create a\n"
                "replacement, then regenerate any provisioning profile bound to the old one\n"
                "— profiles embed the certs that existed when they were made.",
        )
    elif expiring:
        report.warn(
            "certs.sync",
            "; ".join(f"{n} expires in {d}d" for n, d in expiring),
            detail=detail,
            fix="Renew before it lapses. Replacing a certificate invalidates every\n"
                "provisioning profile bound to it, so plan to regenerate those too.",
        )
    elif unreadable:
        # Never report "all good" when a date could not be read — that would be
        # indistinguishable from a genuinely healthy account.
        report.warn(
            "certs.sync",
            f"could not read the expiry date on {len(unreadable)} certificate(s)",
            detail=detail,
            fix="Expiry could not be checked for these. Apple may have changed its date\n"
                "format; verify expiry by hand in the developer portal.",
        )
    else:
        report.ok("certs.sync",
                  f"{len(certs)} account certificate(s), all valid and usable here",
                  detail=detail)
