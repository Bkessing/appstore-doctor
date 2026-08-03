"""Local code-signing checks. No credentials required.

Every check here exists because the error macOS or Xcode actually prints names
a symptom, not a cause:

  * A missing Apple Distribution certificate surfaces as
    "exportArchive No Accounts" and "No signing certificate iOS Distribution
    found" — which reads like Xcode is signed out.
  * A locked login keychain surfaces as "CodeSign ... errSecInternalComponent"
    on whatever framework happened to be signed first — which reads like a
    dependency problem.
  * A provisioning profile bound to a revoked certificate surfaces as
    "doesn't include signing certificate", and once you delete it, as
    "No profiles for '<bundle>' were found" — which reads like the profile is
    missing from disk when it is sitting right there.

Diagnose from state, never from the log text.
"""

import datetime
import glob
import os
import plistlib
import re
import subprocess

from ..report import Finding, OK, WARN, FAIL

PROFILE_DIRS = [
    os.path.expanduser("~/Library/Developer/Xcode/UserData/Provisioning Profiles"),
    os.path.expanduser("~/Library/MobileDevice/Provisioning Profiles"),
]

LOGIN_KEYCHAIN = os.path.expanduser("~/Library/Keychains/login.keychain-db")


def _run(args, timeout=20):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"


def check_identities(report):
    """Are Development AND Distribution signing identities present?"""
    code, out, _ = _run(["security", "find-identity", "-v", "-p", "codesigning"])
    if code != 0:
        report.skip("signing.identities", "could not query the keychain",
                    detail="`security find-identity` failed; is this macOS?")
        return {}

    identities = {}
    for line in out.splitlines():
        m = re.search(r'\)\s+([0-9A-F]{40})\s+"([^"]+)"', line)
        if m:
            identities[m.group(2)] = m.group(1)

    has_dist = any(n.startswith("Apple Distribution") or "iPhone Distribution" in n
                   for n in identities)
    has_dev = any(n.startswith("Apple Development") or "iPhone Developer" in n
                  for n in identities)

    names = "\n".join(f"- {n}" for n in identities) or "(none)"

    if not identities:
        report.fail(
            "signing.identities", "no code-signing identities in the keychain",
            detail=names,
            fix="Create a distribution certificate, then re-run.\n"
                "fastlane cert  (or Xcode > Settings > Accounts > Manage Certificates)",
        )
    elif not has_dist:
        report.fail(
            "signing.identities", "no Apple Distribution identity (release export will fail)",
            detail=names,
            fix="This is what 'exportArchive No Accounts' actually means.\n"
                "Create one: fastlane signing_cert (ios-release-kit), or fastlane cert,\n"
                "or check whether it was revoked in the developer portal\n"
                "(Certificates, Identifiers & Profiles).",
        )
    elif not has_dev:
        report.warn("signing.identities", "Distribution present, no Development identity",
                    detail=names,
                    fix="Fine for release builds; device debugging will fail.")
    else:
        report.ok("signing.identities",
                  f"{len(identities)} identities present (Development + Distribution)",
                  detail=names)
    return identities


def check_keychain(report):
    """Is the login keychain unlocked? A locked keychain breaks codesign."""
    if not os.path.exists(LOGIN_KEYCHAIN):
        report.skip("signing.keychain", "login keychain not found at the default path")
        return
    code, out, err = _run(["security", "show-keychain-info", LOGIN_KEYCHAIN])
    if code == 0:
        report.ok("signing.keychain", "login keychain is unlocked",
                  detail=(out or err).strip())
    else:
        report.fail(
            "signing.keychain", "login keychain is LOCKED",
            detail=(err or out).strip(),
            fix="codesign will fail with errSecInternalComponent on a random framework.\n"
                "Unlock it in a terminal you control (it prompts for your Mac password):\n"
                "  security unlock-keychain ~/Library/Keychains/login.keychain-db\n"
                "This also fixes `git push` failing with -25293.",
        )


def _decode_profile(path):
    code, out, _ = _run(["security", "cms", "-D", "-i", path])
    if code != 0 or not out.strip():
        return None
    try:
        return plistlib.loads(out.encode("utf-8", "replace"))
    except Exception:
        return None


def check_profiles(report, identities, bundle_id=None):
    """Are local provisioning profiles valid, unexpired, and bound to a cert we hold?

    The subtle failure: a profile embeds the certificates that existed when it
    was created. Replace the certificate and every existing profile silently
    stops working, while still sitting on disk looking healthy.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    found = []
    for directory in PROFILE_DIRS:
        found.extend(glob.glob(os.path.join(directory, "*.mobileprovision")))

    if not found:
        report.warn(
            "signing.profiles", "no provisioning profiles installed locally",
            fix="Xcode can create development profiles automatically. For an App Store\n"
                "export you need a distribution profile — automatic signing at the export\n"
                "step requires an Apple ID signed into Xcode, which a headless run lacks.",
        )
        return

    held = set(identities.values())
    rows, problems = [], 0
    # The same profile is normally installed in both the Xcode 15+ UserData path
    # and the legacy MobileDevice path. Dedupe on UUID so it is reported once.
    seen_uuids = set()

    for path in sorted(set(found)):
        data = _decode_profile(path)
        if not data:
            continue
        uuid = data.get("UUID")
        if uuid and uuid in seen_uuids:
            continue
        if uuid:
            seen_uuids.add(uuid)
        name = data.get("Name", os.path.basename(path))
        expires = data.get("ExpirationDate")
        entitlements = data.get("Entitlements") or {}
        app_id = entitlements.get("application-identifier", "")
        profile_bundle = app_id.split(".", 1)[1] if "." in app_id else ""

        if bundle_id and profile_bundle not in ("*", bundle_id):
            continue

        notes = []
        expired = False
        if isinstance(expires, datetime.datetime):
            now = datetime.datetime.now(datetime.timezone.utc)
            exp = expires if expires.tzinfo else expires.replace(tzinfo=datetime.timezone.utc)
            if exp < now:
                expired = True
                notes.append("EXPIRED")
            elif (exp - now).days < 30:
                notes.append(f"expires in {(exp - now).days}d")

        # The important one: does this profile reference a certificate we hold?
        # `security find-identity` reports SHA-1 fingerprints, so match on those.
        fingerprints = set()
        for der in data.get("DeveloperCertificates", []) or []:
            try:
                cert = x509.load_der_x509_certificate(bytes(der))
                fingerprints.add(cert.fingerprint(hashes.SHA1()).hex().upper())
            except Exception:
                continue

        orphaned = bool(fingerprints) and not (fingerprints & held)
        if orphaned:
            notes.append("bound to a certificate NOT in this keychain")

        if expired or orphaned:
            problems += 1
        rows.append(f"- {name}" + (f"  [{', '.join(notes)}]" if notes else ""))

    if not rows:
        report.warn("signing.profiles",
                    f"no profiles matching {bundle_id}" if bundle_id else "no readable profiles")
        return

    detail = "\n".join(rows)
    if problems:
        report.fail(
            "signing.profiles", f"{problems} of {len(rows)} profiles are expired or orphaned",
            detail=detail,
            fix="A profile bound to a replaced/revoked certificate fails export with\n"
                "\"doesn't include signing certificate\". Deleting it without creating a\n"
                "replacement then fails with \"No profiles were found\", which is misleading:\n"
                "the profile is not missing, automatic signing just cannot authenticate.\n"
                "Delete the stale file and create a new profile bound to your current cert.",
        )
    else:
        report.ok("signing.profiles", f"{len(rows)} profile(s) valid and bound to held certs",
                  detail=detail)


def run(report, bundle_id=None):
    identities = check_identities(report)
    check_keychain(report)
    check_profiles(report, identities, bundle_id=bundle_id)
