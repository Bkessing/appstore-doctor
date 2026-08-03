"""Local project checks. Point at a source directory; no credentials needed.

These catch failures that happen at UPLOAD, before review ever sees the build,
which is the most annoying place to fail because the feedback loop is a full
archive-and-upload cycle.
"""

import glob
import json
import os
import plistlib
import re

# PNG IHDR colour types that carry an alpha channel.
ALPHA_COLOUR_TYPES = {4, 6}

REQUIRED_ICON_SIZE = 1024

# SDKs Apple requires to ship a signed privacy manifest. Abbreviated from
# Apple's published list, which is authoritative and DRIFTS -- re-check it:
# https://developer.apple.com/support/third-party-SDK-requirements/
SDKS_NEEDING_MANIFEST = {
    "Alamofire", "abseil", "AppAuth", "BoringSSL", "openssl_grpc", "Capacitor",
    "Charts", "Cordova", "FBAEMKit", "FBLPromises", "FBSDKCoreKit",
    "FBSDKLoginKit", "FBSDKShareKit", "file_picker", "FirebaseABTesting",
    "FirebaseAuth", "FirebaseCore", "FirebaseCrashlytics", "FirebaseDatabase",
    "FirebaseFirestore", "FirebaseInstallations", "FirebaseMessaging",
    "FirebaseRemoteConfig", "Flutter", "flutter_inappwebview",
    "flutter_local_notifications", "fluttertoast", "FMDB", "GoogleDataTransport",
    "GoogleSignIn", "GoogleToolboxForMac", "GoogleUtilities", "grpcpp",
    "GTMAppAuth", "GTMSessionFetcher", "hermes", "image_picker_ios", "IQKeyboardManager",
    "IQKeyboardManagerSwift", "Kingfisher", "leveldb", "Lottie", "MBProgressHUD",
    "nanopb", "OneSignal", "OneSignalCore", "OneSignalOutcomes", "OpenSSL",
    "OrderedSet", "package_info", "package_info_plus", "path_provider",
    "path_provider_ios", "Promises", "Protobuf", "Reachability", "RealmSwift",
    "RxCocoa", "RxSwift", "SDWebImage", "share_plus", "shared_preferences_ios",
    "SnapKit", "sqflite", "Starscream", "SVProgressHUD", "SwiftyGif",
    "SwiftyJSON", "Toast", "UnityFramework", "url_launcher_ios",
    "video_player_avfoundation", "wakelock", "webview_flutter_wkwebview",
}


def _png_dimensions_and_alpha(path):
    """Read width, height and alpha from the PNG header. No subprocess needed.

    Apple's validator rejects an icon that CARRIES an alpha channel, not one
    that merely looks opaque -- an image exported as RGBA fails even if every
    pixel is fully opaque. So test the colour type, never the pixel values.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(26)
    except OSError:
        return None
    if len(head) < 26 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    colour_type = head[25]
    return width, height, colour_type in ALPHA_COLOUR_TYPES


def check_app_icon(report, root):
    """Alpha channel in the App Store icon is a hard upload rejection."""
    sets = glob.glob(os.path.join(root, "**", "AppIcon.appiconset"), recursive=True)
    icon_bundles = glob.glob(os.path.join(root, "**", "*.icon"), recursive=True)

    if not sets and not icon_bundles:
        report.skip("project.icon", "no AppIcon.appiconset or .icon bundle found")
        return

    if icon_bundles and not sets:
        report.warn(
            "project.icon",
            f"only an Icon Composer bundle found ({os.path.basename(icon_bundles[0])})",
            fix="Icon Composer can introduce an alpha channel during export even when the\n"
                "source layers have none, which fails upload with ITMS-90717. This tool\n"
                "cannot read the compiled output from source. Check the 1024x1024 icon\n"
                "inside your built .app before uploading.",
        )
        return

    offenders, largest, rows = [], None, []
    for icon_set in sets:
        for png in sorted(glob.glob(os.path.join(icon_set, "*.png"))):
            info = _png_dimensions_and_alpha(png)
            if not info:
                continue
            width, height, has_alpha = info
            name = os.path.basename(png)
            if has_alpha:
                offenders.append(f"{name} ({width}x{height})")
            if width == height == REQUIRED_ICON_SIZE:
                largest = largest or name
            rows.append(f"- {name}: {width}x{height}{' ALPHA' if has_alpha else ''}")

    if offenders:
        report.fail(
            "project.icon",
            f"{len(offenders)} icon(s) carry an alpha channel",
            detail="\n".join(f"- {o}" for o in offenders),
            fix="Upload fails with ITMS-90717: \"The large app icon ... can't be transparent\n"
                "or contain an alpha channel.\" Apple checks whether the PNG HAS an alpha\n"
                "channel, not whether pixels are transparent, so a fully opaque RGBA export\n"
                "still fails. Re-export as RGB (no alpha).",
        )
        return

    if not largest:
        report.warn(
            "project.icon", f"no {REQUIRED_ICON_SIZE}x{REQUIRED_ICON_SIZE} App Store icon found",
            detail="\n".join(rows[:12]),
            fix=f"The App Store requires a {REQUIRED_ICON_SIZE}x{REQUIRED_ICON_SIZE} icon.",
        )
        return

    report.ok("project.icon", f"{len(rows)} icons, none carry an alpha channel")


def check_privacy_manifest(report, root):
    """Privacy manifest presence, and whether linked SDKs oblige you to have one."""
    manifests = glob.glob(os.path.join(root, "**", "PrivacyInfo.xcprivacy"), recursive=True)
    own = [m for m in manifests if "Pods" not in m and ".build" not in m]

    # Find linked third-party packages from SPM and CocoaPods.
    linked = set()
    for resolved in glob.glob(os.path.join(root, "**", "Package.resolved"), recursive=True):
        try:
            with open(resolved) as handle:
                data = json.load(handle)
            pins = data.get("pins") or (data.get("object") or {}).get("pins") or []
            for pin in pins:
                name = pin.get("identity") or pin.get("package") or ""
                if name:
                    linked.add(name)
        except (OSError, ValueError):
            continue
    podfile = os.path.join(root, "Podfile.lock")
    if os.path.exists(podfile):
        try:
            with open(podfile) as handle:
                for line in handle:
                    m = re.match(r"\s+- ([A-Za-z0-9_+\-/]+)", line)
                    if m:
                        linked.add(m.group(1).split("/")[0])
        except OSError:
            pass

    # Match exactly, and also by prefix, because CocoaPods subspecs collapse to
    # a bare umbrella name ("Firebase/Core" -> "Firebase") that is not itself a
    # literal entry on Apple's list even though every product under it is.
    lowered = {n.lower() for n in linked}
    obliged = sorted(
        sdk for sdk in SDKS_NEEDING_MANIFEST
        if sdk.lower() in lowered or any(sdk.lower().startswith(n) for n in lowered if len(n) > 3)
    )

    if own:
        report.ok(
            "project.privacy_manifest",
            f"PrivacyInfo.xcprivacy present ({os.path.relpath(own[0], root)})",
            detail=(f"{len(obliged)} linked SDK(s) on Apple's manifest-required list: "
                    + ", ".join(obliged)) if obliged else "",
        )
    elif obliged:
        report.fail(
            "project.privacy_manifest",
            f"no PrivacyInfo.xcprivacy, but {len(obliged)} linked SDK(s) require one",
            detail=", ".join(obliged),
            fix="Add one via Xcode > File > New > App Privacy File, and make sure it is a\n"
                "member of the app target (a file merely sitting in the folder is ignored).",
        )
    else:
        report.warn(
            "project.privacy_manifest", "no PrivacyInfo.xcprivacy found",
            fix="Not always mandatory, but required if your app or any linked SDK calls a\n"
                "required-reason API (UserDefaults, file timestamps, disk space, boot time,\n"
                "active keyboards). Missing declarations fail with ITMS-91053.\n"
                "NOTE: this tool only matches linked package NAMES against Apple's published\n"
                "list. It does not scan compiled binaries, so it cannot prove you are clear.",
        )


def check_build_number(report, root, client=None, app=None):
    """Compare the local build number against what has already been uploaded.

    Apple rejects a build whose CFBundleVersion is not higher than the last one
    uploaded. Whether a bump to the marketing version lets you reset it has been
    reported inconsistently across Xcode versions, so do not infer a local rule.
    Ask App Store Connect what actually exists.
    """
    local = None
    for pbx in glob.glob(os.path.join(root, "**", "project.pbxproj"), recursive=True):
        try:
            with open(pbx) as handle:
                found = re.findall(r"CURRENT_PROJECT_VERSION = ([^;]+);", handle.read())
            if found:
                local = found[0].strip()
                break
        except OSError:
            continue

    if local is None:
        report.skip("project.build_number", "no CURRENT_PROJECT_VERSION found in the project")
        return

    if client is None or app is None:
        report.skip("project.build_number",
                    f"local build number is {local} (App Store Connect not queried)")
        return

    try:
        builds = client.get(
            f"/builds?filter[app]={app['id']}&limit=10&sort=-uploadedDate"
        ).get("data", [])
    except Exception as err:  # noqa: BLE001 - any failure here is non-fatal
        report.skip("project.build_number", f"could not list uploaded builds ({err})")
        return

    uploaded = [b["attributes"].get("version") for b in builds if b["attributes"].get("version")]
    if not uploaded:
        report.ok("project.build_number", f"local build {local}, nothing uploaded yet")
        return

    detail = "recently uploaded: " + ", ".join(uploaded[:5])

    if local in uploaded:
        report.fail(
            "project.build_number",
            f"build number {local} has already been uploaded",
            detail=detail,
            fix="Upload is rejected with \"The bundle version must be higher than the\n"
                "previously uploaded version\". Bump CURRENT_PROJECT_VERSION.\n"
                "Note: fastlane lanes that stamp a timestamp build number sidestep this\n"
                "entirely, which is why it may not match what actually ships.",
        )
    else:
        report.ok("project.build_number", f"local build {local} is unused", detail=detail)


def run(report, root, client=None, app=None):
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        report.skip("project", f"{root} is not a directory")
        return
    check_app_icon(report, root)
    check_privacy_manifest(report, root)
    check_build_number(report, root, client=client, app=app)
