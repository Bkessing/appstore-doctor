"""Free App Store surfaces the app is entitled to and is not using. Read-only.

These are not defects. The app ships fine without any of them, which is exactly
why they go unclaimed: nothing fails, nothing warns, and the slots sit empty for
the life of the app. They are reported because they are free, they are the only
distribution levers Apple gates behind a flag rather than a budget, and a small
app that is losing on impressions is usually leaving all of them on the table.

Everything here is a warning at worst. An app deliberately shipping without
events or custom pages is a legitimate choice, not a broken build.
"""

from ..asc import ASCError

# Apple's published ceilings, used to report the unused remainder honestly.
MAX_CUSTOM_PRODUCT_PAGES = 70
MAX_LIVE_EVENTS = 10


def _count(client, path):
    """Returns (count, error). A read failure is not the same as a zero."""
    try:
        data = client.get(path)
    except ASCError as err:
        return None, err
    payload = data.get("data")
    if payload is None:
        return 0, None
    return (len(payload) if isinstance(payload, list) else 1), None


def check_in_app_events(report, client, app):
    """In-App Events are the only free surface that adds space INSIDE search."""
    count, err = _count(client, f"/apps/{app['id']}/appEvents?limit=50")
    if err is not None:
        report.skip("surfaces.events", f"could not read In-App Events ({err})")
        return

    if count:
        report.ok("surfaces.events", f"{count} In-App Event(s) configured")
        return

    report.warn(
        "surfaces.events",
        f"no In-App Events (up to {MAX_LIVE_EVENTS} can be live at once)",
        detail="Event cards appear on the product page, in the user's library, in "
               "curated and personalised Today/Games/Apps selections, and in SEARCH "
               "RESULTS. They are reviewed independently of a binary, so an event "
               "ships without an app release. Max 31 days each, promotable 14 days "
               "before the start date.",
        fix="App Store Connect > your app > In-App Events. The event has to "
            "correspond to something real in the app; Apple rejects filler.",
    )


def check_custom_product_pages(report, client, app):
    """Custom pages carry their own keywords and can outrank the default page."""
    count, err = _count(
        client, f"/apps/{app['id']}/appCustomProductPages?limit={MAX_CUSTOM_PRODUCT_PAGES}"
    )
    if err is not None:
        report.skip("surfaces.custom_pages", f"could not read custom product pages ({err})")
        return

    if count:
        report.ok(
            "surfaces.custom_pages",
            f"{count} custom product page(s), {MAX_CUSTOM_PRODUCT_PAGES - count} slot(s) unused",
        )
        return

    report.warn(
        "surfaces.custom_pages",
        f"no custom product pages ({MAX_CUSTOM_PRODUCT_PAGES} slots unused)",
        detail="Each page gets its own screenshots, promo text, URL, analytics AND "
               "its own assigned keywords, and can appear in search results in place "
               "of the default page for those keywords. Submittable independent of an "
               "app update and automatable through the App Store Connect API.\n"
               "The second win is attribution: give every off-store link its own page "
               "and traffic becomes measurable instead of a guess.",
        fix="App Store Connect > your app > Custom Product Pages. Keep each keyword "
            "combination unique to one page.",
    )


def check_game_center(report, client, app):
    """Game Center initialization gates the Top Played chart and social recs.

    Reported for every app rather than only games: the API exposes no reliable
    'is this a game' flag here, and a non-game seeing one warning is a smaller
    cost than a game silently missing the chart it qualifies for.
    """
    # An unconfigured app answers 200 with a null data member, NOT a 404, so
    # "the request succeeded" is not evidence of anything. Treating it as such
    # reported Game Center as configured on an app with no GameKit at all.
    configured = False
    try:
        payload = client.get(f"/apps/{app['id']}/gameCenterDetail")
        configured = payload.get("data") is not None
    except ASCError as err:
        if "404" not in str(err):
            report.skip("surfaces.game_center", f"could not read Game Center detail ({err})")
            return

    if not configured:
        report.warn(
                "surfaces.game_center",
                "no Game Center configuration (games only: gates the Top Played chart)",
                detail="Apple: with Game Center initialization a game can appear in "
                       "social recommendations on the Home and Friends tabs, and in the "
                       "Top Played chart, which shows on the Games app AND the App "
                       "Store. Without it a game still appears in search, Library and "
                       "Continue Playing, and in nothing else.\n"
                       "Leaderboards are hosted by Apple, so a global leaderboard needs "
                       "no backend of your own.\n"
                       "Ignore this check if the app is not a game.",
                fix="Initialize GKLocalPlayer at launch, add the com.apple.developer."
                    "game-center entitlement, then configure leaderboards or "
                    "achievements in App Store Connect.",
        )
        return

    report.ok("surfaces.game_center", "Game Center is configured")


def run(report, client, app):
    check_in_app_events(report, client, app)
    check_custom_product_pages(report, client, app)
    check_game_center(report, client, app)
