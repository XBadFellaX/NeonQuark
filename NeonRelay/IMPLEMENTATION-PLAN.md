# Neon Relay — direct Plezy APK downloads

Status: implementation plan only. No automation or mirrored release exists yet.
Research date: 2026-11-04. Recheck upstream metadata before implementation.

## Goal and scope

Add a small tool to NeonQuark that detects new stable releases of
`edde746/plezy`, extracts the original Android APKs, and attaches them to a
versioned NeonQuark GitHub Release. Users download APKs without unpacking archives.

Support exactly these two architectures:

| User label | Android ABI | Upstream asset | Output asset |
| --- | --- | --- | --- |
| ARM64 / ARM v8 | `arm64-v8a` | `plezy-android-arm64-v8a.tar.gz` | `plezy-arm64-v8a.apk` |
| ARM32 / ARM v7 | `armeabi-v7a` | `plezy-android-armeabi-v7a.tar.gz` | `plezy-armeabi-v7a.apk` |

Copy APK bytes exactly. No compilation, patching, repacking, signing, Android
SDK installation in the scheduled job, x86 downloads, external server, database,
package manager, notification service, or automatic installation on devices.
Preserve Ion Drift, its URLs, version, tests, and banner.

## Confirmed baseline

- Target: `XBadFellaX/NeonQuark`, public, not archived, default branch `main`.
- Inspected local HEAD: `0556dc74af7826680d94fd1ac7c0ce3c7d7173ba`.
- Existing collection has Ion Drift and Node's built-in tests, without a build
  system or third-party runtime dependencies. Keep that small-tool approach.
- Plezy 2.25.0 has `.tar.gz` Android assets, not ZIPs. Both archives were downloaded
  and inspected without installing or executing the app.
- Each archive contains exactly one regular file, `plezy.apk`. Each APK is a ZIP
  with `AndroidManifest.xml` and native libraries for its expected ABI only.
- Signature validity, package/version metadata, and TV installation were not
  verified during planning. File structure is not proof of installability.

| ABI | Archive SHA-256 | Extracted APK SHA-256 | APK bytes |
| --- | --- | --- | --- |
| `arm64-v8a` | `ead4f34b6c71a60b3f7887a9cc83eafc9204e278aea780dc5b402d4aaf04b3c4` | `5d56ce0c76823152a30f8ff5fd580789822b1a6bc88a3c667cabb752388e2ad6` | 98757662 |
| `armeabi-v7a` | `426e06d9c37588566cf3b234b8cb86a8c94ed50f9ad700e9873581aaf2c6b7b5` | `93ab7ce1741794e705dc5309410f8f26ecd92744fd9b80446b62f334607ebe48` | 93683280 |

These hashes are regression evidence for 2.25.0, not an allowlist that prevents
future versions. For future releases verify each archive against its own
GitHub asset digest. If a digest is missing, fail with a clear explanation.

## Minimal file layout

```text
.github/workflows/neon-relay-checks.yml   # read-only tests on PRs/pushes
.github/workflows/neon-relay.yml          # schedule/manual mirror
neon-relay/IMPLEMENTATION-PLAN.md         # this specification
neon-relay/README.md                     # operator and download guide
neon-relay/relay.py                      # Python standard-library implementation
neon-relay/test_relay.py                 # unittest, small generated fixtures
README.md                               # one collection row and scope wording
.gitignore                              # only necessary local output exclusions
```

Do not commit APKs, downloaded archives, generated manifests, logs, or temporary
test output. Do not add a dependency manifest, generic mirroring framework,
dashboard, or second implementation. GitHub workflow files must be at repository
root under `.github/workflows/`, not inside the tool directory.

## Execution contract

Use Python 3.12+ standard library for HTTP, hashing, JSON, tar and ZIP handling,
tests and local preparation. Use the GitHub CLI already present on the Ubuntu
runner for target release operations, invoked with argument arrays and body files.
Keep networking and publishing narrow enough to fake in unit tests.

Local preparation never publishes:

```sh
python neon-relay/relay.py --tag 2.25.0 --output <temporary-output-directory>
python -m unittest discover -s neon-relay -p 'test_*.py'
node --check ion-drift.user.js
node --test test-ion-drift.cjs
```

Without `--tag`, query the upstream latest stable release endpoint. An explicit
tag supports validation and retrying a known stable version. Validate tags as
numeric three-part stable versions with an optional leading `v`; reject drafts,
prereleases, nonnumeric tags, slashes, newlines and shell-like input. Derive the
destination tag deterministically as `plezy-v<version>` with exactly one `v`.
If upstream changes version format, fail rather than guessing.

Publication requires a separate `--publish` flag, the expected target repository,
and the workflow's exact `GITHUB_SHA` as the target commit. Do not publish from
pull requests, forks, or an arbitrary user-supplied target. Use a fresh temporary
staging directory. Avoid fixed shared temp paths or deleting user-selected directories.

### Fetch and validate

1. Fetch upstream release metadata from GitHub's API, with timeouts and bounded
   retries for transient network/server failures. Respect rate-limit responses;
   do not loop indefinitely. Validate status and response shape before proceeding.
2. Require one and only one of each named Android asset, fully uploaded, with a
   nonzero size and SHA-256 digest. Missing ARMv7 must never become ARM64-only
   success. Capture release ID, tag, source commit, asset IDs, URLs and digests.
3. Resolve the tag's source commit, including annotated tags. Record immutable
   source links. Validate that the source and license are available for that
   version. Preserve upstream license and applicable notices. Plezy's inspected
   license is GPL-3.0; link corresponding source clearly beside the binaries,
   including build/dependency material required by the upstream distribution.
   A generic link to a changing default branch is insufficient provenance.
4. Download only the two selected assets. Accept GitHub's normal release-storage
   redirects; never send a privileged target token to redirected download hosts.
   Do not download arbitrary URLs taken from malformed metadata. Stream downloads
   with a practical bound (256 MiB per archive) and verify size and SHA-256.
5. Inspect tar members without `extractall`. Allow ordinary directory entries,
   but require exactly one regular `.apk` member and reject links, device files,
   absolute paths, traversal components and ambiguous/multiple APKs. Limit APK
   size to 512 MiB and member count to 100. Copy the selected member's stream
   into a fixed output filename. Do not use an archive pathname as a destination.
6. Check APK ZIP integrity, duplicate entry names, the manifest, and native ABI
   paths. Bound ZIP entry count and total uncompressed size before CRC checking
   (100,000 entries, 2 GiB total). Require the expected ABI and reject another ABI
   in this architecture-specific output. Never rewrite the APK to validate it.
7. Stage both validated APKs before any target mutation. Generate `SHA256SUMS`
   for the two APKs, `provenance.json`, and `plezy-LICENSE.txt` copied from that
   source revision. Preserve any required additional distribution notices.
   Do not claim source completeness solely because a URL returned HTTP 200.
8. Provenance records upstream repo/release/tag/commit, original asset identities,
   archive and APK sizes/digests, member names, ABI, source/license links and the
   relay code revision. Never include tokens, machine paths, private project
   details or full environment dumps. Use stable content for retry comparisons;
   a newly generated timestamp must not create a false mismatch on every run.
9. Exit nonzero on invalid input. Clearly distinguish no new version, not-ready
   upstream assets, validation failure and publication failure in logs/summary.
   Do not silently fall back to an older release or an alternate download host.

### Publish and recover safely

- Release title: `Neon Relay · Plezy <version>`. Tag: `plezy-v<version>`.
- Release tags point to the tested NeonQuark code commit, never an upstream Plezy
  commit. Use `--latest=false` so this tool does not take over the collection's
  repository-wide Latest pointer. Do not use generic `/releases/latest/download/`
  links as though GitHub has a separate latest pointer per tool.
- Create a draft only after both APKs and required metadata validate. Upload all
  required assets, read back the draft asset set and verify bytes/hashes, then
  publish. No public release should expose just one APK after a failed second upload.
- Use a single global workflow concurrency group with `cancel-in-progress: false`.
  Serializing writes is simpler than a state database.
- For an existing published destination, compare its required asset set and
  provenance against upstream. A complete matching release is a no-op. An
  incomplete or different published release is an error; do not overwrite APKs,
  move its tag, delete it, or use `--clobber` to hide the mismatch.
- Resume an existing draft only when its tag, target commit and provenance match
  this candidate. Verify existing files, upload missing assets, and publish only
  after the complete readback succeeds. Conflicting drafts require operator review.
- Check for a draft before treating a missing published release as permission to
  create another. Handle upload races/errors without altering another release.
- Recheck upstream release/asset identity and digests before final publication;
  if upstream changed during preparation, fail and leave the draft for inspection.
- Release notes credit Plezy's upstream author/project, state that these are
  unchanged extracted APKs in an unofficial mirror, link the upstream release,
  immutable corresponding source and license, and give architecture labels.
  Preserve genuine notices; no automatic agent/model signatures or co-authors.
- This feature must not commit on each scheduled run. Destination releases and
  their provenance are the completed-version record. Do not add heartbeat commits
  solely to defeat GitHub's inactivity policy.

## GitHub Actions contracts

### Read-only checks

- Run on relevant pull requests and pushes, including changes to Ion Drift.
- Use a standard Ubuntu hosted runner, `contents: read`, and no publication token
  or `pull_request_target`. Run Python tests and existing Node checks.
- Tests use generated tiny archives/APK-shaped ZIPs, fake HTTP and fake publisher
  results. No real GitHub writes, signing keys, device access or downloads in unit tests.

### Scheduled/manual relay

- Schedule at minute 23 of each hour (`23 * * * *`) and allow `workflow_dispatch`.
- Manual inputs: optional upstream stable tag; boolean `dry_run`, default true.
  Scheduled runs publish only after this workflow is intentionally enabled on main.
- Require the expected repository, public visibility and default `main` branch
  for publication. On a fork or wrong ref, refuse before mutation. Dry runs do not
  receive write permission: separate read-only dry-run and write-enabled publishing
  jobs with mutually exclusive conditions; share the same script, not copied logic.
- Use a standard Ubuntu runner; no larger runners, paid external compute, caches
  or retained Actions APK artifacts. Downloads belong in GitHub Releases, where
  users receive `.apk` files directly rather than Actions artifact ZIPs.
- Use `contents: write` only in the publish job. Use the ephemeral `GITHUB_TOKEN`,
  not a personal access token. Owner routing helpers are for local maintenance;
  they must not be copied into hosted Actions or require private RepoPilot files.
- Checkout the exact triggering revision; disable persisted checkout credentials.
  Pin third-party actions to reviewed full commit SHAs and document their versions.
  Avoid unnecessary actions and silently installing newer tool versions.
- Run the relevant checks at that exact revision before any publishing step.
  Pull-request CI success on an older revision does not satisfy this requirement.
- Pass workflow inputs through environment variables or argument arrays; never
  interpolate tag/release strings directly into executable shell source.
- Set a finite job timeout, e.g. 20 minutes. Log version, both ABIs, checks and
  final URLs in the job summary; never tokens. A failure remains a failed run.

## Acceptance tests Gemini must independently assess

| Case | Required result |
| --- | --- |
| Normal pair of generated archives | Two correctly named APKs and correct checksums/provenance |
| Upstream 2.25.0 local preparation | Both APK hashes and sizes match the baseline table |
| Stable selection and explicit tag | Correct tag; drafts/prereleases/unsafe inputs rejected |
| One missing, duplicate, zero-byte or partially uploaded asset | No target mutation; clear error |
| HTTP failure, timeout or exhausted rate limit | Bounded handling; no false success or partial publication |
| Truncated archive, mismatched size/digest | Rejected before creating a draft |
| Unsafe tar path, symlink, hard link, extra APK, size bomb | Rejected; no writes outside staging |
| Corrupt/non-APK ZIP, duplicate ZIP name, missing manifest, wrong ABI | Rejected before publication |
| Metadata/provenance and source/license | Exact version and identities; no credentials/private paths |
| Same complete published release twice | Second run changes no release, tag or asset |
| Conflicting or incomplete published release | Error without overwrite/deletion |
| Interrupted draft upload | Matching draft resumes; mismatch does not clobber |
| Upstream asset changes mid-run | No publication using inconsistent metadata |
| Failure during final publication | Failed run and recoverable draft; no success claim |
| Dry run / PR / fork / wrong branch | No write-capable publication path |
| Two concurrent triggers | Single serialized publisher; no duplicate release |
| Ion Drift regression | Existing Node checks pass and script/install URLs are unchanged |
| Published downloads, when authorized | Anonymous downloads of both APKs match extracted bytes |

Signature acceptance: use existing Android build tools if available to run
`apksigner verify --verbose --print-certs` on both real APKs, and read package,
version and minSdk with available Android metadata tools. Record actual tool
versions/results. Do not install a full Android toolchain just for this script.
If those tools are unavailable, explicitly mark signature/metadata checks NOT RUN;
do not describe ZIP validation as cryptographic signature validation. Store any
manually confirmed signer fingerprint as evidence, not a fabricated trust anchor.

TV acceptance is separate: check the device's supported ABI list and Android
version before choosing an APK. A 64-bit processor may run a 32-bit Android OS.
Without user-authorized device access, mark installation, launch, remote control
navigation and update compatibility NOT RUN. Never uninstall an existing app or
wipe its data to make a test pass. Matching original bytes preserves the upstream
signature, but does not prove compatibility with an installed Play Store build.

## Documentation and publication sequence

1. Implement locally on a scope-appropriate branch such as `feat/plezy-apk-relay`;
   preserve existing work and inspect scoped instructions before changing files.
2. Review actual diff and run focused tests. The root README adds a Neon Relay
   row linking its guide. Scope the existing browser-only claims to Ion Drift so
   the collection does not describe this Python tool as a browser userscript.
3. Tool README explains architecture choice, direct downloads, source/credit,
   manual dry run, hourly latency, failure/retry behavior and limits. Link the
   filtered release list using `releases?q=plezy-v`; publish exact versioned APK
   URLs in release notes/summary. Never fabricate a working release link before
   that release exists. Do not rewrite the README on every run.
4. Public schedules may be delayed/dropped and disable after 60 days without
   repository activity. Document checking Actions and re-enabling the schedule;
   do not promise perpetual maintenance-free operation or invent a keepalive bot.
5. Finish local review first. Actual public commit/push/PR/merge/dispatch/release
   needs task-specific publication authorization. This plan alone does not grant it.
   Reuse valid evidence rather than restarting checks without a reason.
6. When authorized, follow repository protection rules, inspect commit attribution
   with `git log --format=full BASE..HEAD`, verify final CI/head, then run an initial
   2.25.0 dry run and publish. Read back published notes/assets and anonymously
   download both APKs. Compare their digests with the original extracted payloads.
   Repeat the run to prove no-op behavior. Verify scheduled configuration separately;
   manual execution does not prove a scheduled event has fired.
7. Document disabling the relay workflow as rollback. Preserve existing releases
   and tags unless removal is separately requested. Fix implementation defects
   before calling the setup complete; disclose remaining runtime gaps.

## Primary references

- [Plezy 2.25.0](https://github.com/edde746/plezy/releases/tag/2.25.0)
- [Actual release asset list](https://github.com/edde746/plezy/releases/expanded_assets/2.25.0)
- [Plezy license at 2.25.0](https://github.com/edde746/plezy/blob/2.25.0/LICENSE)
- [Actions billing: public standard runners are free](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Release/schedule event behavior and inactivity limit](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [Release assets and quotas](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
