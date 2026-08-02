---
name: appstore-doctor
description: Diagnose why an iOS app will not build, sign, or ship to the App Store. Use when an Xcode or fastlane build fails with a signing or provisioning error (No Accounts, errSecInternalComponent, "doesn't include signing certificate", "No profiles were found"), when an App Store submission is rejected or stuck, when an approved app is not appearing in the store, or when checking whether a version is ready to submit.
---

# appstore-doctor

Read-only diagnostics for iOS release problems. Never writes to App Store Connect.

## When to use this

Reach for it whenever the user hits a shipping problem rather than a code
problem. Signing failures, provisioning failures, rejected or stuck
submissions, an approved app that isn't visible, or a pre-submission check.

**Do not diagnose these from the error text.** iOS release errors name a
symptom, not a cause, and reasoning from the message sends you the wrong way:

| Error the user sees | What it usually means |
|---|---|
| `exportArchive No Accounts` | No Apple Distribution certificate, OR export is using automatic signing with no Apple ID in Xcode |
| `No signing certificate "iOS Distribution" found` | The distribution certificate is missing or was revoked |
| `CodeSign … errSecInternalComponent` | The login keychain is locked |
| `doesn't include signing certificate` | The provisioning profile is bound to a certificate that no longer exists |
| `No profiles for '<bundle>' were found` | Usually NOT a missing file. Automatic signing at export can't authenticate |
| Approved but not in the store | No app-level availability resource |
| `git push` fails with `-25293` | Locked keychain again, this time blocking the credential helper |

Run the tool and read the state instead.

## Usage

```bash
# Local signing checks only, no credentials needed
python -m appstore_doctor --local-only

# Include App Store Connect checks
python -m appstore_doctor --bundle com.example.app

# Machine-readable
python -m appstore_doctor --bundle com.example.app --json
```

Exit code is `1` if anything is blocking, `0` otherwise. Warnings don't fail.

## Credentials

App Store Connect checks need `ASC_KEY_ID`, `ASC_ISSUER_ID`, and the `.p8` at
`~/.appstoreconnect/private_keys/AuthKey_<KEY_ID>.p8` (or `ASC_KEY_FILEPATH`).

If they're absent, say so and run `--local-only` rather than guessing. Never
ask the user to paste a private key into the conversation — it belongs in a
file on their machine.

## Interpreting results

Work the `FAIL` items in order; they block shipping. `WARN` items are worth
raising but don't stop a submission.

Two failure modes deserve extra care:

**A locked keychain cannot be fixed by an agent.** `security unlock-keychain`
prompts for the user's Mac password interactively. Ask them to run it in their
own terminal; don't try to automate around it.

**Certificates and profiles are coupled.** A provisioning profile embeds the
certificates that existed when it was created, so replacing a certificate
silently invalidates every existing profile while they still sit on disk
looking healthy. If the tool reports orphaned profiles, the fix is to delete
them AND create replacements — doing only the first produces a more confusing
error than the one you started with.

## Scope

Diagnoses only. If a fix requires changing the user's App Store Connect
account, describe the change and let them make it.
