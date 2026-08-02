"""App Store Connect listing checks. Requires an ASC API key. Read-only.

The availability check is the one that matters most and is the least known: an
app can be APPROVED, READY_FOR_SALE, and completely invisible in the store
because no app-level availability resource exists. The API returns 404 for it,
which is easy to dismiss as noise, and the listing simply never appears. That
failure costs days because every other signal says the app shipped fine.
"""

from ..asc import ASCError

# Apple accepts either size for the 6.9" slot; 6.5" is the older secondary set.
DISPLAY_SIZES = {
    "APP_IPHONE_67": {(1290, 2796), (1320, 2868)},
    "APP_IPHONE_65": {(1284, 2778), (1242, 2688)},
}

# Guideline 3.1.2 requires these to be discoverable for an auto-renewing sub.
SUBSCRIPTION_TERMS = ["privacy", "terms", "eula", "policy"]

LIVE_STATES = {"READY_FOR_SALE", "APPROVED", "PENDING_DEVELOPER_RELEASE"}
EDITABLE_STATES = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED",
                   "METADATA_REJECTED", "INVALID_BINARY"}


def check_availability(report, client, app):
    """Is the app actually available anywhere? The invisible-listing landmine.

    Two distinct failures, deliberately reported differently:
      1. The availability resource itself 404s -> the app has no availability at
         all and will be invisible regardless of review state. Genuinely broken.
      2. The territories call fails -> we simply could not read it. That is a
         tool problem, not an app problem, and must not be reported as failure.

    Conflating these produces a false alarm on a perfectly healthy live app.
    """
    app_id = app["id"]

    try:
        data = client.get(f"/apps/{app_id}/appAvailabilityV2")
    except ASCError as err:
        if "404" in str(err):
            report.fail(
                "listing.availability", "no app-level availability resource exists (404)",
                detail="This 404 is the actual signal, not API noise.",
                fix="The app can be READY_FOR_SALE and still be invisible in every\n"
                    "storefront. Create availability in App Store Connect > Pricing and\n"
                    "Availability and confirm you can read it back.",
            )
        else:
            report.skip("listing.availability", f"could not read availability ({err})")
        return

    availability = data.get("data", {})
    new_territories = (availability.get("attributes") or {}).get("availableInNewTerritories")

    # Follow the relationship link Apple gives us rather than constructing a URL.
    # The availability resource lives under v1, but its territories relationship
    # points at v2 — hand-building the path silently 404s.
    related = (((availability.get("relationships") or {})
                .get("territoryAvailabilities") or {})
               .get("links") or {}).get("related")

    if not related:
        report.ok(
            "listing.availability",
            "availability resource exists"
            + (" (opted into new territories)" if new_territories else ""),
        )
        return

    try:
        territories = client.get(f"{related}?limit=200").get("data", [])
    except ASCError as err:
        report.skip("listing.availability",
                    "availability exists, but territory list was unreadable",
                    detail=str(err))
        return

    available = [t for t in territories if (t.get("attributes") or {}).get("available")]

    if territories and not available:
        report.fail(
            "listing.availability", "app is available in ZERO territories",
            fix="The listing will not appear in the store no matter what the review\n"
                "state says. Set availability in App Store Connect > Pricing and\n"
                "Availability.",
        )
    else:
        report.ok(
            "listing.availability",
            f"available in {len(available)} territories"
            + (", opted into new ones" if new_territories else ""),
        )


def check_versions(report, client, app):
    """Version state, and whether a build is attached."""
    versions = client.versions(app["id"], limit=5)
    if not versions:
        report.warn("listing.version", "no App Store versions exist for this app")
        return None

    current = versions[0]
    attrs = current["attributes"]
    state = attrs.get("appStoreState")
    version = attrs.get("versionString")
    release = attrs.get("releaseType")

    detail = "\n".join(
        f"- {v['attributes'].get('versionString')}: {v['attributes'].get('appStoreState')}"
        for v in versions
    )

    if state in LIVE_STATES:
        report.ok("listing.version", f"{version} is {state}", detail=detail)
    elif state in EDITABLE_STATES:
        report.warn("listing.version", f"{version} is {state} (editable, not submitted)",
                    detail=detail)
    else:
        report.ok("listing.version", f"{version} is {state}", detail=detail)

    if release == "MANUAL" and state not in LIVE_STATES:
        report.warn(
            "listing.release_type", f"{version} is set to MANUAL release",
            fix="It will sit in PENDING_DEVELOPER_RELEASE after approval until you press\n"
                "the button. Set AFTER_APPROVAL if you want it to ship on approval.",
        )

    if state not in LIVE_STATES:
        build = client.build_for_version(current["id"])
        if build:
            report.ok("listing.build",
                      f"build {build['attributes'].get('version')} attached to {version}")
        else:
            report.fail(
                "listing.build", f"no build attached to {version}",
                fix="A version cannot be submitted without an attached build. Upload one,\n"
                    "wait for processing to reach VALID, then attach it.",
            )
    return current


def check_screenshots(report, client, version):
    """Are ALL iPhone screenshot sets populated, and correctly sized?

    The trap: updating one display type silently leaves the other serving old
    artwork. APP_IPHONE_67 is what modern iPhones render in search results, so
    a tool that only writes APP_IPHONE_65 looks like it worked and changes
    nothing most people will ever see.
    """
    locs = [l for l in client.localizations(version["id"])
            if l["attributes"].get("locale") == "en-US"] or client.localizations(version["id"])
    if not locs:
        report.skip("listing.screenshots", "no localizations found")
        return

    sets = client.screenshot_sets(locs[0]["id"])
    by_type = {}
    for s in sets:
        display = s["attributes"]["screenshotDisplayType"]
        shots = client.screenshots(s["id"])
        by_type[display] = shots

    iphone = {k: v for k, v in by_type.items() if k.startswith("APP_IPHONE")}
    if not iphone:
        report.fail("listing.screenshots", "no iPhone screenshot sets exist",
                    fix="At least one iPhone display type is required to submit.")
        return

    rows, problems = [], []
    for display, shots in sorted(iphone.items()):
        sizes = set()
        for shot in shots:
            asset = shot["attributes"].get("imageAsset") or {}
            if asset.get("width"):
                sizes.add((asset["width"], asset["height"]))
        label = "x".join(f"{w}x{h}" for w, h in sorted(sizes)) or "unknown"
        rows.append(f"- {display}: {len(shots)} screenshots ({label})")

        if not shots:
            problems.append(f"{display} is empty")
        expected = DISPLAY_SIZES.get(display)
        if expected and sizes and not sizes.issubset(expected):
            problems.append(f"{display} has sizes outside {sorted(expected)}")

    counts = {d: len(s) for d, s in iphone.items() if s}
    if len(set(counts.values())) > 1:
        problems.append(
            "sets disagree on count: " + ", ".join(f"{d}={n}" for d, n in counts.items())
        )

    detail = "\n".join(rows)
    if problems:
        report.warn(
            "listing.screenshots", "; ".join(problems), detail=detail,
            fix="APP_IPHONE_67 (6.9in) is what modern iPhones show in search results;\n"
                "APP_IPHONE_65 is secondary. Updating only one leaves the other serving\n"
                "stale artwork to most of your traffic. Write both.",
        )
    else:
        report.ok("listing.screenshots",
                  f"{len(iphone)} iPhone set(s) populated and consistent", detail=detail)


def check_subscription_metadata(report, client, app, version):
    """Guideline 3.1.2: auto-renewing subs need terms discoverable on the listing."""
    try:
        groups = client.get(
            f"/apps/{app['id']}/subscriptionGroups?limit=50"
        ).get("data", [])
    except ASCError:
        report.skip("listing.subscriptions", "could not read subscription groups")
        return

    if not groups:
        report.ok("listing.subscriptions", "no auto-renewing subscriptions (3.1.2 N/A)")
        return

    locs = [l for l in client.localizations(version["id"])
            if l["attributes"].get("locale") == "en-US"]
    if not locs:
        report.skip("listing.subscriptions", "no en-US localization to inspect")
        return

    attrs = locs[0]["attributes"]
    blob = " ".join(str(attrs.get(k) or "") for k in
                    ("description", "promotionalText", "whatsNew")).lower()

    missing = [t for t in SUBSCRIPTION_TERMS if t not in blob]
    has_link = "http" in blob

    if missing and not has_link:
        report.fail(
            "listing.subscriptions",
            "3.1.2 risk: no terms/privacy links found in the listing text",
            detail=f"App has {len(groups)} subscription group(s). "
                   f"Missing keywords: {', '.join(missing)}",
            fix="Apple rejects auto-renewing subscriptions whose listing does not carry a\n"
                "functional EULA/terms link, a privacy policy link, and the auto-renew\n"
                "disclosure with BOTH the amount and the billing period. Note the price\n"
                "string must include the period: '$4.99 per month', not '$4.99'.",
        )
    elif missing:
        report.warn(
            "listing.subscriptions",
            f"3.1.2: links present but no mention of {', '.join(missing)}",
            fix="Confirm the EULA/terms and privacy links are both present and functional.",
        )
    else:
        report.ok("listing.subscriptions",
                  f"3.1.2 metadata present ({len(groups)} subscription group(s))")


def run(report, client, app):
    check_availability(report, client, app)
    version = check_versions(report, client, app)
    if version:
        check_screenshots(report, client, version)
        check_subscription_metadata(report, client, app, version)
