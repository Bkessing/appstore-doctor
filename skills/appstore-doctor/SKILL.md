---
name: appstore-doctor
description: Diagnose why an iOS app will not build, sign, or ship to the App Store. Use when an Xcode or fastlane build fails with a signing or provisioning error (No Accounts, errSecInternalComponent, "doesn't include signing certificate", "No profiles were found"), when an App Store submission is rejected or stuck, when an approved app is not appearing in the store, or when checking whether a version is ready to submit.
---

# appstore-doctor

Read-only diagnostics for iOS release problems. Never writes to App Store Connect.

## Running it

The tool lives wherever the user cloned it. Work out the path once, then reuse it.

```bash
# If it is on PATH (installed via pip):
appstore-doctor --local-only

# Otherwise run it from anywhere by pointing PYTHONPATH at the clone:
PYTHONPATH=/path/to/appstore-doctor python3 -m appstore_doctor --local-only
```

`python3 -m appstore_doctor` on its own only works from inside the clone. If you
do not know where it is, look before asking:

```bash
command -v appstore-doctor || \
  ls -d ~/appstore-doctor ~/code/appstore-doctor ~/Documents/Code/appstore-doctor 2>/dev/null
```

Ask the user for the path only if that finds nothing. Do not guess a path and
then report a failure that is really just a wrong directory.

## When to use this

Reach for it whenever the user hits a shipping problem rather than a code
problem: signing failures, provisioning failures, rejected or stuck
submissions, an approved app that is not visible, or a pre-submission check.

**Do not diagnose these from the error text.** iOS release errors name a
symptom, not a cause, and reasoning from the message sends you the wrong way:

| Error the user sees | What it usually means |
|---|---|
| `exportArchive No Accounts` | No Apple Distribution certificate, OR export is using automatic signing with no Apple ID in Xcode |
| `No signing certificate "iOS Distribution" found` | The distribution certificate is missing or was revoked |
| `CodeSign … errSecInternalComponent` | The login keychain is locked |
| `No matching codesigning identity found` | The certificate exists but its private key is not in this keychain |
| `doesn't include signing certificate` | The provisioning profile is bound to a certificate that no longer exists |
| `No profiles for '<bundle>' were found` | Usually NOT a missing file. Automatic signing at export cannot authenticate |
| `ITMS-90717 Invalid large app icon` | The icon PNG carries an alpha channel, even if every pixel is opaque |
| `The bundle version must be higher…` | That build number was already uploaded |
| Approved but not in the store | No app-level availability resource |
| `git push` fails with `-25293` | Locked keychain again, this time blocking the credential helper |

Run the tool and read the state instead.

## Usage

```bash
# Local signing checks only, no credentials needed
python3 -m appstore_doctor --local-only

# Add project checks (icon alpha, privacy manifest, build number)
python3 -m appstore_doctor --project /path/to/MyApp

# Add App Store Connect checks
python3 -m appstore_doctor --bundle com.example.app --project /path/to/MyApp

# Machine-readable, for parsing
python3 -m appstore_doctor --bundle com.example.app --json
```

Exit code is `1` if anything is blocking, `0` otherwise. Warnings do not fail.

Prefer `--json` when you intend to act on the result programmatically; the text
output is shaped for humans.

## Credentials

App Store Connect checks need `ASC_KEY_ID`, `ASC_ISSUER_ID`, and the `.p8` at
`~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8` (or `ASC_KEY_FILEPATH`).

If they are absent, say so and run `--local-only` rather than guessing. **Never
ask the user to paste a private key into the conversation** — it belongs in a
file on their machine. The key id and issuer id are identifiers, not secrets;
the `.p8` is a credential.

## Interpreting results

Work the `FAIL` items in order; they block shipping. `WARN` items are worth
raising but do not stop a submission.

Three cases deserve extra care:

**A locked keychain cannot be fixed by an agent.** `security unlock-keychain`
prompts interactively for the user's Mac password. Ask them to run it in their
own terminal; do not try to automate around it.

**Certificates and profiles are coupled.** A provisioning profile embeds the
certificates that existed when it was created, so replacing a certificate
silently invalidates every existing profile while they sit on disk looking
healthy. If the tool reports orphaned profiles, the fix is to delete them AND
create replacements — doing only the first produces a more confusing error than
the one you started with.

**The tool states what it cannot check.** It does not know whether a reviewer
can actually reach an in-app purchase, whether demo credentials work, or whether
every required-reason API is declared. When it says so, relay that honestly
rather than treating a clean run as proof the submission will pass.

## Scope

Diagnoses only. It issues no write requests. If a fix requires changing the
user's App Store Connect account, describe the change and let them make it.
