# Neon Relay

Neon Relay turns the Android archives published by
[Plezy](https://github.com/edde746/plezy) into plain `.apk` files on this
repository's releases, so a device can download an APK directly instead of
unpacking a `.tar.gz` first.

These are unofficial mirror copies. The APK bytes are copied exactly from the
upstream release: nothing is rebuilt, patched, repacked or re-signed. Plezy is
created and maintained by [edde746](https://github.com/edde746); this mirror is
not affiliated with or endorsed by the upstream project. Plezy is distributed
under [GPL-3.0](https://github.com/edde746/plezy/blob/2.25.0/LICENSE).

## Downloads

Mirrored releases are tagged `plezy-v<version>`:

**https://github.com/XBadFellaX/NeonQuark/releases?q=plezy-v**

| Device | File | ABI |
| :--- | :--- | :--- |
| Modern 64-bit phones, tablets and TVs | `plezy-arm64-v8a.apk` | `arm64-v8a` |
| Older 32-bit devices and some TVs | `plezy-armeabi-v7a.apk` | `armeabi-v7a` |

Choose `arm64-v8a` when the device supports it, otherwise `armeabi-v7a`. A
64-bit processor can still run a 32-bit Android OS or ship only 32-bit
libraries, so check the device's supported ABIs if the first file fails to
install. The upstream release does not publish x86 or x86_64 Android builds,
and Neon Relay mirrors only the two ARM ABIs.

Each release also contains:

| File | Purpose |
| :--- | :--- |
| `SHA256SUMS` | SHA-256 of both APKs in `sha256sum -c` format. |
| `provenance.json` | Upstream release id, tag, source commit, asset identities, sizes, hashes and ABI details. |
| `plezy-LICENSE.txt` | The upstream license text at that exact source commit. |

## Verify a download

```sh
sha256sum plezy-arm64-v8a.apk plezy-armeabi-v7a.apk
sha256sum -c SHA256SUMS
```

Compare the result with the values printed in the release notes or in
`provenance.json`. Signature verification is a separate check:

```sh
apksigner verify --verbose --print-certs plezy-arm64-v8a.apk
```

The relay does not run `apksigner`; it preserves the original bytes, so the
signature is the one Plezy published upstream. If the required Android build
tools are unavailable, treat signature and package-version checks as not run
rather than passing.
## How it works

`relay.py` (Python 3.12+, standard library only):

1. Reads the upstream GitHub release metadata for an explicit tag or the
   latest stable release. Drafts, pre-releases, non-numeric tags and missing or
   duplicated assets are rejected.
2. Requires one fully uploaded archive per ABI with a nonzero size and a
   published SHA-256 digest. Missing ARMv7 never degrades into ARM64-only
   success.
3. Downloads only those two assets, bounded to 256 MiB each, and verifies the
   size and SHA-256 from the release metadata.
4. Reads each `.tar.gz` without `extractall`: exactly one regular `.apk` member
   is allowed, and links, device nodes, absolute paths, traversal paths,
   multiple APKs, oversized members and oversized archives are rejected.
5. Validates the APK as a ZIP without rewriting it: manifest present, no
   duplicate names, bounded entry count and uncompressed size, native libraries
   only for the expected ABI, and CRC integrity.
6. Resolves the release tag to its exact source commit, fetches the license at
   that commit, and writes `SHA256SUMS`, `provenance.json` and
   `plezy-LICENSE.txt` next to the two APKs.
7. With `--publish`, creates a **draft** release, uploads all five files, reads
   the assets back and compares their hashes, rechecks upstream metadata, then
   removes the draft flag. The release is never marked `latest`.
Publication is idempotent. If the destination release already exists and is
complete with matching assets and provenance, the run is a no-op. An incomplete
or different published release is an error and is never overwritten, moved or
deleted. A matching draft is resumed and completed; a conflicting draft is left
for manual review. `--latest=false` is always passed so the collection's
repository-wide "Latest" pointer is not captured by this tool.

The relay never writes tokens, machine paths or private project data into
`provenance.json`. Temporary staging directories contain only the five
published files.

## Manual use

Prepare and validate locally (no publication, no write token needed):

```sh
python NeonRelay/relay.py --tag 2.25.0 --output /tmp/neon-relay-out
```

Without `--tag`, the latest stable upstream release is used. Publishing is a
separate, explicit step:

```sh
python NeonRelay/relay.py \
  --tag 2.25.0 \
  --output "$RUNNER_TEMP/neon-relay-out" \
  --publish \
  --repo XBadFellaX/NeonQuark \
  --target-commit "$GITHUB_SHA"
```

`--publish` requires the expected repository and a full commit SHA; the release
tag points at the tested NeonQuark commit, never at an upstream Plezy commit.
`gh` must be authenticated with permission to write releases.

Exit codes: `0` success or no-op, `2` invalid input, `3` upstream metadata or
asset problem, `4` validation failure, `5` publication failure.
## Automation

`.github/workflows/neon-relay.yml` runs hourly at minute 23 and can be started
manually from the Actions tab. Scheduled runs publish only from `main` in
`XBadFellaX/NeonQuark`; a manual run defaults to `dry_run: true`, which prepares
and validates without any write permission. The workflow serializes all runs in
a single `neon-relay` concurrency group, so two triggers cannot create duplicate
releases. GitHub may delay scheduled runs, and it disables schedules in public
repositories after 60 days without repository activity; re-enable the schedule
from the Actions tab if it stops firing.

Failures stay visible as failed runs. A failed upload leaves a draft that the
next matching run can resume; it never exposes a partial public release.

## Limits

- Only `arm64-v8a` and `armeabi-v7a` Android builds are mirrored.
- No Android SDK, APK patching, repacking, signing or device installation is
  performed.
- ZIP and tar validation is not a cryptographic signature check.
- Matching upstream bytes preserves the upstream signature but does not prove
  compatibility with an installed Play Store build or with a specific TV.
- The relay does not reformat or rewrite releases on every run and never
  deletes existing releases.

## Rollback

Disable the **Neon Relay** workflow in the Actions tab to stop new mirrors.
Existing releases and tags remain in place; remove them separately only if that
is intended.

## Tests

```sh
python -m unittest discover -s NeonRelay -p "test_*.py"
node --check ion-drift.user.js
node --test test-ion-drift.cjs
```

The relay tests are offline. They build tiny archives and APK-shaped ZIPs and
replace both the HTTP opener and the `gh` runner, so no test contacts GitHub or
writes to a repository.