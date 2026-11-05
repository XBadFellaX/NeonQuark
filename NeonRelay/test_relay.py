"""Offline unit tests for Neon Relay.

Every test uses generated fixtures. No test touches the network or a real
GitHub repository: the HTTP opener and the ``gh`` runner are both fakes.
"""

from __future__ import annotations

import email.message
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

import relay

VERSION = "2.25.0"
SOURCE_COMMIT = "c30493435ca98e7f521b89ecd8d096484d237c57"
TARGET_COMMIT = "a" * 40
LICENSE_TEXT = b"GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"

ARCHIVE_NAMES = {
    "arm64-v8a": "plezy-android-arm64-v8a.tar.gz",
    "armeabi-v7a": "plezy-android-armeabi-v7a.tar.gz",
}


def flip_last_byte(data: bytes) -> bytes:
    if not data:
        return b"\x00"
    return data[:-1] + bytes([data[-1] ^ 0xFF])


def make_apk(abi="arm64-v8a", *, manifest=True, libs=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if manifest:
            archive.writestr("AndroidManifest.xml", b"<manifest />")
        if libs is None:
            libs = [f"lib/{abi}/libplezy.so"]
        for name in libs:
            archive.writestr(name, b"\x7fELF" + name.encode("utf-8"))
    return buffer.getvalue()

def make_duplicate_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"<manifest />")
        archive.writestr("lib/arm64-v8a/liba.so", b"aaa")
        archive.writestr("lib/arm64-v8a/liba.so", b"bbb")
    return buffer.getvalue()


def make_tar(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def tar_entry(name, data=b"", *, kind=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, (data if kind == tarfile.REGTYPE else None)


def make_archive(apk_bytes, *, member="plezy.apk", extra=()):
    entries = [tar_entry(member, apk_bytes)]
    entries.extend(extra)
    return make_tar(entries)


def archive_url(version, name):
    return f"https://github.com/{relay.UPSTREAM_REPO}/releases/download/{version}/{name}"


def asset_payload(asset_id, version, name, data, *, state="uploaded", digest=None):
    return {
        "id": asset_id,
        "name": name,
        "size": len(data),
        "state": state,
        "digest": (
            digest
            if digest is not None
            else "sha256:" + hashlib.sha256(data).hexdigest()
        ),
        "browser_download_url": archive_url(version, name),
    }


def release_payload(
    archives,
    *,
    version=VERSION,
    release_id=4242,
    tag=None,
    draft=False,
    prerelease=False,
    extra_assets=(),
):
    tag = tag or version
    assets = [
        asset_payload(100 + index, version, name, data)
        for index, (name, data) in enumerate(archives.items())
    ]
    assets.extend(extra_assets)
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": assets,
    }

class FakeResponse:
    def __init__(self, body, *, headers=None, status=200):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = (
            headers if headers is not None else {"Content-Length": str(len(body))}
        )

    def read(self, amount=-1):
        if amount is None or amount < 0:
            chunk = self._body[self._offset:]
            self._offset = len(self._body)
            return chunk
        chunk = self._body[self._offset:self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self):
        pass

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class FakeOpener:
    """Route fake responses by exact URL, asserting unknown URLs stay unrequested."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, url, handler):
        self.routes[url] = handler
        return self

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append((url, dict(request.headers)))
        handler = self.routes.get(url)
        if handler is None:
            raise AssertionError(f"unexpected URL: {url}")
        if isinstance(handler, list):
            handler = handler.pop(0) if len(handler) > 1 else handler[0]
        if isinstance(handler, BaseException):
            raise handler
        if isinstance(handler, FakeResponse):
            return handler
        if callable(handler):
            return handler(request)
        return FakeResponse(handler)


def http_error(url, code, headers=None):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, f"HTTP {code}", message, io.BytesIO(b""))

class TestTagValidation(unittest.TestCase):
    def test_accepts_stable_versions(self):
        self.assertEqual(relay.normalize_version_tag("2.25.0"), "2.25.0")
        self.assertEqual(relay.normalize_version_tag("v2.25.0"), "2.25.0")
        self.assertEqual(relay.normalize_version_tag("0.0.0"), "0.0.0")
        self.assertEqual(relay.normalize_version_tag("10.20.30"), "10.20.30")

    def test_rejects_unsafe_or_non_stable_tags(self):
        bad = [
            "",
            "2.25",
            "2.25.0.1",
            "2.25.0-rc.1",
            "v2.25.0-beta",
            "latest",
            "02.25.0",
            " 2.25.0",
            "2.25.0 ",
            "2.25.0\n",
            "2/25/0",
            "2.25.0; rm -rf /",
            "v",
            "2.25.0\t",
        ]
        for tag in bad:
            with self.subTest(tag=tag):
                with self.assertRaises(relay.InputError):
                    relay.normalize_version_tag(tag)
        with self.assertRaises(relay.InputError):
            relay.normalize_version_tag(None)

    def test_destination_names(self):
        self.assertEqual(relay.destination_tag("2.25.0"), "plezy-v2.25.0")
        self.assertEqual(relay.release_title("2.25.0"), "Neon Relay \u00b7 Plezy 2.25.0")

    def test_repository_and_commit_validation(self):
        self.assertEqual(relay.normalize_repo(" XBadFellaX/NeonQuark "), "XBadFellaX/NeonQuark")
        self.assertEqual(relay.validate_target_commit("A" * 40), "a" * 40)
        for repo in ["", "owner", "a/b/c", "own er/repo"]:
            with self.subTest(repo=repo):
                with self.assertRaises(relay.InputError):
                    relay.normalize_repo(repo)
        for commit in ["", "abc", "z" * 40, "a" * 41]:
            with self.subTest(commit=commit):
                with self.assertRaises(relay.InputError):
                    relay.validate_target_commit(commit)

class TestParseRelease(unittest.TestCase):
    def setUp(self):
        self.archives = {
            ARCHIVE_NAMES["arm64-v8a"]: make_archive(make_apk("arm64-v8a")),
            ARCHIVE_NAMES["armeabi-v7a"]: make_archive(make_apk("armeabi-v7a")),
        }

    def test_valid_release(self):
        info = relay.parse_release(release_payload(self.archives))
        self.assertEqual(info.release_id, 4242)
        self.assertEqual(info.tag, VERSION)
        self.assertEqual(info.version, VERSION)
        self.assertFalse(info.draft)
        self.assertEqual(len(info.assets), 2)
        self.assertEqual(info.assets[0].sha256, hashlib.sha256(
            self.archives[ARCHIVE_NAMES["arm64-v8a"]]
        ).hexdigest())

    def test_rejects_malformed_metadata(self):
        base = release_payload(self.archives)
        cases = [
            ("not an object", None),
            ("missing id", {k: v for k, v in base.items() if k != "id"}),
            ("missing tag", {k: v for k, v in base.items() if k != "tag_name"}),
            ("non-stable tag", dict(base, tag_name="nightly")),
            ("non-bool draft", dict(base, draft="false")),
            ("missing assets", {k: v for k, v in base.items() if k != "assets"}),
            ("bad asset size", dict(base, assets=[dict(base["assets"][0], size=-1)])),
            ("bad asset id", dict(base, assets=[dict(base["assets"][0], id=0)])),
            ("bad asset digest", dict(base, assets=[dict(base["assets"][0], digest=7)])),
        ]
        for label, payload in cases:
            with self.subTest(label=label):
                with self.assertRaises(relay.UpstreamError):
                    relay.parse_release(payload)
        for payload in [None, [], "x"]:
            with self.subTest(payload=payload):
                with self.assertRaises(relay.UpstreamError):
                    relay.parse_release(payload)

class TestSelectAssets(unittest.TestCase):
    def setUp(self):
        self.archives = {
            ARCHIVE_NAMES["arm64-v8a"]: make_archive(make_apk("arm64-v8a")),
            ARCHIVE_NAMES["armeabi-v7a"]: make_archive(make_apk("armeabi-v7a")),
        }

    def test_selects_both_architectures(self):
        selected = relay.select_assets(
            relay.parse_release(release_payload(self.archives))
        )
        self.assertEqual(sorted(selected), ["arm64-v8a", "armeabi-v7a"])

    def test_missing_either_architecture_fails(self):
        for abi in ("arm64-v8a", "armeabi-v7a"):
            with self.subTest(abi=abi):
                archives = dict(self.archives)
                del archives[ARCHIVE_NAMES[abi]]
                info = relay.parse_release(release_payload(archives))
                with self.assertRaises(relay.AssetNotReady):
                    relay.select_assets(info)

    def test_duplicate_asset_fails(self):
        payload = release_payload(self.archives)
        payload["assets"].append(dict(payload["assets"][0], id=999))
        with self.assertRaises(relay.AssetNotReady):
            relay.select_assets(relay.parse_release(payload))
    def test_unfinished_or_empty_assets_fail(self):
        for override in ({"state": "new"}, {"size": 0}):
            with self.subTest(override=override):
                payload = release_payload(self.archives)
                payload["assets"][0].update(override)
                with self.assertRaises(relay.AssetNotReady):
                    relay.select_assets(relay.parse_release(payload))

    def test_missing_digest_fails(self):
        payload = release_payload(self.archives)
        payload["assets"][0]["digest"] = None
        with self.assertRaises(relay.AssetNotReady):
            relay.select_assets(relay.parse_release(payload))

    def test_bad_digest_or_url_fails(self):
        cases = [
            {"digest": "md5:abc"},
            {"browser_download_url": "https://evil.example/file.tar.gz"},
            {"browser_download_url": "https://github.com/other/repo/releases/download/2/x"},
        ]
        for override in cases:
            with self.subTest(override=override):
                payload = release_payload(self.archives)
                payload["assets"][0].update(override)
                with self.assertRaises(relay.ValidationError):
                    relay.select_assets(relay.parse_release(payload))

    def test_oversized_archive_fails(self):
        payload = release_payload(self.archives)
        payload["assets"][0]["size"] = relay.MAX_ARCHIVE_BYTES + 1
        with self.assertRaises(relay.ValidationError):
            relay.select_assets(relay.parse_release(payload))

class TestHttpClient(unittest.TestCase):
    def make_client(self, opener, attempts=3):
        self.sleeps = []
        return relay.HttpClient(
            opener=opener,
            attempts=attempts,
            sleeper=self.sleeps.append,
            backoff=1.0,
        )

    def test_get_json_returns_payload(self):
        opener = FakeOpener().add("https://api.github.com/x", b'{"ok": true}')
        client = self.make_client(opener)
        self.assertEqual(client.get_json("https://api.github.com/x"), {"ok": True})
        self.assertEqual(len(opener.calls), 1)

    def test_retries_transient_server_errors(self):
        opener = FakeOpener().add(
            "https://api.github.com/x",
            [
                http_error("https://api.github.com/x", 500),
                http_error("https://api.github.com/x", 503),
                b'{"ok": true}',
            ],
        )
        client = self.make_client(opener)
        self.assertEqual(client.get_json("https://api.github.com/x"), {"ok": True})
        self.assertEqual(len(self.sleeps), 2)

    def test_client_errors_are_not_retried(self):
        opener = FakeOpener().add(
            "https://api.github.com/x", http_error("https://api.github.com/x", 404)
        )
        client = self.make_client(opener)
        with self.assertRaises(relay.UpstreamError):
            client.get_json("https://api.github.com/x")
        self.assertEqual(len(opener.calls), 1)

    def test_rate_limit_is_reported_without_retry(self):
        opener = FakeOpener().add(
            "https://api.github.com/x",
            http_error(
                "https://api.github.com/x",
                403,
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"},
            ),
        )
        client = self.make_client(opener)
        with self.assertRaises(relay.UpstreamError) as caught:
            client.get_json("https://api.github.com/x")
        self.assertIn("rate limit", str(caught.exception))
        self.assertEqual(len(opener.calls), 1)
    def test_timeouts_are_bounded(self):
        opener = FakeOpener().add(
            "https://api.github.com/x",
            [urllib.error.URLError("timed out")],
        )
        client = self.make_client(opener, attempts=2)
        with self.assertRaises(relay.UpstreamError):
            client.get_json("https://api.github.com/x")
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_invalid_json_is_rejected(self):
        opener = FakeOpener().add("https://api.github.com/x", b"not json")
        client = self.make_client(opener)
        with self.assertRaises(relay.UpstreamError):
            client.get_json("https://api.github.com/x")
    def test_download_verifies_size_and_digest(self):
        body = b"payload" * 1000
        digest = hashlib.sha256(body).hexdigest()
        opener = FakeOpener().add("https://github.com/a.tar.gz", body)
        client = self.make_client(opener)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "a.tar.gz"
            size, actual = client.download(
                "https://github.com/a.tar.gz",
                destination,
                max_bytes=relay.MAX_ARCHIVE_BYTES,
                expected_size=len(body),
                expected_sha256=digest,
            )
            self.assertEqual((size, actual), (len(body), digest))
            self.assertEqual(destination.read_bytes(), body)

    def test_download_rejects_truncation_and_bad_digest(self):
        body = b"payload" * 100
        opener = FakeOpener().add("https://github.com/a.tar.gz", body)
        client = self.make_client(opener)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "a.tar.gz"
            with self.assertRaises(relay.ValidationError):
                client.download(
                    "https://github.com/a.tar.gz",
                    destination,
                    max_bytes=relay.MAX_ARCHIVE_BYTES,
                    expected_size=len(body) + 1,
                    expected_sha256=hashlib.sha256(body).hexdigest(),
                )
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name("a.tar.gz.part").exists())
            with self.assertRaises(relay.ValidationError):
                client.download(
                    "https://github.com/a.tar.gz",
                    destination,
                    max_bytes=relay.MAX_ARCHIVE_BYTES,
                    expected_size=len(body),
                    expected_sha256="0" * 64,
                )
    def test_download_enforces_byte_limits(self):
        body = b"x" * 64
        opener = FakeOpener().add(
            "https://github.com/big", FakeResponse(body, headers={})
        )
        client = self.make_client(opener)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "big"
            with self.assertRaises(relay.ValidationError):
                client.download(
                    "https://github.com/big", destination, max_bytes=16
                )
            self.assertFalse(destination.exists())

    def test_declared_size_above_limit_fails_before_download(self):
        opener = FakeOpener().add(
            "https://github.com/big",
            FakeResponse(b"", headers={"Content-Length": "999999"}),
        )
        client = self.make_client(opener)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(relay.ValidationError):
                client.download(
                    "https://github.com/big",
                    Path(directory) / "big",
                    max_bytes=16,
                )

    def test_redirect_strips_authorization_on_host_change(self):
        handler = relay._SafeRedirectHandler()
        request = relay.urllib.request.Request(
            "https://api.github.com/x",
            headers={"Authorization": "Bearer secret"},
        )
        moved = handler.redirect_request(
            request, None, 302, "Found", {}, "https://objects.example.com/y"
        )
        self.assertNotIn("Authorization", moved.headers)
        same = handler.redirect_request(
            request, None, 302, "Found", {}, "https://api.github.com/y"
        )
        self.assertEqual(same.headers.get("Authorization"), "Bearer secret")

class TestTarSafety(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def extract(self, archive_bytes, name="archive.tar.gz"):
        archive = self.root / name
        archive.write_bytes(archive_bytes)
        destination = self.root / "output.apk"
        return relay.extract_single_apk(archive, destination), destination

    def assert_rejected(self, archive_bytes, message=None):
        with self.assertRaises(relay.ValidationError) as caught:
            self.extract(archive_bytes)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_copies_the_only_apk_member(self):
        apk = make_apk()
        archive = make_tar(
            [
                tar_entry("assets/", kind=tarfile.DIRTYPE),
                tar_entry("README.txt", b"notes"),
                tar_entry("plezy.apk", apk),
            ]
        )
        (member, size, digest), destination = self.extract(archive)
        self.assertEqual(member, "plezy.apk")
        self.assertEqual(size, len(apk))
        self.assertEqual(digest, hashlib.sha256(apk).hexdigest())
        self.assertEqual(destination.read_bytes(), apk)

    def test_rejects_traversal_and_absolute_paths(self):
        apk = make_apk()
        cases = [
            "../evil.apk",
            "nested/../../evil.apk",
            "/etc/evil.apk",
            "..\\evil.apk",
            "C:/Windows/evil.apk",
        ]
        for path in cases:
            with self.subTest(path=path):
                self.assert_rejected(make_tar([tar_entry(path, apk)]))

    def test_rejects_links_and_devices(self):
        apk = make_apk()
        cases = [
            tar_entry("link.apk", kind=tarfile.SYMTYPE, linkname="plezy.apk"),
            tar_entry("hard.apk", kind=tarfile.LNKTYPE, linkname="plezy.apk"),
            tar_entry("char.apk", kind=tarfile.CHRTYPE),
            tar_entry("block.apk", kind=tarfile.BLKTYPE),
            tar_entry("fifo.apk", kind=tarfile.FIFOTYPE),
        ]
        for extra in cases:
            with self.subTest(kind=extra[0].type):
                self.assert_rejected(make_archive(apk, extra=[extra]))
    def test_rejects_wrong_number_of_apk_members(self):
        first = make_apk()
        second = make_apk("armeabi-v7a")
        self.assert_rejected(
            make_tar([tar_entry("a.apk", first), tar_entry("b.apk", second)]),
            "multiple",
        )
        self.assert_rejected(make_tar([tar_entry("README.txt", b"x")]), "no .apk")

    def test_rejects_corrupt_or_truncated_archives(self):
        self.assert_rejected(b"not a tarball")
        archive = make_archive(make_apk())
        self.assert_rejected(archive[: len(archive) // 2])

    def test_enforces_member_count_limit(self):
        entries = [tar_entry(f"dir{index}/", kind=tarfile.DIRTYPE) for index in range(4)]
        entries.append(tar_entry("plezy.apk", make_apk()))
        with mock.patch.object(relay, "MAX_TAR_MEMBERS", 3):
            self.assert_rejected(make_tar(entries), "limit")

    def test_enforces_apk_size_limit(self):
        apk = make_apk()
        with mock.patch.object(relay, "MAX_APK_BYTES", len(apk) - 1):
            self.assert_rejected(make_archive(apk), "above the")

    def test_rejects_empty_apk(self):
        self.assert_rejected(make_tar([tar_entry("plezy.apk", b"")]), "empty")

class TestApkValidation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def validate(self, data, abi="arm64-v8a"):
        path = self.root / "app.apk"
        path.write_bytes(data)
        return relay.validate_apk(path, abi)

    def assert_rejected(self, data, abi="arm64-v8a"):
        with self.assertRaises(relay.ValidationError):
            self.validate(data, abi)

    def test_accepts_matching_abi(self):
        for abi in ("arm64-v8a", "armeabi-v7a"):
            with self.subTest(abi=abi):
                info = self.validate(make_apk(abi), abi)
                self.assertEqual(info.native_abis, (abi,))
                self.assertGreaterEqual(info.entry_count, 2)

    def test_rejects_missing_manifest(self):
        self.assert_rejected(make_apk("arm64-v8a", manifest=False))

    def test_rejects_wrong_or_foreign_abi(self):
        self.assert_rejected(make_apk("x86_64"), "arm64-v8a")
        mixed = make_apk(
            "arm64-v8a",
            libs=["lib/arm64-v8a/liba.so", "lib/x86_64/libb.so"],
        )
        self.assert_rejected(mixed, "arm64-v8a")

    def test_rejects_duplicate_entries(self):
        self.assert_rejected(make_duplicate_zip())

    def test_rejects_non_zip(self):
        self.assert_rejected(b"definitely not a zip")

    def test_rejects_empty_native_directory(self):
        apk = make_apk("arm64-v8a", libs=["lib/arm64-v8a/"])
        self.assert_rejected(apk)

    def test_enforces_entry_and_size_limits(self):
        apk = make_apk()
        with mock.patch.object(relay, "MAX_ZIP_ENTRIES", 1):
            self.assert_rejected(apk)
        with mock.patch.object(relay, "MAX_ZIP_UNCOMPRESSED_BYTES", 4):
            self.assert_rejected(apk)

def build_env(
    *,
    version=VERSION,
    tag=None,
    draft=False,
    prerelease=False,
    apk64=None,
    apk32=None,
    license_bytes=LICENSE_TEXT,
    source_commit=SOURCE_COMMIT,
    archive_mutator=None,
    apk64_digest=None,
    latest=False,
):
    apk64 = apk64 if apk64 is not None else make_apk("arm64-v8a")
    apk32 = apk32 if apk32 is not None else make_apk("armeabi-v7a")
    archives = {
        ARCHIVE_NAMES["arm64-v8a"]: make_archive(apk64),
        ARCHIVE_NAMES["armeabi-v7a"]: make_archive(apk32),
    }
    payload = release_payload(
        archives,
        version=version,
        tag=tag,
        draft=draft,
        prerelease=prerelease,
    )
    if apk64_digest is not None:
        payload["assets"][0]["digest"] = apk64_digest

    opener = FakeOpener()
    api_tag = tag or version
    endpoints = ["/releases/latest"] if latest else [f"/releases/tags/{api_tag}"]
    for endpoint in endpoints:
        opener.add(
            f"{relay.UPSTREAM_API}/repos/{relay.UPSTREAM_REPO}{endpoint}",
            json.dumps(payload).encode("utf-8"),
        )
    opener.add(
        f"{relay.UPSTREAM_API}/repos/{relay.UPSTREAM_REPO}/git/ref/tags/{api_tag}",
        json.dumps({"object": {"sha": source_commit, "type": "commit"}}).encode("utf-8"),
    )
    for name, data in archives.items():
        body = archive_mutator(name, data) if archive_mutator else data
        opener.add(archive_url(version, name), body)
    license_url = f"{relay.UPSTREAM_RAW}/{relay.UPSTREAM_REPO}/{source_commit}/LICENSE"
    if license_bytes is None:
        opener.add(license_url, http_error(license_url, 404))
    else:
        opener.add(license_url, license_bytes)

    http = relay.HttpClient(opener=opener, sleeper=lambda seconds: None)
    return {
        "opener": opener,
        "http": http,
        "payload": payload,
        "archives": archives,
        "apk64": apk64,
        "apk32": apk32,
    }


def make_relay(http, **kwargs):
    kwargs.setdefault("relay_commit", "b" * 40)
    kwargs.setdefault("log", lambda message: None)
    return relay.Relay(http, **kwargs)

class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def output(self, name="out"):
        return self.root / name

    def test_prepare_produces_verified_bundle(self):
        env = build_env()
        staged = make_relay(env["http"]).prepare(VERSION, self.output())

        self.assertEqual(staged.version, VERSION)
        self.assertEqual(staged.tag, "plezy-v2.25.0")
        self.assertEqual(staged.upstream_release_id, 4242)
        self.assertEqual(staged.source_commit, SOURCE_COMMIT)
        self.assertEqual(
            sorted(path.name for path in staged.output_dir.iterdir()),
            sorted(relay.REQUIRED_ASSETS),
        )

        apk64 = staged.files["plezy-arm64-v8a.apk"].read_bytes()
        apk32 = staged.files["plezy-armeabi-v7a.apk"].read_bytes()
        self.assertEqual(apk64, env["apk64"])
        self.assertEqual(apk32, env["apk32"])
        expected_sums = (
            f"{hashlib.sha256(env['apk64']).hexdigest()}  plezy-arm64-v8a.apk\n"
            f"{hashlib.sha256(env['apk32']).hexdigest()}  plezy-armeabi-v7a.apk\n"
        )
        self.assertEqual(
            staged.files["SHA256SUMS"].read_text(encoding="utf-8"), expected_sums
        )
        self.assertEqual(
            staged.files["plezy-LICENSE.txt"].read_bytes(), LICENSE_TEXT
        )
        self.assertIn("plezy-arm64-v8a.apk", staged.notes)
        self.assertIn("edde746/plezy", staged.notes)
        provenance = json.loads(
            staged.files["provenance.json"].read_text(encoding="utf-8")
        )
        self.assertEqual(provenance["schema"], "neon-relay/provenance/v1")
        self.assertEqual(provenance["upstream"]["source_commit"], SOURCE_COMMIT)
        self.assertEqual(provenance["upstream"]["release_id"], 4242)
        self.assertEqual(provenance["upstream"]["license"]["spdx"], "GPL-3.0")
        self.assertEqual(provenance["destination"]["tag"], "plezy-v2.25.0")
        arch = {item["abi"]: item for item in provenance["architectures"]}
        self.assertEqual(set(arch), {"arm64-v8a", "armeabi-v7a"})
        self.assertEqual(arch["arm64-v8a"]["apk"]["archive_member"], "plezy.apk")
        self.assertEqual(
            arch["arm64-v8a"]["apk"]["sha256"],
            hashlib.sha256(env["apk64"]).hexdigest(),
        )
        self.assertEqual(
            arch["arm64-v8a"]["upstream_asset"]["name"],
            ARCHIVE_NAMES["arm64-v8a"],
        )
        text = staged.provenance_bytes.decode("utf-8")
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("token", text.lower())
        self.assertNotIn("secret", text.lower())

    def test_prepare_uses_latest_endpoint_without_tag(self):
        env = build_env(latest=True)
        staged = make_relay(env["http"]).prepare(None, self.output())
        self.assertEqual(staged.tag, "plezy-v2.25.0")

    def test_prepare_accepts_v_prefixed_tag(self):
        env = build_env(tag="v2.25.0")
        staged = make_relay(env["http"]).prepare("v2.25.0", self.output())
        self.assertEqual(staged.version, "2.25.0")
        self.assertEqual(staged.tag, "plezy-v2.25.0")
    def test_prepare_rejects_drafts_and_prereleases(self):
        for kwargs in ({"draft": True}, {"prerelease": True}):
            with self.subTest(kwargs=kwargs):
                env = build_env(**kwargs)
                with self.assertRaises(relay.UpstreamError):
                    make_relay(env["http"]).prepare(VERSION, self.output("x"))

    def test_prepare_rejects_mismatched_version(self):
        env = build_env()
        payload = release_payload(env["archives"], version=VERSION, tag="2.26.0")
        env["opener"].add(
            f"{relay.UPSTREAM_API}/repos/{relay.UPSTREAM_REPO}/releases/tags/{VERSION}",
            json.dumps(payload).encode("utf-8"),
        )
        with self.assertRaises(relay.UpstreamError):
            make_relay(env["http"]).prepare(VERSION, self.output())

    def test_prepare_refuses_non_empty_output(self):
        target = self.output()
        target.mkdir()
        (target / "existing.txt").write_text("x", encoding="utf-8")
        env = build_env()
        with self.assertRaises(relay.InputError):
            make_relay(env["http"]).prepare(VERSION, target)

    def test_prepare_rejects_truncated_archive(self):
        env = build_env(archive_mutator=lambda name, data: data[:-8])
        with self.assertRaises(relay.ValidationError):
            make_relay(env["http"]).prepare(VERSION, self.output())

    def test_prepare_rejects_digest_mismatch(self):
        env = build_env(apk64_digest="0" * 64)
        with self.assertRaises(relay.ValidationError):
            make_relay(env["http"]).prepare(VERSION, self.output())

    def test_prepare_requires_the_license(self):
        env = build_env(license_bytes=None)
        with self.assertRaises(relay.UpstreamError):
            make_relay(env["http"]).prepare(VERSION, self.output())
    def test_prepare_resolves_annotated_tags(self):
        env = build_env()
        tag_sha = "1" * 40
        ref_url = f"{relay.UPSTREAM_API}/repos/{relay.UPSTREAM_REPO}/git/ref/tags/{VERSION}"
        env["opener"].add(
            ref_url,
            json.dumps({"object": {"sha": tag_sha, "type": "tag"}}).encode("utf-8"),
        )
        env["opener"].add(
            f"{relay.UPSTREAM_API}/repos/{relay.UPSTREAM_REPO}/git/tags/{tag_sha}",
            json.dumps(
                {"object": {"sha": SOURCE_COMMIT, "type": "commit"}}
            ).encode("utf-8"),
        )
        staged = make_relay(env["http"]).prepare(VERSION, self.output())
        self.assertEqual(staged.source_commit, SOURCE_COMMIT)

    def test_prepare_is_deterministic(self):
        env = build_env()
        first = make_relay(env["http"]).prepare(VERSION, self.output("one"))
        second = make_relay(env["http"]).prepare(VERSION, self.output("two"))
        self.assertEqual(first.provenance_bytes, second.provenance_bytes)
        self.assertEqual(first.sha256sums_bytes, second.sha256sums_bytes)


class TestProvenance(unittest.TestCase):
    def test_semantic_comparison_ignores_relay_version(self):
        candidate = {
            "schema": "neon-relay/provenance/v1",
            "relay": {"name": "Neon Relay", "version": "9.9.9", "commit": None},
            "upstream": {"tag": VERSION},
        }
        existing = json.dumps(
            {
                "schema": "neon-relay/provenance/v1",
                "relay": {"name": "Neon Relay", "version": "1.0.0", "commit": "x"},
                "upstream": {"tag": VERSION},
            }
        ).encode("utf-8")
        self.assertTrue(relay.provenance_equivalent(existing, candidate))
        changed = json.loads(existing)
        changed["upstream"]["tag"] = "2.26.0"
        self.assertFalse(
            relay.provenance_equivalent(
                json.dumps(changed).encode("utf-8"), candidate
            )
        )
        self.assertFalse(relay.provenance_equivalent(b"not json", candidate))

class FakeGh:
    """In-memory stand-in for the GitHub CLI release surface."""

    def __init__(self, repo=relay.EXPECTED_DESTINATION_REPO):
        self.repo = repo
        self.releases = []
        self.files = {}
        self.commands = []
        self.corrupt_uploads = {}
        self.next_id = 5000

    def _release(self, tag):
        for release in self.releases:
            if release["tag_name"] == tag:
                return release
        return None

    @staticmethod
    def _option(args, flag):
        return args[args.index(flag) + 1]

    def _add_asset(self, release, name, data):
        asset_id = self.next_id
        self.next_id += 1
        self.files[asset_id] = data
        release["assets"].append(
            {
                "id": asset_id,
                "name": name,
                "size": len(data),
                "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )

    @staticmethod
    def _asset_json(asset):
        return {
            key: asset[key]
            for key in ("id", "name", "size", "state", "digest")
            if key in asset
        }

    def _release_json(self, release):
        return {
            "id": release["id"],
            "tag_name": release["tag_name"],
            "name": release["name"],
            "draft": release["draft"],
            "target_commitish": release["target_commitish"],
            "html_url": release["html_url"],
            "assets": [self._asset_json(asset) for asset in release["assets"]],
        }
    def seed(
        self,
        staged,
        *,
        draft=False,
        target=TARGET_COMMIT,
        corrupt=None,
        omit=(),
        extra=(),
        digestless=False,
    ):
        release = {
            "id": 900 + len(self.releases),
            "tag_name": staged.tag,
            "name": staged.title,
            "draft": draft,
            "target_commitish": target,
            "html_url": (
                f"https://github.com/{self.repo}/releases/tag/{staged.tag}"
            ),
            "assets": [],
        }
        for name in relay.REQUIRED_ASSETS:
            if name in omit:
                continue
            data = staged.files[name].read_bytes()
            if corrupt and name in corrupt:
                data = corrupt[name]
            self._add_asset(release, name, data)
        for name, data in extra:
            self._add_asset(release, name, data)
        if digestless:
            for asset in release["assets"]:
                asset.pop("digest", None)
        self.releases.append(release)
        return release

    def __call__(self, argv, env):
        args = list(argv[1:])
        self.commands.append(args)
        if args[0] == "api":
            return self._api(args)
        if args[0] == "release":
            return self._release_command(args)
        return (1, b"", b"gh: unsupported command")
    def _api(self, args):
        endpoint = args[-1]
        if "--paginate" in args:
            pages = [[self._release_json(item) for item in self.releases]]
            return (0, json.dumps(pages).encode("utf-8"), b"")
        prefix = f"repos/{self.repo}/releases/tags/"
        if endpoint.startswith(prefix):
            release = self._release(endpoint[len(prefix):])
            if release is None:
                return (1, b"", b"gh: Not Found (HTTP 404)")
            return (
                0,
                json.dumps(self._release_json(release)).encode("utf-8"),
                b"",
            )
        return (1, b"", b"gh: Not Found (HTTP 404)")

    def _release_command(self, args):
        action = args[1]
        if action == "create":
            tag = args[2]
            if self._release(tag) is not None:
                return (1, b"", b"gh: release already exists")
            notes_path = Path(self._option(args, "--notes-file"))
            release = {
                "id": 900 + len(self.releases),
                "tag_name": tag,
                "name": self._option(args, "--title"),
                "draft": "--draft" in args,
                "target_commitish": self._option(args, "--target"),
                "html_url": (
                    f"https://github.com/{self.repo}/releases/tag/{tag}"
                ),
                "assets": [],
                "notes": notes_path.read_text(encoding="utf-8"),
            }
            self.releases.append(release)
            return (0, b"", b"")
        if action == "upload":
            tag = args[2]
            release = self._release(tag)
            if release is None:
                return (1, b"", b"gh: release not found")
            paths = args[args.index("--repo") + 2:]
            for raw in paths:
                path = Path(raw)
                data = path.read_bytes()
                if path.name in self.corrupt_uploads:
                    data = self.corrupt_uploads[path.name]
                self._add_asset(release, path.name, data)
            return (0, b"", b"")
        if action == "edit":
            release = self._release(args[2])
            if release is None:
                return (1, b"", b"gh: release not found")
            if "--draft=false" in args:
                release["draft"] = False
            return (0, b"", b"")
        if action == "download":
            release = self._release(args[2])
            if release is None:
                return (1, b"", b"gh: release not found")
            name = self._option(args, "--pattern")
            directory = Path(self._option(args, "--dir"))
            for asset in release["assets"]:
                if asset["name"] == name:
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory / name).write_bytes(self.files[asset["id"]])
                    return (0, b"", b"")
            return (1, b"", b"gh: asset not found")
        return (1, b"", b"gh: unsupported release action")

class TestPublish(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = build_env()
        self.staged = make_relay(self.env["http"]).prepare(
            VERSION, self.root / "out"
        )
        self.gh = FakeGh()

    def relay(self):
        publisher = relay.GhPublisher(
            relay.EXPECTED_DESTINATION_REPO, runner=self.gh
        )
        return make_relay(self.env["http"], publisher=publisher)

    def publish(self, relay_instance=None):
        return (relay_instance or self.relay()).publish(
            self.staged,
            repo=relay.EXPECTED_DESTINATION_REPO,
            target_commit=TARGET_COMMIT,
        )

    def mutations(self):
        return [
            command
            for command in self.gh.commands
            if command[0] == "release" and command[1] != "download"
        ]

    def test_publishes_via_draft_upload_readback(self):
        result = self.publish()
        self.assertEqual(result.outcome, "published")
        self.assertEqual(result.tag, "plezy-v2.25.0")
        release = self.gh.releases[0]
        self.assertFalse(release["draft"])
        self.assertEqual(release["target_commitish"], TARGET_COMMIT)
        self.assertEqual(
            sorted(asset["name"] for asset in release["assets"]),
            sorted(relay.REQUIRED_ASSETS),
        )
        self.assertIn("Neon Relay", release["notes"])
        self.assertIn("2.25.0", release["notes"])

        actions = [command[1] for command in self.mutations()]
        self.assertEqual(actions, ["create", "upload", "edit"])
        create = self.mutations()[0]
        self.assertIn("--draft", create)
        self.assertIn("--latest=false", create)
        self.assertEqual(self.gh._option(create, "--target"), TARGET_COMMIT)
        self.assertNotIn("--clobber", self.mutations()[1])
        self.assertIn("--draft=false", self.mutations()[2])
    def test_noop_when_published_release_matches(self):
        self.gh.seed(self.staged)
        result = self.publish()
        self.assertEqual(result.outcome, "noop")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(len(self.gh.releases), 1)

    def test_noop_when_assets_have_no_digest(self):
        self.gh.seed(self.staged, digestless=True)
        result = self.publish()
        self.assertEqual(result.outcome, "noop")
        self.assertEqual(self.mutations(), [])
        downloads = [c for c in self.gh.commands if c[:2] == ["release", "download"]]
        self.assertTrue(downloads)

    def test_noop_allows_relay_metadata_changes(self):
        provenance = json.loads(self.staged.provenance_bytes)
        provenance["relay"]["version"] = "0.0.1"
        changed = json.dumps(provenance, indent=2, sort_keys=True) + "\n"
        self.gh.seed(
            self.staged, corrupt={"provenance.json": changed.encode("utf-8")}
        )
        result = self.publish()
        self.assertEqual(result.outcome, "noop")
        self.assertEqual(self.mutations(), [])

    def test_refuses_conflicting_published_release(self):
        self.gh.seed(
            self.staged,
            corrupt={
                "plezy-arm64-v8a.apk": flip_last_byte(
                    self.staged.files["plezy-arm64-v8a.apk"].read_bytes()
                )
            },
        )
        with self.assertRaises(relay.ConflictError):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_refuses_incomplete_published_release(self):
        self.gh.seed(self.staged, omit=("provenance.json",))
        with self.assertRaises(relay.ConflictError):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_refuses_unexpected_published_assets(self):
        self.gh.seed(self.staged, extra=(("notes.txt", b"extra"),))
        with self.assertRaises(relay.ConflictError):
            self.publish()
        self.assertEqual(self.mutations(), [])
    def test_resumes_matching_draft(self):
        self.gh.seed(
            self.staged,
            draft=True,
            omit=("SHA256SUMS", "plezy-LICENSE.txt"),
        )
        result = self.publish()
        self.assertEqual(result.outcome, "published")
        self.assertFalse(self.gh.releases[0]["draft"])
        uploads = [
            command
            for command in self.mutations()
            if command[1] == "upload"
        ]
        self.assertEqual(len(uploads), 1)
        uploaded = {Path(raw).name for raw in uploads[0][uploads[0].index("--repo") + 2:]}
        self.assertEqual(uploaded, {"SHA256SUMS", "plezy-LICENSE.txt"})

    def test_refuses_draft_with_different_target_commit(self):
        self.gh.seed(self.staged, draft=True, target="c" * 40)
        with self.assertRaises(relay.ConflictError):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_refuses_draft_with_different_provenance(self):
        provenance = json.loads(self.staged.provenance_bytes)
        provenance["upstream"]["release_id"] = 1
        changed = json.dumps(provenance, sort_keys=True).encode("utf-8")
        self.gh.seed(
            self.staged, draft=True, corrupt={"provenance.json": changed}
        )
        with self.assertRaises(relay.ConflictError):
            self.publish()
        self.assertEqual(self.mutations(), [])
    def test_readback_failure_keeps_release_as_draft(self):
        broken = flip_last_byte(
            self.staged.files["plezy-armeabi-v7a.apk"].read_bytes()
        )
        self.gh.corrupt_uploads = {"plezy-armeabi-v7a.apk": broken}
        with self.assertRaises(relay.PublishError):
            self.publish()
        self.assertTrue(self.gh.releases[0]["draft"])
        actions = [command[1] for command in self.mutations()]
        self.assertNotIn("edit", actions)

    def test_aborts_when_upstream_changes_during_run(self):
        payload = json.loads(json.dumps(self.env["payload"]))
        payload["assets"][0]["digest"] = "sha256:" + "0" * 64
        self.env["opener"].add(
            f"{relay.UPSTREAM_API}/repos/{relay.UPSTREAM_REPO}"
            f"/releases/tags/{VERSION}",
            json.dumps(payload).encode("utf-8"),
        )
        with self.assertRaises(relay.UpstreamError):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_prepare_never_calls_the_publisher(self):
        self.assertEqual(self.gh.commands, [])

    def test_rejects_unexpected_repository(self):
        with self.assertRaises(relay.InputError):
            self.relay().publish(
                self.staged, repo="someone/else", target_commit=TARGET_COMMIT
            )
        self.assertEqual(self.gh.commands, [])

    def test_rejects_incomplete_target_commit(self):
        with self.assertRaises(relay.InputError):
            self.relay().publish(
                self.staged,
                repo=relay.EXPECTED_DESTINATION_REPO,
                target_commit="abc",
            )
        self.assertEqual(self.gh.commands, [])

class TestCli(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_without_publish_writes_files_and_uses_no_publisher(self):
        env = build_env()
        gh = FakeGh()
        args = relay.build_parser().parse_args(
            ["--tag", VERSION, "--output", str(self.root / "out")]
        )
        with mock.patch.object(
            relay, "default_relay_commit", return_value="d" * 40
        ):
            code = relay.run(args, http=env["http"], publisher=gh)
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "out" / "plezy-arm64-v8a.apk").is_file())
        self.assertEqual(gh.commands, [])

    def test_requires_output_without_publish(self):
        args = relay.build_parser().parse_args(["--tag", VERSION])
        with self.assertRaises(relay.InputError):
            relay.run(args, http=build_env()["http"])

    def test_rejects_publish_flags_without_publish(self):
        args = relay.build_parser().parse_args(
            [
                "--tag",
                VERSION,
                "--output",
                str(self.root / "out"),
                "--repo",
                relay.EXPECTED_DESTINATION_REPO,
            ]
        )
        with self.assertRaises(relay.InputError):
            relay.run(args, http=build_env()["http"])

    def test_publish_requires_repo_and_target_commit(self):
        cases = [[], ["--repo", relay.EXPECTED_DESTINATION_REPO]]
        for extra in cases:
            with self.subTest(extra=extra):
                args = relay.build_parser().parse_args(
                    ["--tag", VERSION, "--output", str(self.root), "--publish", *extra]
                )
                with self.assertRaises(relay.InputError):
                    relay.run(
                        args, http=build_env()["http"], publisher=FakeGh()
                    )
    def test_main_reports_input_errors_with_exit_code(self):
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = relay.main(
                ["--tag", "bad tag", "--output", str(self.root / "out")]
            )
        self.assertEqual(code, relay.EXIT_INPUT)
        self.assertIn("error:", stderr.getvalue())

    def test_summary_is_appended(self):
        path = self.root / "summary.md"
        relay.write_summary(path, "first\n")
        relay.write_summary(path, "second\n")
        self.assertEqual(
            path.read_text(encoding="utf-8"), "first\nsecond\n"
        )


if __name__ == "__main__":
    unittest.main()