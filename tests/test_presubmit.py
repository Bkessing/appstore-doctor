"""Pre-submit gate: the checks a submit lane runs before it submits.

Every case here is a submit that failed on 2026-11-18 after a release-engineer
said it was ready. The fixtures in tests/fixtures/ mirror App Store Connect's
response shapes for those apps, with ids and bundle ids replaced. They were
written from Apple's documented resource shapes and the exact error text in
that day's reports, not captured from a live account: re-record them from a
real run when one is available.

Run with: python3 -m unittest discover tests
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from appstore_doctor import __main__ as cli
from appstore_doctor.asc import ASCError, Client
from appstore_doctor.checks import listing, readiness
from appstore_doctor.report import Report, OK, WARN, FAIL

from tests.test_checks import FakeClient, status_of, version

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return json.load(handle)


def finding(report, check):
    return next((f for f in report.findings if f.check == check), None)


class RecordedClient(FakeClient):
    """FakeClient over a recorded fixture: unknown paths still raise."""

    def __init__(self, recorded, extra=None):
        routes = dict(recorded["routes"])
        routes.update(extra or {})
        super().__init__(routes)
        self.agreement_errors = []


class TestEditableAppInfo(unittest.TestCase):
    def test_live_record_listed_first_is_skipped(self):
        rec = fixture("mint-1.1-privacy-urls.json")
        info = readiness.editable_app_info(RecordedClient(rec), rec["app"])
        self.assertEqual(info["id"], "info-edit")

    def test_single_record_is_used_whatever_its_state(self):
        client = FakeClient({"/apps/9/appInfos": {"data": [
            {"id": "only", "attributes": {"state": "READY_FOR_DISTRIBUTION"}}]}})
        self.assertEqual(readiness.editable_app_info(client, {"id": "9"})["id"], "only")


class TestPrivacyPerLocale(unittest.TestCase):
    def test_mint_six_locales_without_url_fail(self):
        # Mint Condition 1.1: the submit returned 409 for these six locales.
        # Reading infos[0] (the live record) used to pass this.
        rec = fixture("mint-1.1-privacy-urls.json")
        report = Report()
        readiness.check_privacy_policy(report, RecordedClient(rec), rec["app"])
        f = finding(report, "readiness.privacy_policy")
        self.assertEqual(f.status, FAIL)
        for locale in ("es-MX", "es-ES", "de-DE", "fr-FR", "ko", "pt-BR"):
            self.assertIn(locale, f.summary)
        self.assertNotIn("en-US", f.summary)


class TestAgeRatingAnswers(unittest.TestCase):
    def test_unanswered_social_media_fails(self):
        rec = fixture("cardhabit-2.0-presubmit.json")
        report = Report()
        readiness.check_age_rating(report, RecordedClient(rec), rec["app"])
        f = finding(report, "readiness.age_rating")
        self.assertEqual(f.status, FAIL)
        self.assertEqual(f.data["unanswered"], ["socialMedia"])

    def test_complete_declaration_passes_with_optional_nulls(self):
        rec = fixture("mint-1.1-privacy-urls.json")
        report = Report()
        readiness.check_age_rating(report, RecordedClient(rec), rec["app"])
        self.assertEqual(status_of(report, "readiness.age_rating"), OK)

    def test_korea_grac_number_null_is_not_a_question(self):
        # Mac run 2026-11-18: CardHabit 2.0, Mint 1.1 and Lab Tycoon 1.1 were all
        # accepted into review with gracRatingClassificationNumber null.
        self.assertEqual(readiness.unanswered_age_rating_fields(
            {"socialMedia": False, "gracRatingClassificationNumber": None}), [])

    def test_follow_up_required_only_when_parent_is_yes(self):
        self.assertEqual(readiness.unanswered_age_rating_fields(
            {"socialMedia": False, "socialMediaAgeRestricted": None}), [])
        self.assertEqual(readiness.unanswered_age_rating_fields(
            {"socialMedia": True, "socialMediaAgeRestricted": None}),
            ["socialMediaAgeRestricted"])

    def test_a_brand_new_question_is_caught(self):
        # The next question Apple adds will look exactly like this.
        self.assertEqual(readiness.unanswered_age_rating_fields(
            {"gambling": False, "someFutureQuestion": None}), ["someFutureQuestion"])


class TestNameSubtitle(unittest.TestCase):
    def test_shared_words_warn_including_plurals(self):
        rec = fixture("cardhabit-2.0-presubmit.json")
        report = Report()
        readiness.check_name_subtitle(report, RecordedClient(rec), rec["app"])
        f = finding(report, "readiness.name_subtitle")
        self.assertEqual(f.status, WARN, "a repeat is wasted, never blocking")
        self.assertIn("card", f.detail)
        self.assertIn("daily", f.detail)

    def test_filler_words_do_not_count(self):
        self.assertEqual(readiness._words("Pack Opening & Grading Tycoon")
                         & readiness._words("Mint Condition: Card Shop Sim"), set())
        # Two-letter locale filler ("de", "la") is not a shared keyword.
        self.assertEqual(readiness._words("Lab Tycoon de IA")
                         & readiness._words("Juego de empresa"), set())
        self.assertEqual(readiness._words("The Game of Life")
                         & readiness._words("A game for the ages"), {"game"})


class TestIapSubmission(unittest.TestCase):
    def _run(self, with_iaps=False):
        rec = fixture("cardhabit-2.0-presubmit.json")
        report = Report()
        readiness.check_iaps(report, RecordedClient(rec), rec["app"],
                             rec["version"], with_iaps=with_iaps)
        return report

    def test_first_non_consumable_left_out_fails(self):
        # FIRST_NON_CONSUMABLE_MUST_BE_SUBMITTED_ON_VERSION, 2026-11-18.
        f = finding(self._run(), "readiness.iap.submission")
        self.assertEqual(f.status, FAIL)
        self.assertIn("com.example.ch.deck", f.detail)

    def test_submit_with_iaps_carries_it(self):
        # The parked Elite subscription remains, but Pro is approved, so it is
        # a later product of its kind: a warning, not a block.
        f = finding(self._run(with_iaps=True), "readiness.iap.submission")
        self.assertEqual(f.status, WARN)
        self.assertIn("com.example.ch.elite", f.detail)
        self.assertNotIn("com.example.ch.deck", f.detail)

    def test_first_subscription_is_not_covered_by_with_iaps(self):
        client = FakeClient({
            "/apps/123/inAppPurchasesV2": {"data": []},
            "/apps/123/subscriptionGroups": {"data": [{"id": "g"}]},
            "/subscriptionGroups/g/subscriptions": {"data": [
                {"attributes": {"productId": "pro", "state": "READY_TO_SUBMIT"}}]},
        })
        report = Report()
        readiness.check_iaps(report, client, {"id": "123"},
                             version("PREPARE_FOR_SUBMISSION"), with_iaps=True)
        self.assertEqual(status_of(report, "readiness.iap.submission"), FAIL)

    def test_live_version_is_not_a_submission(self):
        rec = fixture("cardhabit-2.0-presubmit.json")
        report = Report()
        readiness.check_iaps(report, RecordedClient(rec), rec["app"],
                             version("READY_FOR_SALE"))
        self.assertIsNone(finding(report, "readiness.iap.submission"))


class TestAgreements(unittest.TestCase):
    def test_client_records_contract_error_code(self):
        body = json.dumps(fixture("contract-error.json")).encode()
        client = Client.__new__(Client)
        client.agreement_errors = []
        client._auth = lambda: "token"
        err = urllib.error.HTTPError("https://x/v1/reviewSubmissionItems", 409,
                                     "Conflict", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ASCError):
                client.get("/apps/1/inAppPurchasesV2", tries=1)
        self.assertEqual(len(client.agreement_errors), 1)
        self.assertIn("MISSING_VALID_CONTRACT", client.agreement_errors[0])

    def test_recorded_agreement_error_fails(self):
        rec = fixture("cardhabit-2.0-presubmit.json")
        client = RecordedClient(rec)
        client.agreement_errors = ["403 on /v1/apps: REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED"]
        report = Report()
        readiness.check_agreements(report, client, rec["app"], rec["version"])
        self.assertEqual(status_of(report, "readiness.agreements"), FAIL)

    def test_products_going_to_review_get_a_reminder(self):
        rec = fixture("cardhabit-2.0-presubmit.json")
        report = Report()
        readiness.check_agreements(report, RecordedClient(rec), rec["app"], rec["version"])
        self.assertEqual(status_of(report, "readiness.agreements"), WARN)

    def test_nothing_to_sell_says_nothing(self):
        rec = fixture("mint-1.1-privacy-urls.json")
        report = Report()
        readiness.check_agreements(report, RecordedClient(rec), rec["app"], rec["version"])
        self.assertIsNone(finding(report, "readiness.agreements"))


class TestRequiredScreenshots(unittest.TestCase):
    def _client(self, sets, locale="en-US"):
        shots, defs = {}, []
        for i, (display, count) in enumerate(sets):
            defs.append({"id": f"s{i}", "attributes": {"screenshotDisplayType": display}})
            shots[f"s{i}"] = [{"attributes": {"imageAsset": {}}}] * count
        return FakeClient({"__locs__": [{"id": "l", "attributes": {"locale": locale}}],
                           "__sets__": defs, "__shots__": shots})

    def test_only_old_iphone_sizes_fail(self):
        report = Report()
        listing.check_screenshots(report, self._client([("APP_IPHONE_61", 5)]), version())
        self.assertEqual(status_of(report, "listing.screenshots.required"), FAIL)

    def test_six_point_five_alone_is_enough(self):
        report = Report()
        listing.check_screenshots(report, self._client([("APP_IPHONE_65", 5)]), version())
        self.assertEqual(status_of(report, "listing.screenshots.required"), OK)

    def test_ipad_app_without_13_inch_fails(self):
        report = Report()
        listing.check_screenshots(report, self._client(
            [("APP_IPHONE_67", 5), ("APP_IPAD_PRO_3GEN_11", 5)]), version())
        f = finding(report, "listing.screenshots.required")
        self.assertEqual(f.status, FAIL)
        self.assertIn("iPad 13in", f.summary)

    def test_primary_locale_is_the_one_checked(self):
        client = FakeClient({
            "__locs__": [{"id": "en", "attributes": {"locale": "en-US"}},
                         {"id": "ja", "attributes": {"locale": "ja"}}],
            "__sets__": [{"id": "s", "attributes": {"screenshotDisplayType": "APP_IPHONE_67"}}],
            "__shots__": {"s": [{"attributes": {"imageAsset": {}}}]},
        })
        report = Report()
        listing.check_screenshots(report, client, version(),
                                  {"attributes": {"primaryLocale": "ja"}})
        self.assertIn("ja", finding(report, "listing.screenshots.required").summary)


class TestPresubmitStuckSubmission(unittest.TestCase):
    def test_lane_cancels_it_so_it_only_warns(self):
        client = FakeClient({"/reviewSubmissions": {"data": [
            {"attributes": {"state": "UNRESOLVED_ISSUES", "submittedDate": "2026-11-01"}}]}})
        report = Report()
        listing.check_stuck_submission(report, client, {"id": "1"}, presubmit=True)
        self.assertEqual(status_of(report, "listing.submission"), WARN)


class TestPresubmitCli(unittest.TestCase):
    """End to end through main(): the exit code is what the submit lane obeys."""

    def _main(self, rec, *flags):
        client = RecordedClient(rec, {
            "/apps/222/appAvailabilityV2": {"data": {"attributes": {}}},
            "/reviewSubmissions": {"data": []},
            "/appStoreVersions/ver-20/appStoreReviewDetail": {"data": {"attributes": {
                "demoAccountRequired": False, "contactEmail": "r@example.com"}}},
            "__versions__": [rec["version"]],
            "__build__": {"attributes": {"version": "1", "usesNonExemptEncryption": False}},
            "__locs__": [{"id": "l", "attributes": {
                "locale": "en-US",
                "description": "Pro renews at $4.99 per month. Privacy policy: "
                               "https://example.com/privacy Terms of Use (EULA): "
                               "https://example.com/terms"}}],
            "__sets__": [{"id": "s", "attributes": {"screenshotDisplayType": "APP_IPHONE_67"}}],
            "__shots__": {"s": [{"attributes": {"imageAsset": {"width": 1320, "height": 2868}}}]},
        })
        client.find_app = lambda bundle_id=None, app_id=None: rec["app"]
        out = io.StringIO()
        with mock.patch.object(cli, "Client", return_value=client), \
                mock.patch.object(cli.signing, "run") as signing_run, \
                mock.patch.object(cli.certificates, "run") as certs_run, \
                mock.patch.object(cli.surfaces, "run") as surfaces_run, \
                mock.patch("sys.stdout", out):
            code = cli.main(["--app-id", "222", "--presubmit", "--json", *flags])
        self.assertFalse(signing_run.called or certs_run.called or surfaces_run.called,
                         "presubmit must not run local signing, certificate or surfaces checks")
        return code, json.loads(out.getvalue())

    def test_cardhabit_2_0_is_stopped_with_every_fix_listed(self):
        code, data = self._main(fixture("cardhabit-2.0-presubmit.json"))
        self.assertEqual(code, 1)
        failed = {f["check"] for f in data["findings"] if f["status"] == "fail"}
        self.assertEqual(failed, {"readiness.age_rating", "readiness.iap.submission"})
        warned = {f["check"] for f in data["findings"] if f["status"] == "warn"}
        self.assertIn("readiness.name_subtitle", warned)
        self.assertIn("readiness.agreements", warned)

    def test_with_iaps_still_stops_on_age_rating(self):
        code, data = self._main(fixture("cardhabit-2.0-presubmit.json"), "--with-iaps")
        self.assertEqual(code, 1)
        failed = {f["check"] for f in data["findings"] if f["status"] == "fail"}
        self.assertEqual(failed, {"readiness.age_rating"})


if __name__ == "__main__":
    unittest.main()
