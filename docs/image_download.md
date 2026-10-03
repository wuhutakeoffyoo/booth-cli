# Public product image download API

`image_download.py` is an optional, standard-library-only Python 3.10+ module.
It downloads public product preview images from `https://booth.pximg.net/`.
It does not retrieve purchased assets, require account credentials, or add a CLI.

```python
from image_download import ImageDownloadError, download_product_image

try:
    result = download_product_image(
        (product.get("original_images") or [None])[0],  # exact metadata URL
        product.get("thumbnail"),
        cache_dir=".image-cache",      # None disables disk caching
        original_timeout=12.0,
        thumbnail_timeout=10.0,
        max_bytes=8 * 1024 * 1024,
        # Optional restriction for a decoder supporting only these formats:
        # accepted_types=("image/jpeg", "image/png"),
    )
except ImageDownloadError as exc:
    print(exc.code)  # safe reason; no URLs, query strings or response bodies
else:
    print(result.source, result.content_type, result.elapsed_seconds, result.note)
    image_bytes = result.data
```

The original URL must be copied exactly from original image metadata. Do not
strip `_base_resized`, remove a resize prefix, change the query string, or infer
an original URL from a thumbnail. Both input URLs are validated before any
network request. Only HTTPS, the exact `booth.pximg.net` host, and the default
HTTPS port (or explicit port 443) are allowed. User information, fragments and
redirects are rejected. Requests send `Referer: https://booth.pm/`. Standard
`urllib` proxy configuration is respected, including environment proxy settings.
TLS certificate verification remains enabled. Proxy URLs, credentials, response
bodies and request URLs are never included in raised error messages.

## Result and fallback

The frozen `ProductImageResult` contains:

| Field | Meaning |
| --- | --- |
| `data: bytes` | Actual downloaded or validated cached bytes; no resizing or upscaling. |
| `source: str` | `original` or `thumbnail`, including on cache hits. |
| `content_type: str` | Normalized server media type. |
| `elapsed_seconds: float` | Monotonic elapsed time for the entire call, including fallback. |
| `note: str` | Download/cache state, or the safe original failure reason plus thumbnail state. |
| `url: str` | Exact successful URL. This may contain query data; do not log it indiscriminately. |
| `cache_hit: bool` | Whether these bytes came from an existing verified cache entry. |
| `cache_path: str or None` | Absolute path to raw cached bytes, when disk caching succeeded. |

An original failure permits one thumbnail attempt with its own time budget.
An absent original (`None` or `""`) goes directly to the supplied thumbnail and
records `original_unavailable`. A duplicate URL is never retried as a thumbnail.
If both attempts fail, `ImageDownloadError.code` describes the thumbnail failure
and `original_reason` preserves the first failure. Invalid options or URLs fail
before any request. Common reasons include `timeout`, `network_error`,
`unsupported_type`, `too_large`, `truncated`, `rate_limited`, and `cooldown`.

All responses must have an `image/*` content type and fit the byte limit, which
cannot exceed 8 MiB. `accepted_types` optionally restricts the downstream
decoder's formats and makes an unsupported original fall back to the thumbnail.
Only status 200 is accepted. Announced oversized bodies, Content-Length
mismatches, incomplete HTTP framing, and compressed HTTP content encodings are
rejected. JPEG, PNG, GIF and WebP also receive basic signature/end framing
checks. These checks are not full image decoding: consumers must independently
enforce supported codecs and decoded dimensions/pixel-memory limits.

## Time, concurrency and cancellation

Admission and network operations share each attempt's wall-clock budget. The
network subprocess includes DNS lookup, TLS, headers and streamed body reads.
At the deadline the caller kills and reaps that process; it does not return
while a timed-out download thread continues in the background. Child stdin is
held open by the caller. Parent death/bridge cancellation closes that pipe and
the worker exits, even during a blocked network operation. The public API is
synchronous; it has no separate cancellation-token argument.

At most two network attempts run concurrently **per importing Python process**,
with at least 250 ms between starts. Threads share this limiter. Independent
Python processes need an additional caller-owned gate if a global limit is
required. HTTP 429 and 503 create a shared cooldown of at least 30 seconds;
longer `Retry-After` values are honored. Other responses carrying `Retry-After`
also establish cooldown. A request whose deadline cannot accommodate cooldown
fails immediately; fallback never bypasses it. Already active downloads are
allowed to finish. There are no automatic retries beyond the single fallback.

The timeout excludes a guarantee against a hung local filesystem or a stalled
operating-system process creation/termination call. Use a local cache directory.
Routine cache reads, process startup and cleanup contribute to reported elapsed
time. Very small timeouts may expire before a worker can start.

## Cache

Pass a dedicated cache directory. `None` performs no disk-cache IO. Each exact
URL is SHA-256 hashed into an `.img` filename with a small `.json` sidecar storing
content type, byte length and content digest. URLs and credentials are not
written into cache metadata. Images remain raw, directly decodable files.

Reads check sizes before bounded reads, verify hashes and image framing, and
reject malformed or oversized entries. Writes use atomic replacement. A failed
cache read/write does not turn a successful network download into an error.
Least-recently-used eviction caps managed image/metadata entries at 64 MiB
after successful writes. Unrelated files are not removed. Cache locking is
process-local; concurrent independent processes can temporarily exceed the cap.
Consumers should treat `cache_path` as ephemeral because future eviction can
remove it; `data` always holds the returned bytes.

Run the entirely offline tests with:

```shell
python -m unittest discover -s tests -p test_image_download.py -v
```
