"""Offline download regressions: no requests to BOOTH or other external hosts."""
import base64
import concurrent.futures
import email.message
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_download as images


ORIGINAL = "https://booth.pximg.net/12345678/real_base_resized.png?token=private&quality=100"
THUMBNAIL = "https://booth.pximg.net/c/300x300_a2_g5/12345678/real_base_resized.png?quality=80"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZsAAAAASUVORK5CYII="
)
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")


class Response:
    def __init__(self, data=PNG, content_type="image/png", length=None, chunk_size=None, delay=0):
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.stream = io.BytesIO(data)
        self.chunk_size = chunk_size
        self.delay = delay
        self.read_bytes = 0
        self.status = 200

    def read1(self, n):
        if self.delay:
            time.sleep(self.delay)
        chunk = self.stream.read(min(n, self.chunk_size) if self.chunk_size else n)
        self.read_bytes += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()


class ImageDownloadTests(unittest.TestCase):
    def setUp(self):
        self.admission = mock.patch.object(images, "_ADMISSION", images._Admission())
        self.interval = mock.patch.object(images, "_START_INTERVAL", 0.005)
        self.admission.start()
        self.interval.start()
        self.addCleanup(self.admission.stop)
        self.addCleanup(self.interval.stop)

    def test_original_url_and_bytes_are_preserved(self):
        with mock.patch.object(images, "_run_worker", return_value=images._Payload(PNG, "image/png")) as worker:
            result = images.download_product_image(ORIGINAL, THUMBNAIL)
        self.assertEqual(worker.call_args.args[0], ORIGINAL)
        self.assertEqual(result.url, ORIGINAL)
        self.assertEqual(result.data, PNG)
        self.assertEqual(result.source, "original")
        self.assertFalse(result.cache_hit)
        self.assertIsNone(result.cache_path)

    def test_original_timeout_falls_back_once_with_separate_budget(self):
        observed = []

        def download(url, limit, deadline, accepted):
            observed.append((url, deadline - time.monotonic()))
            if url == ORIGINAL:
                raise images._AttemptError("timeout")
            return images._Payload(GIF, "image/gif")

        with mock.patch.object(images, "_run_worker", side_effect=download):
            result = images.download_product_image(ORIGINAL, THUMBNAIL, original_timeout=0.2, thumbnail_timeout=0.7)
        self.assertEqual(result.source, "thumbnail")
        self.assertEqual(result.data, GIF)
        self.assertEqual(result.url, THUMBNAIL)
        self.assertIn("original: timeout", result.note)
        self.assertEqual([entry[0] for entry in observed], [ORIGINAL, THUMBNAIL])
        # Monotonic deadlines use floating-point addition/subtraction; permit
        # sub-microsecond rounding while still enforcing the original budget.
        self.assertLessEqual(observed[0][1], 0.2 + 1e-6)
        self.assertGreater(observed[1][1], 0.6)

    def test_missing_original_uses_thumbnail_and_says_why(self):
        with mock.patch.object(images, "_run_worker", return_value=images._Payload(PNG, "image/png")):
            result = images.download_product_image(None, THUMBNAIL)
        self.assertEqual(result.source, "thumbnail")
        self.assertIn("original_unavailable", result.note)

    def test_same_original_and_thumbnail_url_is_not_retried(self):
        with mock.patch.object(images, "_run_worker", side_effect=images._AttemptError("timeout")) as worker:
            with self.assertRaises(images.ImageDownloadError):
                images.download_product_image(ORIGINAL, ORIGINAL)
        self.assertEqual(worker.call_count, 1)

    def test_all_failures_expose_safe_reason_without_url_or_query(self):
        with mock.patch.object(images, "_run_worker", side_effect=images._AttemptError("network_error")) as worker:
            with self.assertRaises(images.ImageDownloadError) as caught:
                images.download_product_image(ORIGINAL, THUMBNAIL)
        self.assertEqual(worker.call_count, 2)
        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(caught.exception.original_reason, "network_error")
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("https", str(caught.exception))

    def test_invalid_hosts_schemes_ports_and_userinfo_are_rejected_before_network(self):
        urls = ["http://booth.pximg.net/a.png", "https://example.com/a.png",
                "https://booth.pximg.net.attacker.test/a.png", "https://booth.pximg.net:444/a.png",
                "https://user@booth.pximg.net/a.png", "https://:password@booth.pximg.net/a.png",
                "https://@booth.pximg.net/a.png", "https://booth.pximg.net/a.png\n",
                "https://booth.pximg.net/a.png#fragment", "https://127.0.0.1/a.png",
                "https://booth.pximg.net\\@example.com/a.png"]
        with mock.patch.object(images, "_run_worker") as worker:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(images.ImageDownloadError):
                    images.download_product_image(url, THUMBNAIL)
        worker.assert_not_called()

    def test_original_cache_hit_avoids_network_and_contains_raw_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(images, "_run_worker", return_value=images._Payload(PNG, "image/png")) as worker:
                first = images.download_product_image(ORIGINAL, THUMBNAIL, cache_dir=root)
                second = images.download_product_image(ORIGINAL, THUMBNAIL, cache_dir=root)
            self.assertEqual(worker.call_count, 1)
            self.assertFalse(first.cache_hit)
            self.assertTrue(second.cache_hit)
            self.assertEqual(second.source, "original")
            self.assertEqual(Path(second.cache_path).read_bytes(), PNG)
            self.assertEqual(Path(second.cache_path).name, hashlib.sha256(ORIGINAL.encode()).hexdigest() + ".img")
            self.assertNotIn("private", " ".join(path.name for path in Path(root).iterdir()))

    def test_corrupt_cache_is_not_returned(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(images, "_run_worker", return_value=images._Payload(PNG, "image/png")) as worker:
                first = images.download_product_image(ORIGINAL, THUMBNAIL, cache_dir=root)
                Path(first.cache_path).write_bytes(b"x" * len(PNG))
                second = images.download_product_image(ORIGINAL, THUMBNAIL, cache_dir=root)
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(second.data, PNG)
            self.assertFalse(second.cache_hit)

    def test_oversized_cache_is_rejected_before_opening_image(self):
        with tempfile.TemporaryDirectory() as root:
            payload = images._Payload(PNG, "image/png")
            path = Path(images._store_cache(root, ORIGINAL, payload))
            path.write_bytes(b"x" * 128)
            original_open = Path.open
            opened_images = []

            def checked_open(candidate, *args, **kwargs):
                if candidate == path:
                    opened_images.append(candidate)
                return original_open(candidate, *args, **kwargs)

            with mock.patch.object(Path, "open", checked_open):
                self.assertIsNone(images._load_cache(root, ORIGINAL, 64, None))
            self.assertEqual(opened_images, [])

    def test_cache_lru_eviction_preserves_recent_image_and_caps_size(self):
        with tempfile.TemporaryDirectory() as root:
            per_entry = len(PNG) + len(json.dumps({"size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest(), "content_type": "image/png"}).encode())
            urls = [f"https://booth.pximg.net/{i}.png" for i in range(3)]
            with mock.patch.object(images, "_CACHE_BYTES", per_entry * 2):
                first = Path(images._store_cache(root, urls[0], images._Payload(PNG, "image/png")))
                second = Path(images._store_cache(root, urls[1], images._Payload(PNG, "image/png")))
                os.utime(first, (1, 1))
                os.utime(second, (2, 2))
                self.assertIsNotNone(images._load_cache(root, urls[0], len(PNG), None))
                third = Path(images._store_cache(root, urls[2], images._Payload(PNG, "image/png")))
            self.assertTrue(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue(third.exists())
            self.assertLessEqual(sum(p.stat().st_size for p in Path(root).iterdir()), per_entry * 2)

    def test_at_most_two_concurrent_network_attempts(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        def download(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04)
                return images._Payload(PNG, "image/png")
            finally:
                with lock:
                    active -= 1

        with mock.patch.object(images, "_run_worker", side_effect=download), concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: images.download_product_image(ORIGINAL, THUMBNAIL), range(6)))
        self.assertEqual(len(results), 6)
        self.assertEqual(peak, 2)
        self.assertEqual(images._ADMISSION.active, 0)

    def test_minimum_start_spacing(self):
        starts = []

        def download(*args):
            starts.append(time.monotonic())
            return images._Payload(PNG, "image/png")

        with mock.patch.object(images, "_START_INTERVAL", 0.035), mock.patch.object(images, "_run_worker", side_effect=download):
            for _ in range(3):
                images.download_product_image(ORIGINAL, THUMBNAIL)
        self.assertTrue(all(b - a >= 0.03 for a, b in zip(starts, starts[1:])))

    def test_rate_limit_cooldown_prevents_immediate_fallback_or_new_call(self):
        with mock.patch.object(images, "_run_worker", side_effect=images._AttemptError("rate_limited", "60")) as worker:
            for _ in range(2):
                with self.assertRaises(images.ImageDownloadError) as caught:
                    images.download_product_image(ORIGINAL, THUMBNAIL)
                self.assertEqual(caught.exception.code, "cooldown")
            self.assertEqual(worker.call_count, 1)

    def test_retry_after_on_other_error_is_obeyed(self):
        with mock.patch.object(images, "_run_worker", side_effect=images._AttemptError("http_error", "60")) as worker:
            with self.assertRaises(images.ImageDownloadError):
                images.download_product_image(ORIGINAL, THUMBNAIL)
            self.assertEqual(worker.call_count, 1)

    def test_bounded_wait_for_busy_slots(self):
        images._ADMISSION.active = 2
        started = time.monotonic()
        with mock.patch.object(images, "_run_worker") as worker:
            with self.assertRaises(images.ImageDownloadError):
                images.download_product_image(ORIGINAL, THUMBNAIL, original_timeout=0.025, thumbnail_timeout=0.025)
        self.assertLess(time.monotonic() - started, 0.3)
        worker.assert_not_called()

    def test_read_limit_applies_even_without_content_length(self):
        response = Response(PNG + b"x" * 512)
        with self.assertRaises(images.ImageDownloadError) as caught:
            images._read_response(response, 128, time.monotonic() + 1, None)
        self.assertEqual(caught.exception.code, "too_large")
        self.assertEqual(response.read_bytes, 129)

    def test_announced_oversize_rejects_without_body_read(self):
        response = Response(length=1000)
        with self.assertRaises(images.ImageDownloadError) as caught:
            images._read_response(response, 128, time.monotonic() + 1, None)
        self.assertEqual(caught.exception.code, "too_large")
        self.assertEqual(response.read_bytes, 0)

    def test_truncated_content_length_and_image_trailer(self):
        for response in (Response(length=len(PNG) + 1), Response(PNG[:-1]), Response(GIF[:-1], "image/gif")):
            with self.subTest(response=response), self.assertRaises(images.ImageDownloadError) as caught:
                images._read_response(response, 1024, time.monotonic() + 1, None)
            self.assertEqual(caught.exception.code, "truncated")

    def test_non_image_and_mismatched_body_are_rejected(self):
        for response, reason in ((Response(b"html", "text/html"), "unsupported_type"),
                                 (Response(b"html", "image/png"), "invalid_image")):
            with self.assertRaises(images.ImageDownloadError) as caught:
                images._read_response(response, 1024, time.monotonic() + 1, None)
            self.assertEqual(caught.exception.code, reason)

    def test_optional_decoder_format_filter_causes_thumbnail_fallback(self):
        def download(url, limit, deadline, accepted):
            response = Response(GIF, "image/gif") if url == ORIGINAL else Response()
            return images._read_response(response, limit, deadline, accepted)

        with mock.patch.object(images, "_run_worker", side_effect=download):
            result = images.download_product_image(ORIGINAL, THUMBNAIL, accepted_types=("image/jpeg", "image/png"))
        self.assertEqual(result.source, "thumbnail")
        self.assertIn("unsupported_type", result.note)
        self.assertEqual(result.data, PNG)

    def test_slow_drip_reader_stops_at_wall_clock_deadline(self):
        response = Response(chunk_size=1, delay=0.012)
        started = time.monotonic()
        with self.assertRaises(images.ImageDownloadError) as caught:
            images._read_response(response, 1024, started + 0.045, None)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertLess(response.read_bytes, len(PNG))

    def test_exact_request_url_referer_and_no_redirect(self):
        opener = mock.Mock()
        opener.open.return_value = Response(length=len(PNG))
        with mock.patch.object(images.urllib.request, "build_opener", return_value=opener) as build, mock.patch.object(
                images.urllib.request, "ProxyHandler", wraps=images.urllib.request.ProxyHandler) as proxy:
            result = images._download_http(ORIGINAL, 1024, 1, None)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ORIGINAL)
        self.assertEqual(request.get_header("Referer"), "https://booth.pm/")
        self.assertTrue(any(isinstance(handler, images._NoRedirect) for handler in build.call_args.args))
        self.assertIsNone(images._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))
        self.assertEqual(result.data, PNG)
        proxy.assert_called_once_with()

    def test_http_rate_limits_and_redirect_errors_preserve_safe_reason(self):
        for status, reason in ((429, "rate_limited"), (503, "rate_limited"), (302, "redirect")):
            headers = email.message.Message()
            headers["Retry-After"] = "90"
            error = images.urllib.error.HTTPError(ORIGINAL, status, "private", headers, io.BytesIO(b"private"))
            opener = mock.Mock()
            opener.open.side_effect = error
            with self.subTest(status=status), mock.patch.object(images.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(images.ImageDownloadError) as caught:
                    images._download_http(ORIGINAL, 1024, 1, None)
            self.assertEqual(caught.exception.code, reason)
            self.assertEqual(caught.exception.retry_after, "90")
            self.assertNotIn("private", str(caught.exception))

    def test_socket_timeout_wrapped_in_urlerror_is_reported_as_timeout(self):
        opener = mock.Mock()
        opener.open.side_effect = images.urllib.error.URLError(TimeoutError("private"))
        with mock.patch.object(images.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(images.ImageDownloadError) as caught:
                images._download_http(ORIGINAL, 1024, 1, None)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertNotIn("private", str(caught.exception))

    def test_image_framing_error_keeps_retry_after(self):
        opener = mock.Mock()
        response = Response(PNG[:-1])
        response.headers["Retry-After"] = "90"
        opener.open.return_value = response
        with mock.patch.object(images.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(images.ImageDownloadError) as caught:
                images._download_http(ORIGINAL, 1024, 1, None)
        self.assertEqual(caught.exception.code, "truncated")
        self.assertEqual(caught.exception.retry_after, "90")

    def test_invalid_options(self):
        options = [{"original_timeout": float("nan")}, {"thumbnail_timeout": 0},
                   {"max_bytes": 8 * 1024 * 1024 + 1}, {"max_bytes": True},
                   {"accepted_types": ()}, {"accepted_types": ("text/html",)}]
        for kwargs in options:
            with self.subTest(kwargs=kwargs), self.assertRaises(images.ImageDownloadError):
                images.download_product_image(ORIGINAL, THUMBNAIL, **kwargs)


class WorkerLifetimeTests(unittest.TestCase):
    def test_stuck_network_child_is_killed_and_reaped_at_deadline(self):
        real_popen = subprocess.Popen
        children = []
        script = (
            "import sys,time; "
            f"sys.path.insert(0, {str(Path(images.__file__).parent)!r}); "
            "import image_download as m; "
            "m._download_http=lambda *a: time.sleep(30); "
            "raise SystemExit(m._worker_main())"
        )

        def offline_child(args, **kwargs):
            child = real_popen([sys.executable, "-u", "-c", script], **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with mock.patch.object(images.subprocess, "Popen", side_effect=offline_child):
            with self.assertRaises(images.ImageDownloadError) as caught:
                images._run_worker(ORIGINAL, 1024, started + 0.4, None)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(children[0].poll())

    def test_parent_pipe_eof_terminates_worker_during_network_operation(self):
        script = (
            "import sys,time; "
            f"sys.path.insert(0, {str(Path(images.__file__).parent)!r}); "
            "import image_download as m; "
            "m._download_http=lambda *a: time.sleep(30); "
            "raise SystemExit(m._worker_main())"
        )
        config = json.dumps({"url": ORIGINAL, "max_bytes": 1024, "timeout": 30, "accepted_types": None}).encode() + b"\n"
        child = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            started = time.monotonic()
            child.communicate(config, timeout=3)
            self.assertEqual(child.returncode, 2)
            self.assertLess(time.monotonic() - started, 3)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def test_successful_child_returns_real_bytes_without_parent_eof(self):
        real_popen = subprocess.Popen
        script = (
            "import sys; "
            f"sys.path.insert(0, {str(Path(images.__file__).parent)!r}); "
            "import image_download as m; "
            f"m._download_http=lambda *a: m._Payload({PNG!r}, 'image/png'); "
            "raise SystemExit(m._worker_main())"
        )

        def offline_child(args, **kwargs):
            return real_popen([sys.executable, "-u", "-c", script], **kwargs)

        with mock.patch.object(images.subprocess, "Popen", side_effect=offline_child):
            result = images._run_worker(ORIGINAL, 1024, time.monotonic() + 3, None)
        self.assertEqual(result.data, PNG)


if __name__ == "__main__":
    unittest.main()
