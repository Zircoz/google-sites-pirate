"""
Unit tests for google_sites_pirate.vault_export.

Everything here is fully offline: no real Vault/Cloud Storage API calls are
made. googleapiclient's HttpError, google.auth's RefreshError, and the
lazily-imported google.cloud.storage module are all faked/stubbed.

Run with:
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import types
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from google.auth import exceptions as google_auth_exceptions
from googleapiclient.errors import HttpError

from google_sites_pirate import vault_export


# ---------------------------------------------------------------------------
# Shared fakes/helpers
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    """Minimal stand-in for the httplib2.Response object HttpError wraps."""

    def __init__(self, status, reason=""):
        self.status = status
        self.reason = reason


def _http_error(status, message="synthetic error"):
    content = json.dumps({"error": {"message": message}}).encode("utf-8")
    return HttpError(_FakeHttpResponse(status), content)


def _make_create_capturing_service(created_id="export-1"):
    """A fake vault service whose matters().exports().create() is spy-able."""
    vault_svc = MagicMock()
    create_mock = vault_svc.matters.return_value.exports.return_value.create
    create_mock.return_value.execute.return_value = {"id": created_id}
    return vault_svc, create_mock


def _make_get_export_service(side_effect):
    """A fake vault service whose matters().exports().get().execute() plays
    back `side_effect` (a list of return values / exceptions, or a single
    return value used for every call)."""
    vault_svc = MagicMock()
    execute = vault_svc.matters.return_value.exports.return_value.get.return_value.execute
    if isinstance(side_effect, list):
        execute.side_effect = side_effect
    else:
        execute.return_value = side_effect
    return vault_svc


def _make_fake_storage_module():
    """A fake `google.cloud.storage` module. Returns (module, download_calls)."""
    download_calls = []

    class _FakeBlob:
        def __init__(self, bucket_name, object_name):
            self._bucket_name = bucket_name
            self._object_name = object_name

        def download_to_filename(self, path):
            download_calls.append((self._bucket_name, self._object_name, path))
            Path(path).write_bytes(b"fake downloaded content")

    class _FakeBucket:
        def __init__(self, bucket_name):
            self._bucket_name = bucket_name

        def blob(self, object_name):
            return _FakeBlob(self._bucket_name, object_name)

    class _FakeClient:
        def __init__(self, credentials=None, project=None):
            self.credentials = credentials
            self.project = project

        def bucket(self, bucket_name):
            return _FakeBucket(bucket_name)

    module = types.ModuleType("google.cloud.storage")
    module.Client = _FakeClient
    return module, download_calls


# ---------------------------------------------------------------------------
# _execute_with_retry
# ---------------------------------------------------------------------------


class ExecuteWithRetryTests(unittest.TestCase):
    def test_returns_value_on_first_success(self):
        request_factory = MagicMock()
        request_factory.return_value.execute.return_value = {"ok": True}
        sleep_calls = []

        result = vault_export._execute_with_retry(
            request_factory, "Doing thing", sleep_fn=sleep_calls.append
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleep_calls, [])
        self.assertEqual(request_factory.call_count, 1)

    def test_retries_transient_errors_then_succeeds_with_exponential_delays(self):
        request = MagicMock()
        request.execute.side_effect = [
            _http_error(503),
            _http_error(429),
            {"ok": True},
        ]
        sleep_calls = []

        result = vault_export._execute_with_retry(
            lambda: request,
            "Doing thing",
            base_delay_seconds=2.0,
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.execute.call_count, 3)
        # base_delay_seconds * 2**(attempt-1) for attempts 1 and 2.
        self.assertEqual(sleep_calls, [2.0, 4.0])

    def test_raises_runtime_error_after_exhausting_max_attempts(self):
        request = MagicMock()
        request.execute.side_effect = _http_error(500)
        sleep_calls = []

        with self.assertRaises(RuntimeError):
            vault_export._execute_with_retry(
                lambda: request,
                "Doing thing",
                max_attempts=3,
                base_delay_seconds=1.0,
                sleep_fn=sleep_calls.append,
            )

        self.assertEqual(request.execute.call_count, 3)
        self.assertEqual(sleep_calls, [1.0, 2.0])

    def test_403_raises_immediately_without_retry_and_includes_matter_hint(self):
        request = MagicMock()
        request.execute.side_effect = _http_error(403)
        sleep_calls = []

        with self.assertRaises(RuntimeError) as ctx:
            vault_export._execute_with_retry(
                lambda: request,
                "Doing thing",
                max_attempts=5,
                sleep_fn=sleep_calls.append,
            )

        self.assertEqual(request.execute.call_count, 1)
        self.assertEqual(sleep_calls, [])
        message = str(ctx.exception)
        self.assertIn("not authorized on", message)
        self.assertIn("Matter", message)

    def test_refresh_error_raises_runtime_error_with_dwd_hint(self):
        request = MagicMock()
        request.execute.side_effect = google_auth_exceptions.RefreshError("no token")

        with self.assertRaises(RuntimeError) as ctx:
            vault_export._execute_with_retry(lambda: request, "Doing thing")

        message = str(ctx.exception)
        self.assertIn("Domain-Wide Delegation", message)
        self.assertEqual(request.execute.call_count, 1)


# ---------------------------------------------------------------------------
# poll_export
# ---------------------------------------------------------------------------


class PollExportTests(unittest.TestCase):
    def test_returns_export_once_completed_after_in_progress_polls(self):
        vault_svc = _make_get_export_service(
            [
                {"status": "IN_PROGRESS"},
                {"status": "IN_PROGRESS"},
                {"status": "COMPLETED", "id": "exp-1"},
            ]
        )
        sleep_calls = []

        result = vault_export.poll_export(
            vault_svc,
            "matter-1",
            "exp-1",
            poll_interval_seconds=15,
            sleep_fn=sleep_calls.append,
        )

        self.assertEqual(result, {"status": "COMPLETED", "id": "exp-1"})
        self.assertEqual(sleep_calls, [15, 15])

    def test_raises_runtime_error_on_failed_status_naming_export_id(self):
        vault_svc = _make_get_export_service({"status": "FAILED"})

        with self.assertRaises(RuntimeError) as ctx:
            vault_export.poll_export(
                vault_svc, "matter-1", "exp-xyz", sleep_fn=lambda s: None
            )

        self.assertIn("exp-xyz", str(ctx.exception))

    def test_raises_timeout_error_when_deadline_passes(self):
        vault_svc = _make_get_export_service({"status": "IN_PROGRESS"})
        # First monotonic() call computes the deadline, second is the
        # in-loop check that must find the deadline already passed.
        monotonic_values = iter([0.0, 1000.0])

        with patch.object(
            vault_export.time, "monotonic", side_effect=lambda: next(monotonic_values)
        ):
            with self.assertRaises(TimeoutError) as ctx:
                vault_export.poll_export(
                    vault_svc,
                    "matter-1",
                    "exp-abc",
                    timeout_seconds=10,
                    sleep_fn=lambda s: None,
                )

        self.assertIn("--export-id exp-abc", str(ctx.exception))


# ---------------------------------------------------------------------------
# create_sites_export
# ---------------------------------------------------------------------------


class CreateSitesExportTests(unittest.TestCase):
    def test_raises_value_error_when_neither_org_unit_nor_account_emails_given(self):
        vault_svc, _ = _make_create_capturing_service()

        with self.assertRaises(ValueError):
            vault_export.create_sites_export(vault_svc, "matter-1", "export name")

    def test_raises_value_error_when_both_org_unit_and_account_emails_given(self):
        vault_svc, _ = _make_create_capturing_service()

        with self.assertRaises(ValueError):
            vault_export.create_sites_export(
                vault_svc,
                "matter-1",
                "export name",
                org_unit_id="ou-1",
                account_emails=["a@example.com"],
            )

    def test_org_unit_export_body_shape(self):
        vault_svc, create_mock = _make_create_capturing_service()

        vault_export.create_sites_export(
            vault_svc, "matter-1", "export name", org_unit_id="ou-123"
        )

        body = create_mock.call_args.kwargs["body"]
        self.assertEqual(create_mock.call_args.kwargs["matterId"], "matter-1")
        self.assertEqual(body["query"]["corpus"], "DRIVE")
        self.assertEqual(body["query"]["dataScope"], "ALL_DATA")
        self.assertEqual(body["query"]["terms"], "type:site")
        self.assertEqual(body["query"]["searchMethod"], "ORG_UNIT")
        self.assertEqual(body["query"]["orgUnitInfo"], {"orgUnitId": "ou-123"})
        self.assertNotIn("accountInfo", body["query"])

    def test_account_export_body_shape(self):
        vault_svc, create_mock = _make_create_capturing_service()
        emails = ["a@example.com", "b@example.com"]

        vault_export.create_sites_export(
            vault_svc, "matter-1", "export name", account_emails=emails
        )

        body = create_mock.call_args.kwargs["body"]
        self.assertEqual(body["query"]["searchMethod"], "ACCOUNT")
        self.assertEqual(body["query"]["accountInfo"], {"emails": emails})
        self.assertNotIn("orgUnitInfo", body["query"])

    def test_include_shared_drives_false_reflected_in_query(self):
        vault_svc, create_mock = _make_create_capturing_service()

        vault_export.create_sites_export(
            vault_svc,
            "matter-1",
            "export name",
            org_unit_id="ou-1",
            include_shared_drives=False,
        )

        body = create_mock.call_args.kwargs["body"]
        self.assertEqual(
            body["query"]["driveOptions"], {"includeSharedDrives": False}
        )


# ---------------------------------------------------------------------------
# _safe_relative_path
# ---------------------------------------------------------------------------


class SafeRelativePathTests(unittest.TestCase):
    def test_keeps_last_two_segments_of_a_multi_segment_name(self):
        result = vault_export._safe_relative_path("export-run/2024-01-01/subdir/file.zip")
        self.assertEqual(result, Path("subdir", "file.zip"))

    def test_strips_traversal_segments(self):
        result = vault_export._safe_relative_path("../../etc/passwd")
        self.assertEqual(result, Path("etc", "passwd"))
        self.assertNotIn("..", result.parts)

    def test_single_segment_name_returns_just_that_name(self):
        result = vault_export._safe_relative_path("file.zip")
        self.assertEqual(result, Path("file.zip"))

    def test_raises_value_error_on_unusable_or_empty_name(self):
        with self.assertRaises(ValueError):
            vault_export._safe_relative_path("")
        with self.assertRaises(ValueError):
            vault_export._safe_relative_path("../..")


# ---------------------------------------------------------------------------
# _extract_zip_safely
# ---------------------------------------------------------------------------


class ExtractZipSafelyTests(unittest.TestCase):
    def test_extracts_good_files_and_blocks_zip_slip_entry(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dest_dir = tmp_path / "dest"
            dest_dir.mkdir()
            zip_path = tmp_path / "archive.zip"

            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("good.txt", "hello")
                zf.writestr("nested/sub.txt", "world")
                zf.writestr("../evil.txt", "pwned")

            vault_export._extract_zip_safely(zip_path, dest_dir)

            self.assertTrue((dest_dir / "good.txt").is_file())
            self.assertTrue((dest_dir / "nested" / "sub.txt").is_file())

            # The traversal entry must not have escaped dest_dir, whether by
            # landing next to it, above it, or anywhere else in tmp_path.
            self.assertFalse((tmp_path / "evil.txt").exists())
            self.assertFalse((dest_dir / "evil.txt").exists())
            extracted_files = [p for p in tmp_path.rglob("*") if p.is_file()]
            for path in extracted_files:
                if path == zip_path:
                    continue
                resolved = path.resolve()
                self.assertTrue(
                    str(resolved).startswith(str(dest_dir.resolve()) + os.sep),
                    f"{resolved} escaped dest_dir",
                )


# ---------------------------------------------------------------------------
# download_export
# ---------------------------------------------------------------------------


class DownloadExportTests(unittest.TestCase):
    def test_raises_runtime_error_when_no_cloud_storage_sink_files(self):
        module, download_calls = _make_fake_storage_module()
        export = {"id": "exp-empty", "cloudStorageSink": {"files": []}}

        with TemporaryDirectory() as tmp:
            with patch.dict(sys.modules, {"google.cloud.storage": module}):
                with self.assertRaises(RuntimeError) as ctx:
                    vault_export.download_export(export, Path(tmp), creds=object())

        self.assertIn("exp-empty", str(ctx.exception))
        self.assertEqual(download_calls, [])

    def test_skips_malformed_sink_entries_without_raising(self):
        module, download_calls = _make_fake_storage_module()
        export = {
            "id": "exp-ok",
            "cloudStorageSink": {
                "files": [
                    {"bucketName": "bucket-1"},  # missing objectName
                    {"objectName": "some/object.txt"},  # missing bucketName
                    {"bucketName": "bucket-1", "objectName": "run1/dir/good.txt"},
                ]
            },
        }

        with TemporaryDirectory() as tmp:
            dest_dir = Path(tmp) / "dest"
            with patch.dict(sys.modules, {"google.cloud.storage": module}):
                downloaded = vault_export.download_export(export, dest_dir, creds=object())

            self.assertEqual(len(downloaded), 1)
            self.assertTrue(downloaded[0].is_file())
        self.assertEqual(len(download_calls), 1)
        self.assertEqual(download_calls[0][0], "bucket-1")
        self.assertEqual(download_calls[0][1], "run1/dir/good.txt")


if __name__ == "__main__":
    unittest.main()
