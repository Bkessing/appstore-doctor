"""appstore-doctor — diagnose why an iOS app will not ship.

Read-only. Nothing is written to your App Store Connect account and no
credentials leave this machine.
"""

import argparse
import sys

from .report import Report
from .checks import signing, listing, readiness, certificates
from .asc import Client, MissingCredentials, ASCError

VERSION = "0.1.0"

EPILOG = """
examples:
  appstore-doctor                              local signing checks only
  appstore-doctor --bundle com.acme.app        add App Store Connect checks
  appstore-doctor --app-id 1234567890 --json   machine-readable output

credentials (App Store Connect checks only):
  ASC_KEY_ID          key id from App Store Connect > Users and Access > Integrations
  ASC_ISSUER_ID       issuer id from the same page
  ASC_KEY_FILEPATH    path to AuthKey_<KEY_ID>.p8
                      (defaults to ~/.appstoreconnect/private_keys/)

  The key is used locally to sign a request to Apple. It is never uploaded
  anywhere, and this tool only ever issues GET requests.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="appstore-doctor",
        description=__doc__.strip().splitlines()[0],
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bundle", help="bundle identifier, e.g. com.acme.app")
    parser.add_argument("--app-id", help="numeric App Store id, if you know it")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--local-only", action="store_true",
                        help="skip App Store Connect checks entirely")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--version", action="version", version=f"appstore-doctor {VERSION}")
    args = parser.parse_args(argv)

    report = Report()

    # Local signing checks always run and never need credentials.
    signing.run(report, bundle_id=args.bundle)

    if not args.local_only:
        if not (args.bundle or args.app_id):
            report.skip(
                "listing", "no --bundle or --app-id given, skipping App Store Connect checks",
                fix="Pass --bundle com.your.app to check availability, version state,\n"
                    "screenshots and subscription metadata.",
            )
        else:
            try:
                client = Client()
                certificates.run(report, client)
                app = client.find_app(bundle_id=args.bundle, app_id=args.app_id)
                if not app:
                    report.fail(
                        "listing.app",
                        f"no app matching {args.bundle or args.app_id} on this account",
                        fix="Check the bundle id, and that the API key belongs to the right team.",
                    )
                else:
                    version = listing.run(report, client, app)
                    readiness.run(report, client, app, version)
            except MissingCredentials as err:
                report.skip("listing", "App Store Connect credentials not configured",
                            detail=str(err))
            except ASCError as err:
                report.fail("listing", "could not reach App Store Connect", detail=str(err))

    if args.json:
        print(report.to_json())
    else:
        report.render(color=not args.no_color)

    return report.exit_code()


if __name__ == "__main__":
    sys.exit(main())
