"""Submission-readiness checks against App Store Connect. Read-only.

These are the blockers that have nothing to do with your code. An app can build,
sign, upload and still be unsubmittable — or get rejected on the first pass —
because a field nobody looks at is empty.

Everything here was confirmed readable against the live API before it was
written; checks are not speculative.
"""

import re

from ..asc import ASCError

# IAP/subscription states that are fine to submit alongside a binary.
IAP_OK = {"READY_TO_SUBMIT", "WAITING_FOR_REVIEW", "IN_REVIEW", "APPROVED",
          "DEVELOPER_ACTION_NEEDED", "PENDING_BINARY_APPROVAL"}
IAP_LIVE = {"APPROVED"}

NOT_SUBMITTED_STATES = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED",
                        "METADATA_REJECTED", "INVALID_BINARY", "DEVELOPER_REMOVED_FROM_SALE"}

# The app itself is on sale to the public.
ON_SALE_STATES = {"READY_FOR_SALE", "PENDING_DEVELOPER_RELEASE", "APPROVED"}


# An app has two appInfo records while an update is being prepared: the live
# one and the editable one. Reading infos[0] checked whichever Apple listed
# first, which on 2026-11-18 was the live record, so six locales with no privacy
# URL on Mint Condition 1.1's editable record passed here and then failed the
# submit with a 409.
LIVE_INFO_STATES = {"READY_FOR_DISTRIBUTION", "READY_FOR_SALE"}


def editable_app_info(client, app):
    """The appInfo a submission will use: the one not yet live, else the only one."""
    infos = client.get(f"/apps/{app['id']}/appInfos?limit=10").get("data", [])
    if not infos:
        return None

    def state(info):
        attrs = info.get("attributes") or {}
        return attrs.get("state") or attrs.get("appStoreState")

    return next((i for i in infos if state(i) not in LIVE_INFO_STATES), infos[0])


def app_info_localizations(client, info):
    return client.get(
        f"/appInfos/{info['id']}/appInfoLocalizations?limit=50"
    ).get("data", [])


def check_privacy_policy(report, client, app):
    """A privacy policy URL is mandatory in EVERY locale. Missing one is a 409 at submit."""
    try:
        info = editable_app_info(client, app)
        if not info:
            report.skip("readiness.privacy_policy", "no appInfo record found")
            return
        locs = app_info_localizations(client, info)
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
            detail="Checked the editable app info, the one this submission will use.\n"
                   "Adding a locale (new store text) adds a localization with an EMPTY\n"
                   "privacy URL; the en-US one is not inherited.",
            fix="Every locale needs a privacy policy URL, and it must resolve. Set the same\n"
                "URL the other locales carry on each one listed, in App Store Connect >\n"
                "App Information (switch language at the top), or by PATCHing\n"
                "privacyPolicyUrl on each appInfoLocalization.",
        )
    else:
        report.ok("readiness.privacy_policy",
                  f"privacy policy URL set for {len(locs)} localization(s)")


# Age-rating attributes that are legitimately null on a complete declaration:
# overrides, the kids band, an optional info URL, fields Apple has retired, and
# the Korea GRAC rating number (null on all three apps Apple accepted for review
# on 2026-11-18: CardHabit 2.0, Mint Condition 1.1, Lab Tycoon 1.1).
# Everything else that is null is a question nobody answered, and a NEW question
# from Apple shows up exactly that way: `socialMedia` arrived in late Oct 2026
# as a null field and blocked CardHabit 2.0's submit on 2026-11-18 with an
# error reported against the version, not the rating.
AGE_RATING_OPTIONAL = {
    "kidsAgeBand", "ageRatingOverride", "ageRatingOverrideV2", "koreaAgeRatingOverride",
    "developerAgeRatingInfoUrl", "seventeenPlus", "gamblingAndContests",
    "gracRatingClassificationNumber",
}
# Follow-up questions: required only when the parent answer is true.
AGE_RATING_FOLLOW_UPS = {"socialMediaAgeRestricted": "socialMedia"}


def unanswered_age_rating_fields(attrs):
    missing = []
    for key, value in sorted(attrs.items()):
        if value is not None or key in AGE_RATING_OPTIONAL:
            continue
        parent = AGE_RATING_FOLLOW_UPS.get(key)
        if parent and attrs.get(parent) is not True:
            continue
        missing.append(key)
    return missing


def check_age_rating(report, client, app):
    """Every age-rating question must be answered, including ones Apple added recently."""
    try:
        info = editable_app_info(client, app)
        if not info:
            report.skip("readiness.age_rating", "no appInfo record found")
            return
        rating = client.get(f"/appInfos/{info['id']}/ageRatingDeclaration")
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
    store_rating = (info.get("attributes") or {}).get("appStoreAgeRating")

    if not attrs:
        report.warn("readiness.age_rating", "age rating declaration is empty")
        return

    missing = unanswered_age_rating_fields(attrs)
    if missing:
        report.fail(
            "readiness.age_rating",
            f"{len(missing)} age-rating question(s) unanswered: {', '.join(missing)}",
            detail="A null answer blocks submission. Apple adds questions without notice\n"
                   "(socialMedia, late Oct 2026), and the submit error names the VERSION.",
            fix="Answer each one in App Store Connect > App Information > Age Rating, or\n"
                "PATCH /v1/ageRatingDeclarations/<id>. Each answer is a statement to\n"
                "Apple about the app: decide it from what the app does, never by default.",
            data={"unanswered": missing, "attributes": attrs},
        )
        return

    answered = [k for k, v in attrs.items() if v not in (None, "", False, "NONE")]
    report.ok(
        "readiness.age_rating",
        f"age rating declared{f' ({store_rating})' if store_rating else ''}",
        detail=f"{len(answered)} non-default answers recorded",
        data={"attributes": attrs},
    )


# Words that may repeat between name and subtitle without wasting search weight.
_FILLER = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "your",
           "my", "by", "&", "+", "-", "at", "or"}


def _words(text):
    out = set()
    for w in re.findall(r"[^\W_]+", (text or "").lower()):
        # Under 3 letters is mostly locale filler ("de", "la", "et"); skip it.
        if w in _FILLER or len(w) < 3:
            continue
        # Plural and singular are the same keyword to the store index.
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def check_name_subtitle(report, client, app):
    """A word in both the name and the subtitle is indexed once: the repeat is wasted.

    A warning, never a block. Apple accepts it; it just spends characters that
    could carry another search term. CardHabit 2.0's rename on 2026-11-18 was
    held for exactly this ("cards", "daily" in both).
    """
    try:
        info = editable_app_info(client, app)
        if not info:
            return
        locs = app_info_localizations(client, info)
    except ASCError as err:
        report.skip("readiness.name_subtitle", f"could not read app info ({err})")
        return

    rows = []
    for loc in locs:
        attrs = loc.get("attributes") or {}
        shared = _words(attrs.get("name")) & _words(attrs.get("subtitle"))
        if shared:
            rows.append(f"- {attrs.get('locale')}: \"{attrs.get('name')}\" / "
                        f"\"{attrs.get('subtitle')}\" share {', '.join(sorted(shared))}")

    if rows:
        report.warn(
            "readiness.name_subtitle",
            f"name and subtitle repeat a word in {len(rows)} locale(s)",
            detail="\n".join(rows),
            fix="The store indexes name, subtitle and keywords together, so a repeated\n"
                "word earns nothing twice. Keep it if it reads better; otherwise swap the\n"
                "repeat in the subtitle for a term the app does not rank for yet.",
        )
    elif locs:
        report.ok("readiness.name_subtitle",
                  f"no repeated words between name and subtitle ({len(locs)} locale(s))")


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


def check_iaps(report, client, app, version, with_iaps=False):
    """In-app purchases must be submitted alongside the binary, in a valid state.

    with_iaps: the caller's submission will carry every unapproved in-app
    purchase (ios-release-kit's submit_with_iaps passes it). It does not carry
    subscriptions, so those are still checked.
    """
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

    check_iap_submission(report, iaps, subs, version, with_iaps)

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
            detail="\n".join(f"- {pid}: {state}" for pid, state in incomplete)
                   + (f"\nIn review (pending, not incomplete): "
                      f"{', '.join(p for p, _ in in_flight)}" if in_flight else ""),
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


# Product states that will not be reviewed unless something attaches them.
IAP_UNSUBMITTED = {"READY_TO_SUBMIT", "DEVELOPER_ACTION_NEEDED", "REJECTED"}
# States a version can be submitted from (READY_FOR_REVIEW: attached to a draft
# submission that has not been sent yet).
SUBMITTABLE_STATES = NOT_SUBMITTED_STATES | {"READY_FOR_REVIEW"}


def check_iap_submission(report, iaps, subs, version, with_iaps=False):
    """An unapproved product that is the FIRST of its kind must ride with this version.

    Apple refuses a first consumable or first non-consumable submitted on its
    own (FIRST_NON_CONSUMABLE_MUST_BE_SUBMITTED_ON_VERSION, 2026-11-18), and a
    version submitted without it goes live unable to sell (Lab Tycoon 1.0,
    2026-08-06). An unapproved product whose kind already has an approved
    sibling can be submitted later, so it only warns: it may be parked on purpose
    (CardHabit's Elite tier).
    """
    if version["attributes"].get("appStoreState") not in SUBMITTABLE_STATES:
        return

    def attrs(p):
        return p.get("attributes") or {}

    approved_kinds = {attrs(i).get("inAppPurchaseType") for i in iaps
                      if attrs(i).get("state") in IAP_LIVE}
    sub_approved = any(attrs(s).get("state") in IAP_LIVE for s in subs)

    first, later = [], []
    for i in iaps:
        a = attrs(i)
        if a.get("state") not in IAP_UNSUBMITTED:
            continue
        row = f"{a.get('productId')} ({a.get('inAppPurchaseType') or 'in-app purchase'}, {a.get('state')})"
        if with_iaps:
            continue
        (later if a.get("inAppPurchaseType") in approved_kinds else first).append(row)
    for sub in subs:
        a = attrs(sub)
        if a.get("state") not in IAP_UNSUBMITTED:
            continue
        row = f"{a.get('productId')} (subscription, {a.get('state')})"
        (later if sub_approved else first).append(row)

    if first:
        report.fail(
            "readiness.iap.submission",
            f"{len(first)} first-of-its-kind product(s) are not in this submission",
            detail="\n".join(f"- {r}" for r in first + later),
            fix="A first consumable, non-consumable or subscription must be submitted WITH\n"
                "a version. In-app purchases: use ios-release-kit's submit_with_iaps\n"
                "(DRY=1 first; afterwards confirm the version AND each product read\n"
                "WAITING_FOR_REVIEW). It does not carry subscriptions: attach those to the\n"
                "same review submission before it is submitted.",
        )
    elif later:
        report.warn(
            "readiness.iap.submission",
            f"{len(later)} unapproved product(s) will not be reviewed with this version",
            detail="\n".join(f"- {r}" for r in later),
            fix="Fine if they are parked on purpose. Otherwise submit them with the\n"
                "version (submit_with_iaps) or on their own.",
        )
    elif with_iaps and any(attrs(i).get("state") in IAP_UNSUBMITTED for i in iaps):
        report.ok("readiness.iap.submission",
                  "unapproved in-app purchases will ride with this version (submit_with_iaps)")


def check_agreements(report, client, app, version):
    """An inactive agreement blocks submission, and the API cannot read agreements.

    /v1/agreements is a 404. The only machine signal is an error code on some
    other request, so this runs LAST and reports any such code seen during the
    run. Without one, a product submission still gets a reminder, because the
    contract error only appears at submit time (CardHabit and The Stoics,
    2026-11-18: IAP_SUBMISSION_NOT_ALLOWED_PROVIDER_MISSING_VALID_CONTRACT).
    """
    seen = list(getattr(client, "agreement_errors", []) or [])
    if seen:
        report.fail(
            "readiness.agreements",
            "Apple reports a missing or expired agreement",
            detail="\n".join(f"- {e}" for e in seen),
            fix="Open App Store Connect > Business and accept every pending agreement\n"
                "(the Paid Applications Agreement for anything that sells). Only the\n"
                "Account Holder can accept one. Re-run this check afterwards.",
        )
        return

    if not version or version["attributes"].get("appStoreState") not in SUBMITTABLE_STATES:
        return
    try:
        iaps = client.get(f"/apps/{app['id']}/inAppPurchasesV2?limit=200").get("data", [])
    except ASCError:
        return
    unapproved = [i for i in iaps
                  if (i.get("attributes") or {}).get("state") in IAP_UNSUBMITTED]
    if unapproved:
        report.warn(
            "readiness.agreements",
            "products are going to review: the Paid Applications Agreement must be Active",
            fix="The API cannot read agreements. Check App Store Connect > Business shows\n"
                "the Paid Applications Agreement as Active before submitting. If it is not,\n"
                "the product submit fails with ..._PROVIDER_MISSING_VALID_CONTRACT.",
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


def run(report, client, app, version, with_iaps=False):
    check_privacy_policy(report, client, app)
    check_age_rating(report, client, app)
    check_name_subtitle(report, client, app)
    if version:
        check_review_details(report, client, version)
        check_iaps(report, client, app, version, with_iaps=with_iaps)
        check_export_compliance(report, client, version)
    # Last: it reports agreement errors seen by every request above.
    check_agreements(report, client, app, version)
