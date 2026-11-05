#!/usr/bin/env python3
"""Neon Relay - mirror Plezy Android APKs into NeonQuark releases.

The script prepares two APKs from the upstream ``edde746/plezy`` release
archives. It only publishes when ``--publish`` is combined with the expected
destination repository and an explicit target commit.

Everything outside the GitHub CLI uses the Python standard library. Tests
replace the HTTP opener and the ``gh`` process runner, so no test touches the
network or a real repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

RELAY_VERSION = "1.0.0"

UPSTREAM_REPO = "edde746/plezy"
UPSTREAM_API = "https://api.github.com"
UPSTREAM_RAW = "https://raw.githubusercontent.com"
UPSTREAM_SITE = "https://github.com"
EXPECTED_DESTINATION_REPO = "XBadFellaX/NeonQuark"

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_APK_BYTES = 512 * 1024 * 1024
MAX_TAR_MEMBERS = 100
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
MAX_LICENSE_BYTES = 1 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_TEXT_ASSET_BYTES = 8 * 1024 * 1024
READ_CHUNK = 1024 * 1024

USER_AGENT = f"NeonRelay/{RELAY_VERSION} (+{UPSTREAM_SITE}/{EXPECTED_DESTINATION_REPO})"

VERSION_PATTERN = re.compile(r"v?(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
SHA256_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
EXIT_OK = 0
EXIT_INPUT = 2
EXIT_UPSTREAM = 3
EXIT_VALIDATION = 4
EXIT_PUBLICATION = 5


class RelayError(Exception):
    """Base class for expected failures with a distinct exit code."""

    exit_code = 1


class InputError(RelayError):
    exit_code = EXIT_INPUT


class UpstreamError(RelayError):
    exit_code = EXIT_UPSTREAM


class AssetNotReady(UpstreamError):
    exit_code = EXIT_UPSTREAM


class ValidationError(RelayError):
    exit_code = EXIT_VALIDATION


class PublishError(RelayError):
    exit_code = EXIT_PUBLICATION


class ConflictError(PublishError):
    exit_code = EXIT_PUBLICATION


@dataclass(frozen=True)
class ArchSpec:
    abi: str
    upstream_asset: str
    output_asset: str


ARCHITECTURES: tuple[ArchSpec, ...] = (
    ArchSpec("arm64-v8a", "plezy-android-arm64-v8a.tar.gz", "plezy-arm64-v8a.apk"),
    ArchSpec("armeabi-v7a", "plezy-android-armeabi-v7a.tar.gz", "plezy-armeabi-v7a.apk"),
)

REQUIRED_ASSETS: tuple[str, ...] = tuple(
    spec.output_asset for spec in ARCHITECTURES
) + ("SHA256SUMS", "provenance.json", "plezy-LICENSE.txt")


def normalize_version_tag(tag: str) -> str:
    """Return ``X.Y.Z`` for a stable version tag, rejecting unsafe input."""
    if not isinstance(tag, str):
        raise InputError("release tag must be a string")
    if not tag or len(tag) > 32:
        raise InputError(f"invalid release tag: {tag!r}")
    if tag != tag.strip():
        raise InputError(f"invalid release tag: {tag!r}")
    match = VERSION_PATTERN.fullmatch(tag)
    if match is None:
        raise InputError(
            f"release tag {tag!r} is not a stable numeric version "
            "(expected MAJOR.MINOR.PATCH with an optional leading 'v')"
        )
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def destination_tag(version: str) -> str:
    return f"plezy-v{version}"


def release_title(version: str) -> str:
    return f"Neon Relay \u00b7 Plezy {version}"


def validate_target_commit(commit: str) -> str:
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        raise InputError("--target-commit must be a full 40 or 64 character commit SHA")
    return commit.lower()


def normalize_repo(repo: str) -> str:
    if not isinstance(repo, str):
        raise InputError("--repo must be a string")
    value = repo.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise InputError(f"invalid repository: {repo!r}")
    return value

def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_rate_limited(headers: Mapping[str, str] | None) -> bool:
    if not headers:
        return False
    remaining = headers.get("X-RateLimit-Remaining")
    return isinstance(remaining, str) and remaining.strip() == "0"


def _rate_limit_message(url: str, headers: Mapping[str, str] | None) -> str:
    reset = (headers or {}).get("X-RateLimit-Reset")
    suffix = ""
    if reset and str(reset).isdigit():
        seconds = max(0, int(reset) - int(time.time()))
        suffix = f"; retry after about {seconds}s"
    return f"GitHub API rate limit exhausted for {url}{suffix}"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Drop credentials when a redirect leaves the original host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).hostname
        new_host = urllib.parse.urlsplit(newurl).hostname
        if old_host != new_host:
            for header in ("Authorization", "authorization"):
                new.remove_header(header)
                new.unredirected_hdrs.pop(header, None)
        return new

class HttpClient:
    """Small bounded HTTP client with retries and integrity checks."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        attempts: int = 4,
        backoff: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.backoff = backoff
        self._sleeper = sleeper
        self._user_agent = user_agent
        if opener is None:
            opener = urllib.request.build_opener(_SafeRedirectHandler()).open
        self._opener = opener

    def _open(self, url: str, headers: Mapping[str, str] | None = None) -> Any:
        request_headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/vnd.github+json",
        }
        if headers:
            request_headers.update(headers)

        last_reason = "unknown error"
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(url, headers=request_headers)
            try:
                return self._opener(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    exc.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
                if status == 429 or (status in (403, 429) and _is_rate_limited(exc.headers)):
                    raise UpstreamError(_rate_limit_message(url, exc.headers)) from exc
                if status >= 500 and attempt < self.attempts:
                    self._sleeper(self.backoff * attempt)
                    last_reason = f"HTTP {status}"
                    continue
                raise UpstreamError(f"HTTP {status} for {url}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                last_reason = f"network failure for {url}: {reason}"
                if attempt < self.attempts:
                    self._sleeper(self.backoff * attempt)
                    continue
                raise UpstreamError(last_reason) from exc
        raise UpstreamError(last_reason)

    @staticmethod
    def _read_bounded(response: Any, max_bytes: int, what: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValidationError(f"{what} exceeds the {max_bytes} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def get_json(self, url: str, *, token: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        response = self._open(url, headers)
        try:
            body = self._read_bounded(response, MAX_JSON_BYTES, f"JSON response from {url}")
        finally:
            response.close()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError(f"invalid JSON response from {url}") from exc

    def get_bytes(self, url: str, *, max_bytes: int, what: str, token: str | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        response = self._open(url, headers)
        try:
            return self._read_bounded(response, max_bytes, what)
        finally:
            response.close()

    def download(
        self,
        url: str,
        destination: Path,
        *,
        max_bytes: int,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        what: str = "asset",
    ) -> tuple[int, str]:
        if expected_size is not None and expected_size <= 0:
            raise ValidationError(f"{what} declares an empty size")
        if expected_size is not None and expected_size > max_bytes:
            raise ValidationError(
                f"{what} declares {expected_size} bytes, above the {max_bytes} byte limit"
            )

        response = self._open(url, {"Accept": "application/octet-stream"})
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        hasher = hashlib.sha256()
        total = 0
        try:
            declared = (response.headers or {}).get("Content-Length")
            if declared is not None:
                try:
                    declared_bytes = int(declared)
                except (TypeError, ValueError):
                    declared_bytes = None
                if declared_bytes is not None and declared_bytes > max_bytes:
                    raise ValidationError(
                        f"{what} is {declared_bytes} bytes, above the {max_bytes} byte limit"
                    )
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValidationError(f"{what} exceeds the {max_bytes} byte limit")
                    hasher.update(chunk)
                    handle.write(chunk)
        finally:
            response.close()

        try:
            if expected_size is not None and total != expected_size:
                raise ValidationError(
                    f"{what} size mismatch: expected {expected_size} bytes, received {total}"
                )
            digest = hasher.hexdigest()
            if expected_sha256 is not None and digest != expected_sha256.lower():
                raise ValidationError(
                    f"{what} SHA-256 mismatch: expected {expected_sha256.lower()}, received {digest}"
                )
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return total, digest


@dataclass(frozen=True)
class ReleaseAsset:
    asset_id: int
    name: str
    size: int
    state: str
    digest: str | None
    download_url: str

    @property
    def sha256(self) -> str:
        if not self.digest:
            raise AssetNotReady(f"{self.name} has no SHA-256 digest")
        return self.digest.split(":", 1)[1]


@dataclass(frozen=True)
class ReleaseInfo:
    release_id: int
    tag: str
    draft: bool
    prerelease: bool
    assets: tuple[ReleaseAsset, ...]

    @property
    def version(self) -> str:
        return normalize_version_tag(self.tag)

def parse_release(payload: Any) -> ReleaseInfo:
    if not isinstance(payload, Mapping):
        raise UpstreamError("release metadata is not a JSON object")
    release_id = payload.get("id")
    tag = payload.get("tag_name")
    draft = payload.get("draft")
    prerelease = payload.get("prerelease")
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise UpstreamError("release metadata is missing a valid id")
    if not isinstance(tag, str) or not tag:
        raise UpstreamError("release metadata is missing a tag name")
    try:
        normalize_version_tag(tag)
    except InputError as exc:
        raise UpstreamError(f"upstream tag {tag!r} is not a stable version") from exc
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise UpstreamError("release metadata is missing draft/prerelease flags")
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        raise UpstreamError("release metadata is missing an asset list")
    assets: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, Mapping):
            raise UpstreamError("release asset metadata is not a JSON object")
        asset_id = item.get("id")
        name = item.get("name")
        size = item.get("size")
        state = item.get("state")
        digest = item.get("digest")
        url = item.get("browser_download_url")
        if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
            raise UpstreamError("release asset metadata is missing a valid id")
        if not isinstance(name, str) or not name:
            raise UpstreamError("release asset metadata is missing a name")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise UpstreamError(f"release asset {name!r} has an invalid size")
        if not isinstance(state, str):
            raise UpstreamError(f"release asset {name!r} has an invalid state")
        if digest is not None and not isinstance(digest, str):
            raise UpstreamError(f"release asset {name!r} has an invalid digest")
        if not isinstance(url, str):
            url = ""
        assets.append(
            ReleaseAsset(
                asset_id=asset_id,
                name=name,
                size=size,
                state=state,
                digest=digest,
                download_url=url,
            )
        )
    return ReleaseInfo(
        release_id=release_id,
        tag=tag,
        draft=draft,
        prerelease=prerelease,
        assets=tuple(assets),
    )

def _is_allowed_download_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return False
    prefix = f"/{UPSTREAM_REPO}/releases/download/"
    return parsed.path.startswith(prefix)


def select_assets(info: ReleaseInfo) -> dict[str, ReleaseAsset]:
    by_name: dict[str, list[ReleaseAsset]] = {}
    for asset in info.assets:
        by_name.setdefault(asset.name, []).append(asset)

    selected: dict[str, ReleaseAsset] = {}
    for spec in ARCHITECTURES:
        matches = by_name.get(spec.upstream_asset, [])
        if not matches:
            raise AssetNotReady(f"upstream release {info.tag} has no {spec.upstream_asset} asset")
        if len(matches) > 1:
            raise AssetNotReady(
                f"upstream release {info.tag} has {len(matches)} copies of {spec.upstream_asset}"
            )
        asset = matches[0]
        if asset.state != "uploaded":
            raise AssetNotReady(f"{asset.name} is not fully uploaded (state {asset.state!r})")
        if asset.size <= 0:
            raise AssetNotReady(f"{asset.name} is empty")
        if asset.size > MAX_ARCHIVE_BYTES:
            raise ValidationError(
                f"{asset.name} is {asset.size} bytes, above the {MAX_ARCHIVE_BYTES} byte limit"
            )
        if not asset.digest:
            raise AssetNotReady(
                f"{asset.name} has no SHA-256 digest; refusing to mirror unverifiable bytes"
            )
        if SHA256_DIGEST_PATTERN.fullmatch(asset.digest) is None:
            raise ValidationError(f"{asset.name} has an unsupported digest {asset.digest!r}")
        if not _is_allowed_download_url(asset.download_url):
            raise ValidationError(
                f"{asset.name} points at an unexpected download URL; refusing to follow it"
            )
        selected[spec.abi] = asset
    return selected

def resolve_source_commit(http: Any, tag: str) -> str:
    payload = http.get_json(
        f"{UPSTREAM_API}/repos/{UPSTREAM_REPO}/git/ref/tags/{urllib.parse.quote(tag, safe='')}"
    )
    if not isinstance(payload, Mapping):
        raise UpstreamError("tag reference metadata is not a JSON object")
    target = payload.get("object")
    for _ in range(5):
        if not isinstance(target, Mapping):
            raise UpstreamError("tag reference metadata is missing its object")
        sha = target.get("sha")
        kind = target.get("type")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise UpstreamError("tag reference metadata has an invalid commit id")
        if kind == "commit":
            return sha
        if kind != "tag":
            raise UpstreamError(f"tag {tag!r} points at an unsupported object type {kind!r}")
        nested = http.get_json(f"{UPSTREAM_API}/repos/{UPSTREAM_REPO}/git/tags/{sha}")
        if not isinstance(nested, Mapping):
            raise UpstreamError("annotated tag metadata is not a JSON object")
        target = nested.get("object")
    raise UpstreamError(f"tag {tag!r} has too many levels of indirection")

def _validate_tar_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if not name or "\x00" in name:
        raise ValidationError("archive contains a member with an empty or NUL name")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValidationError(f"archive contains an absolute path: {name!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValidationError(f"archive contains a drive-qualified path: {name!r}")
    if ".." in normalized.split("/"):
        raise ValidationError(f"archive contains a traversal path: {name!r}")
    if member.issym() or member.islnk():
        raise ValidationError(f"archive contains a link: {name!r}")
    if member.ischr() or member.isblk() or member.isfifo():
        raise ValidationError(f"archive contains a device node: {name!r}")
    if not (member.isdir() or member.isreg()):
        raise ValidationError(f"archive contains an unsupported member type: {name!r}")

def extract_single_apk(archive: Path, destination: Path) -> tuple[str, int, str]:
    """Copy the only regular ``.apk`` member out of a ``.tar.gz`` archive."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if not members:
                raise ValidationError("archive is empty")
            if len(members) > MAX_TAR_MEMBERS:
                raise ValidationError(
                    f"archive has {len(members)} members, above the {MAX_TAR_MEMBERS} limit"
                )
            for member in members:
                _validate_tar_member(member)

            apk_members = [
                member
                for member in members
                if member.isreg() and member.name.lower().endswith(".apk")
            ]
            if not apk_members:
                raise ValidationError("archive contains no .apk member")
            if len(apk_members) > 1:
                names = ", ".join(sorted(member.name for member in apk_members))
                raise ValidationError(f"archive contains multiple .apk members: {names}")
            member = apk_members[0]
            if member.size <= 0:
                raise ValidationError("archive contains an empty .apk member")
            if member.size > MAX_APK_BYTES:
                raise ValidationError(
                    f"{member.name} is {member.size} bytes, above the {MAX_APK_BYTES} limit"
                )

            source = tar.extractfile(member)
            if source is None:
                raise ValidationError(f"could not read {member.name} from the archive")

            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".part")
            hasher = hashlib.sha256()
            total = 0
            try:
                with source, temporary.open("wb") as handle:
                    while True:
                        chunk = source.read(READ_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_APK_BYTES:
                            raise ValidationError(
                                f"{member.name} exceeds the {MAX_APK_BYTES} byte limit"
                            )
                        hasher.update(chunk)
                        handle.write(chunk)
                if total != member.size:
                    raise ValidationError(
                        f"{member.name} is truncated: expected {member.size} bytes, read {total}"
                    )
                os.replace(temporary, destination)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            return member.name, total, hasher.hexdigest()
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ValidationError(f"unreadable archive {archive.name}: {exc}") from exc


@dataclass(frozen=True)
class ApkInfo:
    entry_count: int
    uncompressed_bytes: int
    native_abis: tuple[str, ...]

def validate_apk(path: Path, expected_abi: str) -> ApkInfo:
    """Check ZIP integrity and native library ABIs without rewriting the APK."""
    if not zipfile.is_zipfile(path):
        raise ValidationError(f"{path.name} is not a ZIP archive")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ValidationError(
                    f"{path.name} has {len(infos)} entries, above the {MAX_ZIP_ENTRIES} limit"
                )
            names = [info.filename for info in infos]
            seen: set[str] = set()
            duplicates: set[str] = set()
            for name in names:
                if name in seen:
                    duplicates.add(name)
                seen.add(name)
            if duplicates:
                raise ValidationError(
                    f"{path.name} has duplicate ZIP entries: {', '.join(sorted(duplicates))}"
                )

            total = sum(info.file_size for info in infos)
            if total > MAX_ZIP_UNCOMPRESSED_BYTES:
                raise ValidationError(
                    f"{path.name} declares {total} uncompressed bytes, above the "
                    f"{MAX_ZIP_UNCOMPRESSED_BYTES} byte limit"
                )
            if "AndroidManifest.xml" not in seen:
                raise ValidationError(f"{path.name} has no AndroidManifest.xml")
            abis: set[str] = set()
            for name in names:
                parts = name.split("/")
                if len(parts) >= 3 and parts[0] == "lib" and parts[1]:
                    abis.add(parts[1])

            if expected_abi not in abis:
                raise ValidationError(f"{path.name} has no native libraries for {expected_abi}")
            foreign = sorted(abis - {expected_abi})
            if foreign:
                raise ValidationError(
                    f"{path.name} contains native libraries for unexpected ABIs: {', '.join(foreign)}"
                )
            native_files = [
                name
                for name in names
                if name.startswith(f"lib/{expected_abi}/") and not name.endswith("/")
            ]
            if not native_files:
                raise ValidationError(f"{path.name} has an empty lib/{expected_abi}/ directory")

            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValidationError(f"{path.name} has a corrupt ZIP entry: {corrupt}")
            return ApkInfo(
                entry_count=len(infos),
                uncompressed_bytes=total,
                native_abis=tuple(sorted(abis)),
            )
    except zipfile.BadZipFile as exc:
        raise ValidationError(f"{path.name} is not a valid ZIP archive: {exc}") from exc

@dataclass(frozen=True)
class StagedArch:
    abi: str
    upstream_asset: ReleaseAsset
    output_asset: str
    archive_size: int
    archive_sha256: str
    apk_size: int
    apk_sha256: str
    member_name: str
    apk_info: ApkInfo


@dataclass(frozen=True)
class StagedRelease:
    version: str
    tag: str
    title: str
    output_dir: Path
    upstream_tag: str
    upstream_release_id: int
    source_commit: str
    architectures: tuple[StagedArch, ...]
    license_sha256: str
    license_size: int
    files: Mapping[str, Path]
    file_sizes: Mapping[str, int]
    file_sha256: Mapping[str, str]
    sha256sums_bytes: bytes
    provenance: Mapping[str, Any]
    provenance_bytes: bytes
    notes: str

def build_provenance(
    *,
    version: str,
    info: ReleaseInfo,
    source_commit: str,
    architectures: Sequence[StagedArch],
    license_sha256: str,
    license_size: int,
    relay_commit: str | None,
) -> dict[str, Any]:
    license_url = f"{UPSTREAM_SITE}/{UPSTREAM_REPO}/blob/{source_commit}/LICENSE"
    return {
        "schema": "neon-relay/provenance/v1",
        "relay": {
            "name": "Neon Relay",
            "version": RELAY_VERSION,
            "commit": relay_commit,
        },
        "upstream": {
            "repository": UPSTREAM_REPO,
            "release_id": info.release_id,
            "tag": info.tag,
            "version": version,
            "release_url": (
                f"{UPSTREAM_SITE}/{UPSTREAM_REPO}/releases/tag/"
                f"{urllib.parse.quote(info.tag, safe='')}"
            ),
            "source_commit": source_commit,
            "source_tree_url": f"{UPSTREAM_SITE}/{UPSTREAM_REPO}/tree/{source_commit}",
            "source_archive_url": (
                f"{UPSTREAM_SITE}/{UPSTREAM_REPO}/archive/{source_commit}.tar.gz"
            ),
            "license": {
                "spdx": "GPL-3.0",
                "asset": "plezy-LICENSE.txt",
                "url": license_url,
                "sha256": license_sha256,
                "size": license_size,
            },
        },
        "destination": {
            "repository": EXPECTED_DESTINATION_REPO,
            "tag": destination_tag(version),
            "title": release_title(version),
        },
        "architectures": [
            {
                "abi": arch.abi,
                "output_asset": arch.output_asset,
                "upstream_asset": {
                    "id": arch.upstream_asset.asset_id,
                    "name": arch.upstream_asset.name,
                    "size": arch.archive_size,
                    "sha256": arch.archive_sha256,
                    "url": arch.upstream_asset.download_url,
                },
                "apk": {
                    "size": arch.apk_size,
                    "sha256": arch.apk_sha256,
                    "archive_member": arch.member_name,
                    "native_abis": list(arch.apk_info.native_abis),
                    "zip_entries": arch.apk_info.entry_count,
                    "zip_uncompressed_bytes": arch.apk_info.uncompressed_bytes,
                },
            }
            for arch in architectures
        ],
    }


def build_sha256sums(architectures: Sequence[StagedArch]) -> bytes:
    ordered = sorted(architectures, key=lambda arch: arch.output_asset)
    lines = [f"{arch.apk_sha256}  {arch.output_asset}\n" for arch in ordered]
    return "".join(lines).encode("utf-8")

def build_notes(
    version: str,
    info: ReleaseInfo,
    source_commit: str,
    architectures: Sequence[StagedArch],
) -> str:
    tag = destination_tag(version)
    rows = []
    for arch in architectures:
        label = (
            "64-bit ARM (arm64-v8a)"
            if arch.abi == "arm64-v8a"
            else "32-bit ARM (armeabi-v7a)"
        )
        url = (
            f"{UPSTREAM_SITE}/{EXPECTED_DESTINATION_REPO}/releases/download/"
            f"{tag}/{arch.output_asset}"
        )
        rows.append(
            f"| {label} | [{arch.output_asset}]({url}) | "
            f"{arch.apk_size} bytes | `{arch.apk_sha256}` |"
        )
    table = "\n".join(rows)
    upstream_tag = urllib.parse.quote(info.tag, safe="")
    return (
        f"## Neon Relay \u00b7 Plezy {version}\n\n"
        f"Unofficial mirror of the Android builds from "
        f"[Plezy {info.tag}]({UPSTREAM_SITE}/{UPSTREAM_REPO}/releases/tag/{upstream_tag}) "
        f"by [edde746]({UPSTREAM_SITE}/edde746).\n\n"
        "These are the unchanged APKs extracted from the upstream release archives. "
        "No patching, repacking or re-signing was performed.\n\n"
        "| Device | Download | Size | SHA-256 |\n"
        "| :--- | :--- | :--- | :--- |\n"
        f"{table}\n\n"
        "- Check a download with `sha256sum -c SHA256SUMS` after downloading both APKs "
        "and `SHA256SUMS`.\n"
        "- A 64-bit processor can still run a 32-bit Android OS: check the device's "
        "supported ABI before choosing a file.\n"
        f"- Corresponding source for this version: "
        f"[tree]({UPSTREAM_SITE}/{UPSTREAM_REPO}/tree/{source_commit}) \u00b7 "
        f"[archive]({UPSTREAM_SITE}/{UPSTREAM_REPO}/archive/{source_commit}.tar.gz)\n"
        f"- License: GPL-3.0 \u2014 "
        f"[LICENSE at {source_commit[:12]}]"
        f"({UPSTREAM_SITE}/{UPSTREAM_REPO}/blob/{source_commit}/LICENSE)\n"
        "- `provenance.json` in this release records upstream identities, sizes and "
        "SHA-256 digests.\n\n"
        "Plezy is created and maintained by edde746. This mirror is not affiliated with "
        "or endorsed by the upstream project.\n"
    )

def prepare_output_dir(output_dir: Path | None) -> Path:
    if output_dir is None:
        return Path(tempfile.mkdtemp(prefix="neon-relay-stage-"))
    path = Path(output_dir)
    if path.exists():
        if not path.is_dir():
            raise InputError(f"output path is not a directory: {path}")
        if any(path.iterdir()):
            raise InputError(f"output directory is not empty: {path}")
    else:
        path.mkdir(parents=True, exist_ok=False)
    return path


def _git_head() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if COMMIT_PATTERN.fullmatch(value) else None


def default_relay_commit() -> str | None:
    return os.environ.get("GITHUB_SHA") or _git_head()


def _provenance_without_relay(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "relay"}


def provenance_equivalent(existing: bytes, candidate: Mapping[str, Any]) -> bool:
    try:
        decoded = json.loads(existing.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(decoded, Mapping):
        return False
    return _provenance_without_relay(decoded) == _provenance_without_relay(candidate)

class GhPublisher:
    """Narrow wrapper around the GitHub CLI for release mutations."""

    def __init__(
        self,
        repo: str,
        *,
        token: str | None = None,
        gh: str = "gh",
        runner: Callable[[Sequence[str], Mapping[str, str]], tuple[int, bytes, bytes]]
        | None = None,
    ) -> None:
        self.repo = normalize_repo(repo)
        self._token = token
        self._gh = gh
        self._runner = runner or self._subprocess_runner

    @staticmethod
    def _subprocess_runner(
        argv: Sequence[str], env: Mapping[str, str]
    ) -> tuple[int, bytes, bytes]:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            env=dict(env),
            timeout=300,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def _run(self, args: Sequence[str]) -> tuple[int, bytes, bytes]:
        env = os.environ.copy()
        env["GH_PROMPT_DISABLED"] = "1"
        if self._token:
            env["GH_TOKEN"] = self._token
            env["GITHUB_TOKEN"] = self._token
        argv = [self._gh, *args]
        return self._runner(argv, env)

    @staticmethod
    def _failure(
        args: Sequence[str], code: int, stdout: bytes, stderr: bytes
    ) -> PublishError:
        detail = (stderr or stdout).decode("utf-8", "replace").strip()
        return PublishError(
            f"gh {' '.join(args[:2])} failed (exit {code}): {detail}"
        )

    def _json(self, args: Sequence[str]) -> Any:
        code, stdout, stderr = self._run(args)
        if code != 0:
            raise self._failure(args, code, stdout, stderr)
        try:
            return json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublishError(f"gh returned invalid JSON for {' '.join(args)}") from exc

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        endpoint = f"repos/{self.repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
        code, stdout, stderr = self._run(["api", endpoint])
        if code != 0:
            text = (stderr or stdout).decode("utf-8", "replace")
            if "HTTP 404" in text or "Not Found" in text:
                return None
            raise self._failure(["api", endpoint], code, stdout, stderr)
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublishError(f"gh returned invalid JSON for {endpoint}") from exc
        if not isinstance(payload, dict):
            raise PublishError(f"gh returned an unexpected payload for {endpoint}")
        return payload

    def list_releases(self) -> list[dict[str, Any]]:
        endpoint = f"repos/{self.repo}/releases?per_page=100"
        payload = self._json(["api", "--paginate", "--slurp", endpoint])
        if not isinstance(payload, list):
            raise PublishError("gh returned an unexpected release list")
        releases: list[dict[str, Any]] = []
        for page in payload:
            if isinstance(page, list):
                releases.extend(item for item in page if isinstance(item, dict))
        return releases

    def create_draft(
        self, tag: str, target_commit: str, title: str, notes_path: Path
    ) -> None:
        args = [
            "release",
            "create",
            tag,
            "--repo",
            self.repo,
            "--target",
            target_commit,
            "--title",
            title,
            "--notes-file",
            str(notes_path),
            "--draft",
            "--latest=false",
        ]
        code, stdout, stderr = self._run(args)
        if code != 0:
            raise self._failure(args, code, stdout, stderr)

    def upload_assets(self, tag: str, paths: Sequence[Path]) -> None:
        args = [
            "release",
            "upload",
            tag,
            "--repo",
            self.repo,
            *[str(path) for path in paths],
        ]
        code, stdout, stderr = self._run(args)
        if code != 0:
            raise self._failure(args, code, stdout, stderr)

    def publish_release(self, tag: str) -> None:
        args = ["release", "edit", tag, "--repo", self.repo, "--draft=false"]
        code, stdout, stderr = self._run(args)
        if code != 0:
            raise self._failure(args, code, stdout, stderr)

    def download_asset(self, tag: str, name: str, max_bytes: int) -> bytes:
        with tempfile.TemporaryDirectory(prefix="neon-relay-readback-") as directory:
            args = [
                "release",
                "download",
                tag,
                "--repo",
                self.repo,
                "--pattern",
                name,
                "--dir",
                directory,
            ]
            code, stdout, stderr = self._run(args)
            if code != 0:
                raise self._failure(args, code, stdout, stderr)
            target = Path(directory) / name
            if not target.is_file():
                raise PublishError(f"gh did not download the expected asset {name}")
            if target.stat().st_size > max_bytes:
                raise PublishError(f"downloaded asset {name} is unexpectedly large")
            return target.read_bytes()


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    tag: str
    release_url: str

class Relay:
    """Orchestrates upstream validation, staging and publication."""

    def __init__(
        self,
        http: HttpClient,
        *,
        publisher: Any | None = None,
        relay_commit: str | None = None,
        api_token: str | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.http = http
        self.publisher = publisher
        self.relay_commit = relay_commit
        self.api_token = api_token
        self._log = log or (lambda message: print(message, flush=True))

    def log(self, message: str) -> None:
        self._log(message)

    def _fetch_release_by_tag(self, tag: str) -> ReleaseInfo:
        payload = self.http.get_json(
            f"{UPSTREAM_API}/repos/{UPSTREAM_REPO}/releases/tags/"
            f"{urllib.parse.quote(tag, safe='')}",
            token=self.api_token,
        )
        return parse_release(payload)

    def _latest_release(self) -> ReleaseInfo:
        payload = self.http.get_json(
            f"{UPSTREAM_API}/repos/{UPSTREAM_REPO}/releases/latest",
            token=self.api_token,
        )
        return parse_release(payload)

    def prepare(self, tag: str | None, output_dir: Path | None) -> StagedRelease:
        if tag is None:
            info = self._latest_release()
            version = info.version
        else:
            version = normalize_version_tag(tag)
            info = self._fetch_release_by_tag(tag)

        if info.draft:
            raise UpstreamError(f"upstream release {info.tag} is a draft")
        if info.prerelease:
            raise UpstreamError(f"upstream release {info.tag} is a pre-release")
        if info.version != version:
            raise UpstreamError(
                f"upstream release tag {info.tag!r} does not match requested version {version}"
            )

        self.log(f"Upstream {UPSTREAM_REPO} release {info.tag} (id {info.release_id})")
        source_commit = resolve_source_commit(self.http, info.tag)
        self.log(f"Source commit {source_commit}")

        selected = select_assets(info)
        out = prepare_output_dir(output_dir)
        staged_architectures: list[StagedArch] = []
        with tempfile.TemporaryDirectory(prefix="neon-relay-download-") as download_dir:
            for spec in ARCHITECTURES:
                asset = selected[spec.abi]
                archive_path = Path(download_dir) / f"{spec.abi}.tar.gz"
                self.log(f"Downloading {asset.name} ({asset.size} bytes)")
                archive_size, archive_sha = self.http.download(
                    asset.download_url,
                    archive_path,
                    max_bytes=MAX_ARCHIVE_BYTES,
                    expected_size=asset.size,
                    expected_sha256=asset.sha256,
                    what=asset.name,
                )
                apk_path = out / spec.output_asset
                member_name, apk_size, apk_sha = extract_single_apk(archive_path, apk_path)
                apk_info = validate_apk(apk_path, spec.abi)
                archive_path.unlink(missing_ok=True)

                staged_architectures.append(
                    StagedArch(
                        abi=spec.abi,
                        upstream_asset=asset,
                        output_asset=spec.output_asset,
                        archive_size=archive_size,
                        archive_sha256=archive_sha,
                        apk_size=apk_size,
                        apk_sha256=apk_sha,
                        member_name=member_name,
                        apk_info=apk_info,
                    )
                )
                self.log(
                    f"Validated {spec.abi}: {spec.output_asset} "
                    f"({apk_size} bytes, sha256 {apk_sha})"
                )
        license_url = f"{UPSTREAM_RAW}/{UPSTREAM_REPO}/{source_commit}/LICENSE"
        license_bytes = self.http.get_bytes(
            license_url,
            max_bytes=MAX_LICENSE_BYTES,
            what="LICENSE",
        )
        if not license_bytes.strip():
            raise UpstreamError("upstream LICENSE is empty")
        if license_bytes.lstrip()[:1] == b"<":
            raise UpstreamError(
                "upstream LICENSE looks like an HTML page, not a license text"
            )
        license_sha = hashlib.sha256(license_bytes).hexdigest()
        write_bytes(out / "plezy-LICENSE.txt", license_bytes)

        sums_bytes = build_sha256sums(staged_architectures)
        write_bytes(out / "SHA256SUMS", sums_bytes)

        provenance = build_provenance(
            version=version,
            info=info,
            source_commit=source_commit,
            architectures=staged_architectures,
            license_sha256=license_sha,
            license_size=len(license_bytes),
            relay_commit=self.relay_commit,
        )
        provenance_bytes = (
            json.dumps(provenance, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        ).encode("utf-8")
        write_bytes(out / "provenance.json", provenance_bytes)
        files: dict[str, Path] = {
            arch.output_asset: out / arch.output_asset for arch in staged_architectures
        }
        files["SHA256SUMS"] = out / "SHA256SUMS"
        files["provenance.json"] = out / "provenance.json"
        files["plezy-LICENSE.txt"] = out / "plezy-LICENSE.txt"
        file_sizes = {name: path.stat().st_size for name, path in files.items()}
        file_hashes = {name: sha256_file(path) for name, path in files.items()}
        for arch in staged_architectures:
            file_hashes[arch.output_asset] = arch.apk_sha256
            file_sizes[arch.output_asset] = arch.apk_size

        staged = StagedRelease(
            version=version,
            tag=destination_tag(version),
            title=release_title(version),
            output_dir=out,
            upstream_tag=info.tag,
            upstream_release_id=info.release_id,
            source_commit=source_commit,
            architectures=tuple(staged_architectures),
            license_sha256=license_sha,
            license_size=len(license_bytes),
            files=files,
            file_sizes=file_sizes,
            file_sha256=file_hashes,
            sha256sums_bytes=sums_bytes,
            provenance=provenance,
            provenance_bytes=provenance_bytes,
            notes=build_notes(version, info, source_commit, staged_architectures),
        )
        self.log(f"Prepared {staged.tag} in {out}")
        return staged

    def _recheck_upstream(self, staged: StagedRelease) -> None:
        info = self._fetch_release_by_tag(staged.upstream_tag)
        if info.release_id != staged.upstream_release_id:
            raise UpstreamError(
                f"upstream release id changed during the run "
                f"({staged.upstream_release_id} -> {info.release_id})"
            )
        if info.draft or info.prerelease:
            raise UpstreamError(
                "upstream release became a draft or pre-release during the run"
            )
        selected = select_assets(info)
        for spec in ARCHITECTURES:
            current = selected[spec.abi]
            expected = next(
                arch for arch in staged.architectures if arch.abi == spec.abi
            )
            if (
                current.asset_id != expected.upstream_asset.asset_id
                or current.size != expected.archive_size
                or current.sha256 != expected.archive_sha256
            ):
                raise UpstreamError(
                    f"upstream asset {current.name} changed during the run; "
                    "refusing to publish"
                )
        commit = resolve_source_commit(self.http, staged.upstream_tag)
        if commit != staged.source_commit:
            raise UpstreamError(
                f"upstream source commit changed during the run "
                f"({staged.source_commit} -> {commit})"
            )

    def _lookup_destination(self, tag: str) -> dict[str, Any] | None:
        release = self.publisher.get_release_by_tag(tag)
        if release is not None:
            return release
        drafts = [
            item
            for item in self.publisher.list_releases()
            if item.get("draft") is True and item.get("tag_name") == tag
        ]
        if len(drafts) > 1:
            raise ConflictError(
                f"multiple drafts exist for {tag}; operator review required"
            )
        return drafts[0] if drafts else None

    def _check_release(
        self,
        release: Mapping[str, Any],
        staged: StagedRelease,
        *,
        allow_missing: bool,
    ) -> dict[str, Any]:
        tag = staged.tag
        assets: dict[str, Mapping[str, Any]] = {}
        for item in release.get("assets") or []:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
                raise PublishError(f"gh returned malformed asset metadata for {tag}")
            name = item["name"]
            if name in assets:
                raise ConflictError(f"release {tag} lists the asset {name} twice")
            assets[name] = item

        unexpected = sorted(set(assets) - set(REQUIRED_ASSETS))
        if unexpected:
            raise ConflictError(
                f"release {tag} has unexpected assets: {', '.join(unexpected)}; "
                "operator review required"
            )
        missing = [name for name in REQUIRED_ASSETS if name not in assets]
        if missing and not allow_missing:
            raise ConflictError(
                f"release {tag} is incomplete: missing {', '.join(missing)}; "
                "operator review required"
            )
        for name in REQUIRED_ASSETS:
            if name not in assets:
                continue
            asset = assets[name]
            if name not in ("provenance.json", "SHA256SUMS"):
                size = asset.get("size")
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size != staged.file_sizes[name]
                ):
                    raise ConflictError(
                        f"release {tag} asset {name} has an unexpected size; "
                        "operator review required"
                    )
            if name == "provenance.json":
                data = self.publisher.download_asset(tag, name, MAX_TEXT_ASSET_BYTES)
                if not provenance_equivalent(data, staged.provenance):
                    raise ConflictError(
                        f"release {tag} provenance does not match this candidate; "
                        "operator review required"
                    )
                continue
            if name == "SHA256SUMS":
                data = self.publisher.download_asset(tag, name, MAX_TEXT_ASSET_BYTES)
                if data != staged.sha256sums_bytes:
                    raise ConflictError(
                        f"release {tag} SHA256SUMS does not match this candidate; "
                        "operator review required"
                    )
                continue
            digest = asset.get("digest")
            expected = f"sha256:{staged.file_sha256[name]}"
            if isinstance(digest, str) and digest:
                if digest.lower() != expected:
                    raise ConflictError(
                        f"release {tag} asset {name} has a different SHA-256; "
                        "refusing to overwrite it"
                    )
            else:
                data = self.publisher.download_asset(tag, name, MAX_APK_BYTES)
                if hashlib.sha256(data).hexdigest() != staged.file_sha256[name]:
                    raise ConflictError(
                        f"release {tag} asset {name} has a different SHA-256; "
                        "refusing to overwrite it"
                    )
        return assets

    @staticmethod
    def _check_draft_target(release: Mapping[str, Any], target_commit: str) -> None:
        target = release.get("target_commitish")
        if not isinstance(target, str) or target.lower() != target_commit.lower():
            raise ConflictError(
                "existing draft points at a different target commit; "
                "operator review required"
            )

    def _readback(self, staged: StagedRelease) -> dict[str, Any]:
        release = self.publisher.get_release_by_tag(staged.tag)
        if release is None:
            raise PublishError(f"release {staged.tag} disappeared during publication")
        self._check_release(release, staged, allow_missing=False)
        return release

    def publish(
        self, staged: StagedRelease, *, repo: str, target_commit: str
    ) -> PublishResult:
        if self.publisher is None:
            raise PublishError("no publisher is configured")
        destination = normalize_repo(repo)
        if destination.lower() != EXPECTED_DESTINATION_REPO.lower():
            raise InputError(
                f"refusing to publish to {destination!r}; "
                f"expected {EXPECTED_DESTINATION_REPO}"
            )
        target_commit = validate_target_commit(target_commit)

        existing = self._lookup_destination(staged.tag)
        if existing is not None and existing.get("draft") is not True:
            self._check_release(existing, staged, allow_missing=False)
            self.log(f"No-op: {staged.tag} is already published with matching assets")
            return PublishResult(
                outcome="noop",
                tag=staged.tag,
                release_url=str(existing.get("html_url") or ""),
            )
        notes_path: Path | None = None
        try:
            if existing is None:
                self._recheck_upstream(staged)
                handle = tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", suffix=".md", delete=False
                )
                try:
                    handle.write(staged.notes)
                finally:
                    handle.close()
                notes_path = Path(handle.name)
                self.log(f"Creating draft release {staged.tag}")
                try:
                    self.publisher.create_draft(
                        staged.tag, target_commit, staged.title, notes_path
                    )
                    created = self.publisher.get_release_by_tag(staged.tag)
                    if created is None:
                        raise PublishError(
                            f"draft release {staged.tag} is not readable after creation"
                        )
                    self._check_draft_target(created, target_commit)
                except PublishError:
                    raced = self._lookup_destination(staged.tag)
                    if raced is None:
                        raise
                    existing = raced
                    if existing.get("draft") is not True:
                        raise ConflictError(
                            f"release {staged.tag} was published concurrently; "
                            "operator review required"
                        )

            if existing is not None:
                self._check_draft_target(existing, target_commit)
                self._check_release(existing, staged, allow_missing=True)
                present = {
                    item.get("name") for item in existing.get("assets") or []
                }
                missing_paths = [
                    staged.files[name]
                    for name in REQUIRED_ASSETS
                    if name not in present
                ]
            else:
                missing_paths = [staged.files[name] for name in REQUIRED_ASSETS]
            if missing_paths:
                self.log(
                    f"Uploading {len(missing_paths)} asset(s) to draft {staged.tag}"
                )
                self.publisher.upload_assets(staged.tag, missing_paths)

            self._readback(staged)
            self._recheck_upstream(staged)

            self.log(f"Publishing {staged.tag}")
            self.publisher.publish_release(staged.tag)
            release = self._readback(staged)
            if release.get("draft") is not False:
                raise PublishError(
                    f"release {staged.tag} is still a draft after publishing"
                )
            self.log(f"Published {staged.tag}")
            return PublishResult(
                outcome="published",
                tag=staged.tag,
                release_url=str(release.get("html_url") or ""),
            )
        finally:
            if notes_path is not None:
                notes_path.unlink(missing_ok=True)


def write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def render_summary(staged: StagedRelease, result: PublishResult | None) -> str:
    lines = [
        f"### Neon Relay \u00b7 Plezy {staged.version}",
        "",
        f"- Upstream: [{staged.upstream_tag}]"
        f"({UPSTREAM_SITE}/{UPSTREAM_REPO}/releases/tag/"
        f"{urllib.parse.quote(staged.upstream_tag, safe='')})"
        f" (release {staged.upstream_release_id}, commit `{staged.source_commit}`)",
        f"- Destination tag: `{staged.tag}`",
    ]
    for arch in staged.architectures:
        lines.append(
            f"- `{arch.output_asset}` ({arch.abi}): {arch.apk_size} bytes, "
            f"sha256 `{arch.apk_sha256}`"
        )
    if result is None:
        lines.append("- Result: prepared locally (not published)")
    elif result.outcome == "noop":
        lines.append(f"- Result: no-op, {staged.tag} was already complete")
    else:
        lines.append(f"- Result: published {staged.tag}")
        if result.release_url:
            lines.append(f"- Release: {result.release_url}")
    lines.append("")
    return "\n".join(lines)


def write_summary(path: Path | None, text: str) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay.py",
        description=(
            "Prepare (and optionally publish) Plezy Android APKs for NeonQuark."
        ),
    )
    parser.add_argument(
        "--tag",
        default=None,
        help=(
            "Upstream stable version tag, for example 2.25.0. "
            "Defaults to the latest stable release."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Directory that receives the prepared files. Required without --publish.",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish a NeonQuark release after preparing and verifying the files.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=f"Destination repository for --publish (must be {EXPECTED_DESTINATION_REPO}).",
    )
    parser.add_argument(
        "--target-commit",
        default=None,
        help="Full commit SHA that the destination release tag must point to.",
    )
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=None,
        help="Append a Markdown job summary to this file.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Neon Relay {RELAY_VERSION}",
    )
    return parser

def run(
    args: argparse.Namespace,
    *,
    http: HttpClient | None = None,
    publisher: Any | None = None,
) -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    if args.publish:
        if not args.repo:
            raise InputError("--publish requires --repo")
        if not args.target_commit:
            raise InputError("--publish requires --target-commit")
    else:
        if args.repo or args.target_commit:
            raise InputError("--repo and --target-commit are only valid with --publish")
        if args.output is None:
            raise InputError("--output is required without --publish")

    client = http or HttpClient()
    if publisher is None and args.publish:
        publisher = GhPublisher(normalize_repo(args.repo), token=token)

    relay = Relay(
        client,
        publisher=publisher,
        relay_commit=default_relay_commit(),
        api_token=token,
    )
    relay.log(f"Neon Relay {RELAY_VERSION}")

    summary_path = args.summary_file
    if summary_path is None:
        env_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        summary_path = Path(env_summary) if env_summary else None

    managed_dir: Path | None = None
    try:
        output_dir = args.output
        if output_dir is None:
            output_dir = Path(tempfile.mkdtemp(prefix="neon-relay-stage-"))
            managed_dir = output_dir

        staged = relay.prepare(args.tag, output_dir)
        result: PublishResult | None = None
        if args.publish:
            result = relay.publish(
                staged,
                repo=args.repo,
                target_commit=args.target_commit,
            )
        write_summary(summary_path, render_summary(staged, result))
        return EXIT_OK
    finally:
        if managed_dir is not None:
            import shutil

            shutil.rmtree(managed_dir, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except RelayError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("error: interrupted", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())