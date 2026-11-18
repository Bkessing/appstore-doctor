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


# What an app's own analytics code sends, mapped to the collected-data type its
# privacy manifest must then declare. Matched on event NAMES and imports in the
# app's Swift source, so it is a floor, not a proof: an event called "buy_done"
# would slip through. Lab Tycoon 1.1 sent "iap_purchased" with no PurchaseHistory
# declared, which is the case this exists for.
CAPTURE_CALL = re.compile(r"\b(?:capture|track|logEvent|trackEvent)\(\s*\"([A-Za-z0-9_.:\- ]+)\"")
PRIVACY_EVENT_RULES = (
    # (collected type, what triggers it, test on an event name or None, import regex or None)
    ("NSPrivacyCollectedDataTypePurchaseHistory", "purchase events",
     re.compile(r"purchas|(^|_)iap(_|$)|subscri|checkout", re.I), None),
    ("NSPrivacyCollectedDataTypeProductInteraction", "analytics events",
     re.compile(r"."), None),
    ("NSPrivacyCollectedDataTypeCrashData", "a crash reporter",
     None, re.compile(r"^\s*import\s+(Sentry|FirebaseCrashlytics|Bugsnag)\b", re.M)),
)
SOURCE_SKIP_DIRS = ("/.build/", "/Pods/", "/DerivedData/", "/Tests/", "Tests/", "/fastlane/", "/.git/")


def _app_sources(root):
    for path in glob.glob(os.path.join(root, "**", "*.swift"), recursive=True):
        rel = "/" + os.path.relpath(path, root).replace(os.sep, "/")
        if any(skip in rel for skip in SOURCE_SKIP_DIRS):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                yield rel.lstrip("/"), handle.read()
        except OSError:
            continue


def declared_data_types(manifest_path):
    """Collected-data types a PrivacyInfo.xcprivacy declares, or None if unreadable."""
    try:
        with open(manifest_path, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    out = set()
    for entry in data.get("NSPrivacyCollectedDataTypes") or []:
        if isinstance(entry, dict) and entry.get("NSPrivacyCollectedDataType"):
            out.add(entry["NSPrivacyCollectedDataType"])
    return out


def check_privacy_events(report, root):
    """The manifest must declare what the app's own analytics code sends."""
    manifests = [m for m in glob.glob(os.path.join(root, "**", "PrivacyInfo.xcprivacy"), recursive=True)
                 if "/Pods/" not in m and "/.build/" not in m]
    events, imports = {}, {}
    for rel, text in _app_sources(root):
        for name in CAPTURE_CALL.findall(text):
            events.setdefault(name, rel)
        for ctype, _why, _ev, imp in PRIVACY_EVENT_RULES:
            if imp is not None and imp.search(text):
                imports.setdefault(ctype, rel)
    needed = {}
    for ctype, why, ev, imp in PRIVACY_EVENT_RULES:
        hits = sorted(n for n in events if ev is not None and ev.search(n))
        if hits:
            needed[ctype] = (why, hits[:3], events[hits[0]])
        elif ctype in imports:
            needed[ctype] = (why, [], imports[ctype])
    if not needed:
        report.ok("project.privacy_events", "no analytics events or crash reporter found to declare")
        return
    if not manifests:
        report.fail(
            "project.privacy_events",
            "the app sends analytics but has no PrivacyInfo.xcprivacy",
            detail=", ".join(sorted(t.replace("NSPrivacyCollectedDataType", "") for t in needed)),
            fix="Add a privacy manifest to the app target that declares each data type above.",
        )
        return
    declared = set()
    for m in manifests:
        types = declared_data_types(m)
        if types is None:
            report.fail("project.privacy_events", f"could not read {os.path.relpath(m, root)}",
                        fix="Fix the plist so Xcode and this check can parse it.")
            return
        declared |= types
    missing = {t: v for t, v in needed.items() if t not in declared}
    if not missing:
        report.ok("project.privacy_events",
                  f"manifest declares what the code sends ({len(needed)} type(s) checked)")
        return
    lines = []
    for ctype, (why, hits, where) in sorted(missing.items()):
        short = ctype.replace("NSPrivacyCollectedDataType", "")
        seen = ", ".join(hits) if hits else "import"
        lines.append(f"{short}: the app sends {why} ({seen}; {where})")
    first = sorted(missing)[0].replace("NSPrivacyCollectedDataType", "")
    report.fail(
        "project.privacy_events",
        f"privacy manifest does not declare {', '.join(sorted(t.replace('NSPrivacyCollectedDataType', '') for t in missing))}",
        detail="\n".join(lines),
        fix=f"Add an NSPrivacyCollectedDataTypes entry for each missing type (for example\n"
            f"NSPrivacyCollectedDataType{first}, linked as the app links its other analytics,\n"
            "purpose Analytics), and tick the same type in App Store Connect > App Privacy.",
        data={"missing": sorted(missing)},
    )


# Ways a release lane commonly overwrites the build number at build time. Any of
# these means the checked-in CURRENT_PROJECT_VERSION is not what gets uploaded.
STAMP_MARKERS = (
    "increment_build_number",
    "CURRENT_PROJECT_VERSION=",
    "build_number:",
    "latest_testflight_build_number",
)


def build_number_is_stamped(root):
    """Whether a Fastfile in this project rewrites the build number at build time.

    Deliberately a text scan and deliberately not clever. It decides between a
    hard failure and a warning, so a false positive costs a softened message and
    a false negative costs nothing that was not already the old behaviour.
    """
    for name in ("fastlane/Fastfile", "Fastfile"):
        path = os.path.join(root, name)
        try:
            with open(path) as handle:
                body = handle.read()
        except OSError:
            continue
        if any(marker in body for marker in STAMP_MARKERS):
            return True
    return False


def check_build_number(report, root, client=None, app=None):
    """Compare the local build number against what has already been uploaded.

    Apple rejects a build whose CFBundleVersion is not higher than the last one
    uploaded. Whether a bump to the marketing version lets you reset it has been
    reported inconsistently across Xcode versions, so do not infer a local rule.
    Ask App Store Connect what actually exists.

    The committed CURRENT_PROJECT_VERSION is only what ships if nothing rewrites
    it at build time. Plenty of release lanes stamp a timestamp instead, and on
    those projects the checked-in value is decoration -- reporting a hard failure
    against it is a false alarm, and a checker that cries wolf gets ignored. When
    a lane looks like it stamps the number, this warns and says why instead.
    """
    stamped = build_number_is_stamped(root)
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

    if local not in uploaded:
        report.ok("project.build_number", f"local build {local} is unused", detail=detail)
        return

    if stamped:
        # The number in the project is not the number that will be uploaded, so
        # a collision here predicts nothing.
        report.warn(
            "project.build_number",
            f"build number {local} is already uploaded, but a release lane stamps its own",
            detail=detail,
            fix="Your Fastfile sets the build number at build time, so the value in\n"
                "the project file is not what ships and this collision is probably\n"
                "harmless. Worth a look only if you also build outside that lane.",
        )
        return

    report.fail(
        "project.build_number",
        f"build number {local} has already been uploaded",
        detail=detail,
        fix="Upload is rejected with \"The bundle version must be higher than the\n"
            "previously uploaded version\". Bump CURRENT_PROJECT_VERSION.",
    )


def run(report, root, client=None, app=None, presubmit=False):
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        (report.fail if presubmit else report.skip)("project", f"{root} is not a directory")
        return
    if presubmit:
        # The build is already uploaded: icon and build number are settled. What
        # still decides the submit is whether the privacy story matches the code.
        check_privacy_events(report, root)
        return
    check_app_icon(report, root)
    check_privacy_manifest(report, root)
    check_privacy_events(report, root)
    check_build_number(report, root, client=client, app=app)
