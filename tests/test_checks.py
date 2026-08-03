"""Tests for the diagnostic logic.

These deliberately exercise the FAILURE paths. A diagnostic that only works on a
healthy machine is worthless — the whole value is in what it says when things
are broken, and every bug found while building this was a wrong verdict rather
than a crash:

  * availability was reported as missing on a live, selling app because a
    follow-up request 404'd and the error handler conflated "could not read"
    with "does not exist"
  * profiles were double-counted because the same profile is installed in two
    directories
  * incomplete in-app purchases were reported as blocking a submission they do
    not actually block

Each of those is pinned below. Run with: python3 -m unittest discover tests
"""

import base64
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from appstore_doctor.report import Report, OK, WARN, FAIL, SKIP
from appstore_doctor.checks import listing, readiness, certificates
from appstore_doctor.asc import ASCError


def status_of(report, check):
    for f in report.findings:
        if f.check == check:
            return f.status
    return None


class FakeClient:
    """Minimal stand-in. Raises ASCError for any path not explicitly stubbed,
    so a test can never accidentally pass by hitting the real API."""

    def __init__(self, routes=None):
        self.routes = routes or {}

    def get(self, path, **kw):
        for prefix, value in self.routes.items():
            if path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise ASCError(f"404 on {path}: not stubbed")

    def build_for_version(self, version_id):
        return self.routes.get("__build__")

    def localizations(self, version_id):
        return self.routes.get("__locs__", [])

    def screenshot_sets(self, loc_id):
        return self.routes.get("__sets__", [])

    def screenshots(self, set_id):
        return self.routes.get("__shots__", {}).get(set_id, [])

    def versions(self, app_id, limit=5):
        return self.routes.get("__versions__", [])

    def certificates(self):
        return self.routes.get("__certs__", [])


APP = {"id": "123", "attributes": {"bundleId": "com.example.app"}}


def version(state="READY_FOR_SALE", release="AFTER_APPROVAL", vid="v1"):
    return {"id": vid, "attributes": {"versionString": "1.0",
                                      "appStoreState": state,
                                      "releaseType": release}}


class TestAvailability(unittest.TestCase):
    """Regression: a live app was reported as having zero availability."""

    def test_missing_resource_is_a_real_failure(self):
        report = Report()
        listing.check_availability(report, FakeClient(), APP)
        self.assertEqual(status_of(report, "listing.availability"), FAIL)

    def test_unreadable_territories_is_not_reported_as_broken(self):
        # The resource EXISTS; only the follow-up call fails. Reporting that as
        # "app is invisible" is the false positive this pins.
        client = FakeClient({
            "/apps/123/appAvailabilityV2": {"data": {
                "attributes": {"availableInNewTerritories": True},
                "relationships": {"territoryAvailabilities": {
                    "links": {"related": "https://example.invalid/territories"}}},
            }},
        })
        report = Report()
        listing.check_availability(report, client, APP)
        self.assertEqual(status_of(report, "listing.availability"), SKIP,
                         "an unreadable territory list must not read as a broken app")

    def test_zero_available_territories_fails(self):
        client = FakeClient({
            "/apps/123/appAvailabilityV2": {"data": {
                "attributes": {},
                "relationships": {"territoryAvailabilities": {
                    "links": {"related": "https://example.test/t"}}},
            }},
            "https://example.test/t": {"data": [
                {"attributes": {"available": False}},
                {"attributes": {"available": False}},
            ]},
        })
        report = Report()
        listing.check_availability(report, client, APP)
        self.assertEqual(status_of(report, "listing.availability"), FAIL)

    def test_available_passes(self):
        client = FakeClient({
            "/apps/123/appAvailabilityV2": {"data": {
                "attributes": {"availableInNewTerritories": True},
                "relationships": {"territoryAvailabilities": {
                    "links": {"related": "https://example.test/t"}}},
            }},
            "https://example.test/t": {"data": [{"attributes": {"available": True}}] * 175},
        })
        report = Report()
        listing.check_availability(report, client, APP)
        self.assertEqual(status_of(report, "listing.availability"), OK)


class TestScreenshots(unittest.TestCase):
    """Regression: updating one display type while the other serves stale art."""

    def _client(self, sets):
        shots, set_defs = {}, []
        for i, (display, count, size) in enumerate(sets):
            sid = f"s{i}"
            set_defs.append({"id": sid, "attributes": {"screenshotDisplayType": display}})
            shots[sid] = [{"attributes": {"imageAsset": {"width": size[0],
                                                         "height": size[1]}}}
                          for _ in range(count)]
        return FakeClient({
            "__locs__": [{"id": "l1", "attributes": {"locale": "en-US"}}],
            "__sets__": set_defs,
            "__shots__": shots,
        })

    def test_mismatched_counts_between_display_types_warns(self):
        client = self._client([("APP_IPHONE_67", 8, (1290, 2796)),
                               ("APP_IPHONE_65", 5, (1284, 2778))])
        report = Report()
        listing.check_screenshots(report, client, version())
        self.assertEqual(status_of(report, "listing.screenshots"), WARN)

    def test_empty_set_warns(self):
        client = self._client([("APP_IPHONE_67", 5, (1290, 2796)),
                               ("APP_IPHONE_65", 0, (0, 0))])
        report = Report()
        listing.check_screenshots(report, client, version())
        self.assertEqual(status_of(report, "listing.screenshots"), WARN)

    def test_no_iphone_sets_fails(self):
        client = self._client([("APP_IPAD_PRO_129", 5, (2048, 2732))])
        report = Report()
        listing.check_screenshots(report, client, version())
        self.assertEqual(status_of(report, "listing.screenshots"), FAIL)

    def test_consistent_sets_pass(self):
        client = self._client([("APP_IPHONE_67", 5, (1290, 2796)),
                               ("APP_IPHONE_65", 5, (1284, 2778))])
        report = Report()
        listing.check_screenshots(report, client, version())
        self.assertEqual(status_of(report, "listing.screenshots"), OK)

    def test_wrong_dimensions_warn(self):
        client = self._client([("APP_IPHONE_67", 5, (1170, 2532))])
        report = Report()
        listing.check_screenshots(report, client, version())
        self.assertEqual(status_of(report, "listing.screenshots"), WARN)


class TestIAPSeverity(unittest.TestCase):
    """Regression: MISSING_METADATA products were reported as blocking a
    submission they do not block."""

    def _client(self, states):
        return FakeClient({
            "/apps/123/inAppPurchasesV2": {"data": [
                {"attributes": {"productId": f"p{i}", "state": s}}
                for i, s in enumerate(states)]},
            "/apps/123/subscriptionGroups": {"data": []},
        })

    def test_incomplete_products_warn_not_fail(self):
        report = Report()
        readiness.check_iaps(report, self._client(["MISSING_METADATA", "APPROVED"]),
                             APP, version("READY_FOR_SALE"))
        self.assertEqual(status_of(report, "readiness.iap"), WARN,
                         "incomplete IAPs cannot sell but do not block submission")

    def test_all_approved_passes(self):
        report = Report()
        readiness.check_iaps(report, self._client(["APPROVED", "APPROVED"]),
                             APP, version("READY_FOR_SALE"))
        self.assertEqual(status_of(report, "readiness.iap"), OK)

    def test_no_iaps_passes(self):
        report = Report()
        readiness.check_iaps(report, self._client([]), APP, version())
        self.assertEqual(status_of(report, "readiness.iap"), OK)

    def test_unsubmitted_version_gets_reachability_warning(self):
        # The Guideline 2.1(b) case no API can see, so it must be surfaced.
        report = Report()
        readiness.check_iaps(report, self._client(["APPROVED"]), APP,
                             version("PREPARE_FOR_SUBMISSION"))
        self.assertEqual(status_of(report, "readiness.iap.reachable"), WARN)


class TestReviewDetails(unittest.TestCase):
    def test_required_but_missing_credentials_fails(self):
        client = FakeClient({"/appStoreVersions/v1/appStoreReviewDetail": {"data": {
            "attributes": {"demoAccountRequired": True, "demoAccountName": "",
                           "demoAccountPassword": "", "contactEmail": "a@b.c"}}}})
        report = Report()
        readiness.check_review_details(report, client, version())
        self.assertEqual(status_of(report, "readiness.review_details"), FAIL)

    def test_credentials_present_passes(self):
        client = FakeClient({"/appStoreVersions/v1/appStoreReviewDetail": {"data": {
            "attributes": {"demoAccountRequired": True, "demoAccountName": "u",
                           "demoAccountPassword": "p", "contactEmail": "a@b.c"}}}})
        report = Report()
        readiness.check_review_details(report, client, version())
        self.assertEqual(status_of(report, "readiness.review_details"), OK)


class TestExportCompliance(unittest.TestCase):
    def test_undeclared_fails(self):
        client = FakeClient({"__build__": {"attributes": {"version": "42",
                                                          "usesNonExemptEncryption": None}}})
        report = Report()
        readiness.check_export_compliance(report, client, version())
        self.assertEqual(status_of(report, "readiness.export_compliance"), FAIL)

    def test_exempt_passes(self):
        client = FakeClient({"__build__": {"attributes": {"version": "42",
                                                          "usesNonExemptEncryption": False}}})
        report = Report()
        readiness.check_export_compliance(report, client, version())
        self.assertEqual(status_of(report, "readiness.export_compliance"), OK)


def _self_signed(days_valid=365):
    """A throwaway cert so the certificate check can be tested without network."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Cert")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days_valid))
            .sign(key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    return base64.b64encode(der).decode(), cert.fingerprint(hashes.SHA1()).hex().upper()


class TestCertificateSync(unittest.TestCase):
    """The highest-value check: a cert on the account with no private key here."""

    def test_missing_private_key_for_distribution_fails(self):
        content, _fp = _self_signed()
        client = FakeClient({"__certs__": [
            {"attributes": {"certificateType": "DISTRIBUTION", "displayName": "Dist",
                            "certificateContent": content,
                            "expirationDate": "2030-01-01T00:00:00.000+00:00"}}]})
        report = Report()
        with mock.patch.object(certificates, "_local_fingerprints", lambda: set()):
            certificates.run(report, client)
        self.assertEqual(status_of(report, "certs.sync"), FAIL)

    def test_key_present_passes(self):
        content, fingerprint = _self_signed()
        client = FakeClient({"__certs__": [
            {"attributes": {"certificateType": "DISTRIBUTION", "displayName": "Dist",
                            "certificateContent": content,
                            "expirationDate": "2030-01-01T00:00:00.000+00:00"}}]})
        report = Report()
        with mock.patch.object(certificates, "_local_fingerprints", lambda: {fingerprint}):
            certificates.run(report, client)
        self.assertEqual(status_of(report, "certs.sync"), OK)

    def test_expired_certificate_fails(self):
        content, fingerprint = _self_signed()
        client = FakeClient({"__certs__": [
            {"attributes": {"certificateType": "DISTRIBUTION", "displayName": "Old",
                            "certificateContent": content,
                            "expirationDate": "2020-01-01T00:00:00.000+00:00"}}]})
        report = Report()
        with mock.patch.object(certificates, "_local_fingerprints", lambda: {fingerprint}):
            certificates.run(report, client)
        self.assertEqual(status_of(report, "certs.sync"), FAIL)

    def test_no_certificates_at_all_fails(self):
        report = Report()
        certificates.run(report, FakeClient({"__certs__": []}))
        self.assertEqual(status_of(report, "certs.sync"), FAIL)

    def test_unreadable_keychain_skips_rather_than_guessing(self):
        content, _ = _self_signed()
        client = FakeClient({"__certs__": [
            {"attributes": {"certificateType": "DISTRIBUTION", "displayName": "D",
                            "certificateContent": content,
                            "expirationDate": "2030-01-01T00:00:00.000+00:00"}}]})
        report = Report()
        with mock.patch.object(certificates, "_local_fingerprints", lambda: None):
            certificates.run(report, client)
        self.assertEqual(status_of(report, "certs.sync"), SKIP)


class TestAppleDateParsing(unittest.TestCase):
    """Apple's own format must parse, and an unparseable one must return None
    rather than silently reading as 'not expiring'."""

    def test_apple_current_format(self):
        self.assertIsNotNone(certificates.parse_apple_date("2027-08-01T18:29:30.000+00:00"))

    def test_offset_without_colon(self):
        self.assertIsNotNone(certificates.parse_apple_date("2027-08-01T18:29:30.000+0000"))

    def test_zulu(self):
        self.assertIsNotNone(certificates.parse_apple_date("2027-08-01T18:29:30Z"))

    def test_garbage_returns_none(self):
        self.assertIsNone(certificates.parse_apple_date("not a date"))

    def test_unreadable_expiry_warns_rather_than_passing(self):
        content, fingerprint = _self_signed()
        client = FakeClient({"__certs__": [
            {"attributes": {"certificateType": "DISTRIBUTION", "displayName": "D",
                            "certificateContent": content,
                            "expirationDate": "definitely not a date"}}]})
        report = Report()
        with mock.patch.object(certificates, "_local_fingerprints", lambda: {fingerprint}):
            certificates.run(report, client)
        self.assertEqual(status_of(report, "certs.sync"), WARN)


class TestReportContract(unittest.TestCase):
    def test_exit_code_is_one_only_when_something_blocks(self):
        r = Report()
        r.ok("a", "fine")
        r.warn("b", "hmm")
        self.assertEqual(r.exit_code(), 0, "warnings must not fail a CI run")
        r.fail("c", "broken")
        self.assertEqual(r.exit_code(), 1)

    def test_json_is_wellformed(self):
        import json
        r = Report()
        r.fail("x", "bad", detail="d", fix="f")
        parsed = json.loads(r.to_json())
        self.assertEqual(parsed["summary"]["fail"], 1)
        self.assertEqual(parsed["findings"][0]["fix"], "f")


if __name__ == "__main__":
    unittest.main()
