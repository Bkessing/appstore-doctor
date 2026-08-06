"""Submission-readiness checks against App Store Connect. Read-only.

These are the blockers that have nothing to do with your code. An app can build,
sign, upload and still be unsubmittable — or get rejected on the first pass —
because a field nobody looks at is empty.

Everything here was confirmed readable against the live API before it was
written; checks are not speculative.
"""

from ..asc import ASCError

# IAP/subscription states that are fine to submit alongside a binary.
IAP_OK = {"READY_TO_SUBMIT", "WAITING_FOR_REVIEW", "IN_REVIEW", "APPROVED",
          "DEVELOPER_ACTION_NEEDED", "PENDING_BINARY_APPROVAL"}
IAP_LIVE = {"APPROVED"}

NOT_SUBMITTED_STATES = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED",
                        "METADATA_REJECTED", "INVALID_BINARY", "DEVELOPER_REMOVED_FROM_SALE"}

# The app itself is on sale to the public.
ON_SALE_STATES = {"READY_FOR_SALE", "PENDING_DEVELOPER_RELEASE", "APPROVED"}


def check_privacy_policy(report, client, app):
    """A privacy policy URL is mandatory. Missing one is a metadata rejection."""
    try:
        infos = client.get(f"/apps/{app['id']}/appInfos?limit=5").get("data", [])
        if not infos:
            report.skip("readiness.privacy_policy", "no appInfo record found")
            return
        locs = client.get(
            f"/appInfos/{infos[0]['id']}/appInfoLocalizations?limit=50"
        ).get("data", [])
    except ASCError as err:
        report.skip("readiness.privacy_policy", f"could not read app info ({err})")
        return

    missing = [l["attributes"].get("locale") for l in locs
               if not (l["attributes"].get("privacyPolicyUrl") or "").strip()]

    if not locs:
        report.skip("readiness.privacy_policy", "no localizations found")
    elif missing:
        report.fail(
            "readiness.privacy_policy",
            f"no privacy policy URL set for: {', '.join(missing)}",
            fix="Every app needs a privacy policy URL, and it must resolve. Set it in\n"
                "App Store Connect > App Information > Privacy Policy URL.\n"
                "Or, with ios-release-kit:  URL=<your-url> fastlane set_privacy_policy",
        )
    else:
        report.ok("readiness.privacy_policy",
                  f"privacy policy URL set for {len(locs)} localization(s)")


def check_age_rating(report, client, app):
    """An age rating declaration must exist before a version can be submitted."""
    try:
        infos = client.get(f"/apps/{app['id']}/appInfos?limit=5").get("data", [])
        if not infos:
            report.skip("readiness.age_rating", "no appInfo record found")
            return
        rating = client.get(f"/appInfos/{infos[0]['id']}/ageRatingDeclaration")
    except ASCError as err:
        if "404" in str(err):
            report.fail(
                "readiness.age_rating", "no age rating declaration exists",
                fix="Complete the age rating questionnaire in App Store Connect >\n"
                    "App Information. A version cannot be submitted without it.",
            )
        else:
            report.skip("readiness.age_rating", f"could not read age rating ({err})")
        return

    attrs = (rating.get("data") or {}).get("attributes") or {}
    answered = [k for k, v in attrs.items() if v not in (None, "", False)]
    store_rating = ((client.get(f"/apps/{app['id']}/appInfos?limit=1")
                    .get("data") or [{}])[0].get("attributes") or {}).get("appStoreAgeRating")

    if not attrs:
        report.warn("readiness.age_rating", "age rating declaration is empty")
    else:
        report.ok(
            "readiness.age_rating",
            f"age rating declared{f' ({store_rating})' if store_rating else ''}",
            detail=f"{len(answered)} non-default answers recorded",
        )


def check_review_details(report, client, version):
    """Guideline 2.1: if the app requires a login, reviewers need credentials.

    A reviewer who cannot get past your sign-in screen rejects the build. This
    is one of the most common rejection causes and it is entirely preventable.
    """
    try:
        detail = client.get(f"/appStoreVersions/{version['id']}/appStoreReviewDetail")
    except ASCError as err:
        if "404" in str(err):
            report.warn(
                "readiness.review_details", "no App Review information set for this version",
                fix="Add review contact details, and demo account credentials if your app\n"
                    "has a sign-in. Reviewers reject builds they cannot get into.",
            )
        else:
            report.skip("readiness.review_details", f"could not read review detail ({err})")
        return

    attrs = (detail.get("data") or {}).get("attributes") or {}
    required = attrs.get("demoAccountRequired")
    name = (attrs.get("demoAccountName") or "").strip()
    password = (attrs.get("demoAccountPassword") or "").strip()
    contact = (attrs.get("contactEmail") or "").strip()

    if required and not (name and password):
        report.fail(
            "readiness.review_details",
            "demo account is marked required but credentials are missing",
            fix="Guideline 2.1. Fill in the demo account name and password in App Store\n"
                "Connect > App Review Information, and verify they actually work.",
        )
        return

    if not required:
        report.warn(
            "readiness.review_details",
            "no demo account provided (fine only if your app needs no sign-in)",
            fix="If any part of the app is behind a login, a reviewer must be able to reach\n"
                "it. Missing credentials is a very common Guideline 2.1 rejection.",
        )
    else:
        report.ok("readiness.review_details", "demo account credentials present")

    if not contact:
        report.warn("readiness.review_details.contact", "no review contact email set")


def check_iaps(report, client, app, version):
    """In-app purchases must be submitted alongside the binary, in a valid state."""
    try:
        iaps = client.get(f"/apps/{app['id']}/inAppPurchasesV2?limit=200").get("data", [])
        groups = client.get(f"/apps/{app['id']}/subscriptionGroups?limit=50").get("data", [])
        subs = []
        for group in groups:
            subs.extend(client.get(
                f"/subscriptionGroups/{group['id']}/subscriptions?limit=200"
            ).get("data", []))
    except ASCError as err:
        report.skip("readiness.iap", f"could not read in-app purchases ({err})")
        return

    products = [(i["attributes"].get("productId"), i["attributes"].get("state"))
                for i in iaps]
    products += [(s["attributes"].get("productId"), s["attributes"].get("state"))
                 for s in subs]

    if not products:
        report.ok("readiness.iap", "no in-app purchases configured")
        return

    rows = "\n".join(f"- {pid}: {state}" for pid, state in products)
    version_state = version["attributes"].get("appStoreState")

    # MISSING_METADATA does NOT block the app's submission — an incomplete
    # product simply never goes on sale. Reporting it as a blocker cries wolf on
    # a perfectly shippable app, so it is a warning: real, worth knowing, not
    # stopping you.
    incomplete = [(p, s) for p, s in products if s == "MISSING_METADATA"]
    approved = [(p, s) for p, s in products if s in IAP_LIVE]
    in_flight = [(p, s) for p, s in products
                 if s in {"WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_BINARY_APPROVAL"}]

    # An app is on sale with nothing to sell whenever ANY version is live, not
    # just the one being inspected: a newer version sitting in review does not
    # put the shipped one back in the box.
    on_sale = version_state in ON_SALE_STATES
    if not on_sale:
        try:
            others = client.get(
                f"/apps/{app['id']}/appStoreVersions?limit=10&filter[platform]=IOS"
            ).get("data", [])
            on_sale = any(v.get("attributes", {}).get("appStoreState") in ON_SALE_STATES
                          for v in others)
        except ASCError:
            pass

    if incomplete:
        report.warn(
            "readiness.iap",
            f"{len(incomplete)} of {len(products)} in-app purchases are incomplete "
            "and cannot sell",
            detail=rows,
            fix="MISSING_METADATA means the product has no localized display name,\n"
                "description or review screenshot. It will not be offered to anyone.\n"
                "Complete it, or delete it so it stops showing up here.",
        )
    elif on_sale and not approved and not in_flight:
        # The app is on sale and every product it sells is unapproved, so it
        # takes no money at all. This reads as healthy from the outside -- the
        # listing is live, downloads happen, nothing is "blocked" -- which is
        # exactly why it needs to be loud. Lab Tycoon shipped 1.0 to the store
        # on 2026-08-06 with both IAPs sitting in READY_TO_SUBMIT, and this
        # check reported PASS ("0 of 2 approved") because it only looked for
        # trouble in the not-yet-submitted states.
        report.fail(
            "readiness.iap",
            f"the app is on sale but none of its {len(products)} in-app "
            "purchases are approved — it can take no money",
            detail=rows,
            fix="The FIRST consumable and FIRST non-consumable must be submitted WITH\n"
                "an app version, so a live app carries them on its next release: one\n"
                "reviewSubmission holding the version AND each product, every item\n"
                "attached before submitted=true. Confirm each product flips to\n"
                "WAITING_FOR_REVIEW; no flip means it was never really attached.",
        )
    elif on_sale and not approved and in_flight:
        report.warn(
            "readiness.iap",
            f"the app is on sale and cannot take money yet — "
            f"{len(in_flight)} of {len(products)} product(s) are in review",
            detail=rows,
            fix="Nothing to do but wait. Until one product of each type is approved,\n"
                "the live version sells nothing.",
        )
    elif version_state in NOT_SUBMITTED_STATES and not approved:
        report.warn(
            "readiness.iap",
            f"none of the {len(products)} IAPs are approved yet",
            detail=rows,
            fix="First-time IAPs must be submitted WITH the binary — attach them to the\n"
                "version before submitting, or review will not see them.",
        )
    else:
        report.ok("readiness.iap",
                  f"{len(approved)} of {len(products)} in-app purchase(s) approved",
                  detail=rows)

    if products and version_state in NOT_SUBMITTED_STATES:
        report.warn(
            "readiness.iap.reachable",
            "verify a reviewer can actually REACH each purchase in the app",
            fix="IAPs gated behind progression, a paywall the reviewer never hits, or a\n"
                "feature flag are a common Guideline 2.1(b) rejection: 'we were unable to\n"
                "locate the In-App Purchases.' No API can detect this — check it by hand\n"
                "on a fresh install before submitting.",
        )


def check_export_compliance(report, client, version):
    """A build with no encryption declaration cannot be submitted."""
    build = client.build_for_version(version["id"])
    if not build:
        report.skip("readiness.export_compliance", "no build attached yet")
        return

    attrs = build.get("attributes") or {}
    uses = attrs.get("usesNonExemptEncryption")

    if uses is None:
        report.fail(
            "readiness.export_compliance",
            f"build {attrs.get('version')} has no export compliance declaration",
            fix="Set ITSAppUsesNonExemptEncryption in your Info.plist so this is answered\n"
                "at upload time, or answer it in App Store Connect. Submission is blocked\n"
                "until it is set.",
        )
    elif uses:
        report.warn(
            "readiness.export_compliance",
            f"build {attrs.get('version')} declares non-exempt encryption",
            fix="You may need French encryption declaration documentation and/or a US\n"
                "export compliance review. Most apps using only HTTPS should declare false.",
        )
    else:
        report.ok("readiness.export_compliance", "export compliance declared (exempt)")


def run(report, client, app, version):
    check_privacy_policy(report, client, app)
    check_age_rating(report, client, app)
    if version:
        check_review_details(report, client, version)
        check_iaps(report, client, app, version)
        check_export_compliance(report, client, version)
