# appstore-doctor

Diagnose why your iOS app won't ship.

App Store errors name a symptom, not a cause. A missing distribution certificate
reports as `No Accounts`. A locked keychain reports as `errSecInternalComponent`
on whatever framework happened to be signed first. An app can be `READY_FOR_SALE`
and invisible in every storefront on Earth.

This reads the actual state of your machine and your App Store Connect account
and tells you what's wrong in plain language, with the fix.

**Read-only.** It never writes to your App Store Connect account. Your API key
never leaves your machine.

```
$ appstore-doctor --bundle com.acme.app

  FAIL  signing.identities: no Apple Distribution identity (release export will fail)
          - Apple Development: you@example.com (ABCDE12345)
     fix:  This is what 'exportArchive No Accounts' actually means.
           Create one: fastlane cert  — or check whether it was revoked in the
           developer portal (Certificates, Identifiers & Profiles).

  FAIL  listing.availability: no app-level availability resource exists (404)
          This 404 is the actual signal, not API noise.
     fix:  The app can be READY_FOR_SALE and still be invisible in every
           storefront. Create availability in App Store Connect > Pricing and
           Availability and confirm you can read it back.

  2 blocking issues
```

## Install

No install required. Clone it and run it:

```bash
git clone https://github.com/Bkessing/appstore-doctor
cd appstore-doctor
python3 -m appstore_doctor --local-only
```

That works on a stock Mac. Everything is standard library except `cryptography`,
which macOS usually already has — if not, `python3 -m pip install cryptography`.

If you'd rather have it on your `PATH` as a command:

```bash
python3 -m pip install .
appstore-doctor --local-only
```

Note that the `pip` bundled with Xcode's Python (3.9, pip 21.x) is too old to
install this and will fail. Use the clone-and-run form above, or a Python from
Homebrew or python.org.

Requires Python 3.9+. The signing checks are macOS-only; the App Store Connect
checks run anywhere.

## Use

Local signing checks need no credentials at all:

```bash
appstore-doctor --local-only
```

Add App Store Connect checks by naming your app:

```bash
appstore-doctor --bundle com.acme.app
appstore-doctor --bundle com.acme.app --project ~/code/MyApp
appstore-doctor --app-id 1234567890 --json
```

## With Claude Code

There's a skill in `skills/appstore-doctor/`. Copy it into your skills
directory and Claude will reach for it whenever you hit a signing or
submission problem:

```bash
# available in every project
mkdir -p ~/.claude/skills
cp -R skills/appstore-doctor ~/.claude/skills/

# or just this project
mkdir -p .claude/skills
cp -R /path/to/appstore-doctor/skills/appstore-doctor .claude/skills/
```

Then ask it things like *"why is my build failing to sign?"* or *"is 1.4 ready
to submit?"* and it will run the checks instead of guessing from the error text.

**If you did not `pip install`,** tell the skill where the clone lives, because
`python3 -m appstore_doctor` only works from inside the repo:

```bash
export PYTHONPATH=/path/to/appstore-doctor
```

The skill also carries a translation table from Apple's error strings to what
they actually mean, which is useful to an agent even before it runs anything.
`No Accounts` is not a login problem, and `errSecInternalComponent` has nothing
to do with the framework it names.

Works the same way in any agent that reads `SKILL.md` files.

### Credentials

Only needed for the App Store Connect checks. From App Store Connect →
Users and Access → Integrations → App Store Connect API:

```bash
export ASC_KEY_ID=XXXXXXXXXX          # not a secret, just an identifier
export ASC_ISSUER_ID=xxxxxxxx-xxxx-…  # same
# and put AuthKey_<KEY_ID>.p8 here:
#   ~/.appstoreconnect/private_keys/
```

A **Developer** role key is enough. The key signs a request locally and talks
straight to Apple — there is no server in the middle, nothing is uploaded, and
every request this tool makes is a `GET`.

## The three tools

| | | |
|---|---|---|
| [ios-bootstrapper](https://github.com/kessinger-tools/ios-bootstrapper) | build the app | paid |
| [ios-release-kit](https://github.com/Bkessing/ios-release-kit) | ship it | free |
| [appstore-doctor](https://github.com/Bkessing/appstore-doctor) | diagnose it | free |

The two free tools are the whole release story and stand on their own — nothing
here is crippled to sell you something. **ios-bootstrapper** is the step before
them: it creates the app itself, with persistence that will not wipe your users
on update, analytics you can filter your own traffic out of, and the purchase and
crash-reporting wiring already done.

## Install both

These are two halves of one job: this one reads, the other writes. Install the
pair and Claude picks whichever the moment calls for.

```bash
git clone https://github.com/Bkessing/appstore-doctor
git clone https://github.com/Bkessing/ios-release-kit

mkdir -p ~/.claude/skills
cp -R appstore-doctor/skills/appstore-doctor  ~/.claude/skills/
cp -R ios-release-kit/skills/ios-release-kit  ~/.claude/skills/
```

They are deliberately separate packages. appstore-doctor issues **GET requests
only** — that is why handing it an API key is reasonable, and it would not
survive being merged into something that can submit an app for review. It also
runs on a stock Mac with no Apple membership and no fastlane, which matters when
the broken thing *is* your fastlane setup.

## What it checks

**Locally, no credentials:**

| Check | The failure it catches |
|---|---|
| Signing identities | Missing Distribution cert → `exportArchive No Accounts` |
| Keychain lock state | Locked keychain → `CodeSign … errSecInternalComponent` |
| Provisioning profiles | Expired, or bound to a certificate you no longer hold |

**Account vs. this Mac:**

| Check | The failure it catches |
|---|---|
| Certificate sync | A certificate your account has but this Mac can't sign with — `No codesigning identities … were found` while the portal looks perfect |
| Certificate expiry | `CSSMERR_TP_CERT_EXPIRED`, and the profiles it silently invalidates |

**Against your project directory** (`--project`, no credentials):

| Check | The failure it catches |
|---|---|
| App icon alpha channel | ITMS-90717 — and note Apple rejects an icon that *has* an alpha channel, so a fully opaque RGBA export still fails |
| Privacy manifest | A linked SDK on Apple's required list with no `PrivacyInfo.xcprivacy` |
| Build number | Uploading a `CURRENT_PROJECT_VERSION` that already exists |

**Against App Store Connect:**

| Check | The failure it catches |
|---|---|
| App availability | Approved, live, and invisible in every territory |
| Stuck review submission | A rejected submission still holding the version, so every resubmit fails against the *version* |
| IDFA declaration | Never answered, which can block a first submission |
| Version state | Rejected or unsubmitted when you thought otherwise |
| Attached build | Submitting a version with no build attached |
| Screenshot sets | Updating 6.5" while 6.9" quietly serves stale artwork |
| Subscription metadata | Guideline 3.1.2 rejections on auto-renewing subs |
| Release type | `MANUAL` when you expected it to ship on approval |
| Privacy policy URL | Guideline 5.1.1 metadata rejection |
| Age rating | Missing declaration blocks submission |
| Review demo account | Guideline 2.1 — a reviewer who can't get past your login |
| In-app purchases | Products stuck in `MISSING_METADATA` that can never sell |

**Free surfaces you are entitled to and not using:**

These never fail a build, which is exactly why they stay empty for the life of an
app. All are reported as warnings, never failures.

| Check | What is being left unused |
|---|---|
| In-App Events | Event cards appear *inside search results* and ship without an app release |
| Custom product pages | 70 slots, each with its own keywords, able to outrank your default page for them, and the only way to attribute off-store traffic |
| Game Center | Initialization gates the Top Played chart and social recommendations. Games only; ignore it on a non-game |
| Export compliance | Missing `ITSAppUsesNonExemptEncryption` blocks submission |

### What it deliberately does not claim

Some things aren't detectable and the tool says so rather than guessing:

- **Whether a reviewer can actually reach your in-app purchases.** IAPs gated
  behind progression cause Guideline 2.1(b) rejections and no API can see it.
  The tool raises it as something to check by hand.
- **Whether your demo credentials actually work.** It can only see that they're set.
- **Agreement status and the App Privacy questionnaire.** Both return 404 on the
  App Store Connect API, so they're out of reach.
- **Whether you've declared every required-reason API.** Apple detects this from
  compiled binaries, including closed-source SDKs. This tool only matches linked
  package *names* against Apple's published list, so it can tell you that you
  probably need a manifest — it cannot tell you that you're clear.
- **Icon Composer `.icon` bundles.** The compiled output can differ from the
  source layers, so the tool says it can't check rather than pretending.

## Why these checks

Every one of them is a specific day someone lost.

**The certificate that vanished.** A distribution certificate and every
provisioning profile disappeared from a developer account between two builds.
The build failed with `exportArchive No Accounts`, which reads like Xcode is
signed out. It wasn't. Recreating the certificate then produced
`CodeSign … errSecInternalComponent` on `Sentry.framework` — which reads like a
dependency problem. It was a locked keychain.

**The profile that looked fine.** After replacing the certificate, export failed
with *"doesn't include signing certificate"* — a cached profile still bound to the
revoked cert. Deleting it produced *"No profiles for 'com.example.app' were
found"*, even with a valid profile installed in both provisioning directories.
That message is misleading: the profile isn't missing. Automatic signing at the
export step needs an Apple ID signed into Xcode, and a headless run doesn't have
one. The fix is manual signing with the profile named explicitly.

**The app nobody could find.** An app sat at `READY_FOR_SALE` for days, fully
approved, and appeared in no storefront. The app-level availability resource
had never been created. The API returned 404 for it the whole time, and that
404 was the only signal.

**The screenshots that didn't change.** A release pipeline updated
`APP_IPHONE_65` and left `APP_IPHONE_67` untouched. The 6.9" set is what modern
iPhones render in search results. Every screenshot refresh for months changed
nothing most people ever saw.

## Exit codes

`0` — no blocking issues. `1` — at least one `FAIL`. Warnings do not fail the
run, so this is safe in CI.

## Scope

This diagnoses. It does not fix, and it does not write. A tool that mutates a
live App Store listing on a bug is not one you should hand an API key to.

## Support

Something it missed, or a false positive? support@brandonkessinger.com. A
failure mode this does not catch yet is the most useful thing you can send.

## License

MIT.
