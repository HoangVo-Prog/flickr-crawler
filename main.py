"""Download image URLs with a visible UC Chrome browser. Run: python main.py"""
import argparse
import csv
import hashlib
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image, UnidentifiedImageError
from driver_setup import create_driver, get_user_agents, rotate_user_agent

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("image_downloader")
FORMATS = {
    "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
    "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tif", "AVIF": ".avif",
    "MPO": ".mpo",  # Multi-Picture Object (multiple JPEG frames).
}

# A direct image URL normally opens in Chrome's image viewer, not the download
# manager. Fetch it on its own origin, then click a blob-backed download link.
# This uses the browser's normal session and download configuration.
DOWNLOAD_JS = r"""
const [url, filename, timeoutMs] = arguments;
const done = arguments[arguments.length - 1];
(async () => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      credentials: 'same-origin', cache: 'force-cache', signal: ctrl.signal
    });
    if (!res.ok) return {ok: false, status: res.status, error: `HTTP ${res.status}`};
    const mime = (res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (!mime.startsWith('image/') && mime !== 'application/octet-stream') {
      return {ok: false, status: res.status, error: `Not an image: ${mime || 'unknown type'}`};
    }
    const blob = await res.blob();
    if (!blob.size) return {ok: false, status: res.status, error: 'Empty image'};
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    return {ok: true, status: res.status, bytes: blob.size};
  } catch (e) {
    return {ok: false, status: 0, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
})().then(done).catch(e => done({ok: false, status: 0, error: String(e)}));
"""


class DownloadError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def read_urls(path: Path):
    """Accept plain URLs and lines of the form \"https://...\",."""
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        value = raw.strip()
        if not value or value in ("[", "]") or value.startswith("#"):
            continue
        value = value.rstrip(",").strip().strip('"\'')
        yield line_no, value


def image_basename(url: str):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Invalid HTTP(S) URL")
    original = Path(unquote(parts.path)).name
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(original).stem)[:110] or "image"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    suffix = Path(original).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif", ".mpo"}:
        suffix = ".jpg"  # Temporary name; final suffix comes from file contents.
    return f"{stem}_{digest}", suffix


def valid_image(path: Path):
    """Return extension according to image bytes (not URL/file extension).

    MPO is JPEG-based but can contain several frames. Preserve the complete
    original MPO instead of converting it to a single-frame JPEG.
    """
    with Image.open(path) as im:
        ext = FORMATS.get(im.format)
        if not ext:
            raise ValueError(f"Unsupported image format: {im.format}")
        if im.format == "MPO":
            # Validate every frame, not only the initial JPEG preview.
            for frame in range(im.n_frames):
                im.seek(frame)
                im.load()
        else:
            im.verify()
    return ext


def existing_image(folder: Path, basename: str):
    """Find an existing image, including a previously misnamed MPO *.jpg.

    Old versions rejected MPO *after* downloading it, leaving its original
    *.jpg on disk. Repair the suffix in-place and reuse the downloaded bytes.
    """
    suffixes = set(FORMATS.values()) | {".jpeg", ".tiff"}
    # Prefer correctly named files before migrating older, misnamed files.
    candidates = [folder / (basename + ext) for ext in sorted(suffixes)]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size == 0:
                raise ValueError("Empty file")
            actual_ext = valid_image(candidate)
            correct_path = folder / (basename + actual_ext)
            if candidate != correct_path:
                if correct_path.is_file():
                    # Do not overwrite a valid file that was already downloaded.
                    try:
                        if valid_image(correct_path) == actual_ext:
                            return correct_path
                    except (OSError, ValueError, UnidentifiedImageError):
                        pass
                    LOG.warning("Replacing invalid existing file: %s", correct_path.name)
                    correct_path.unlink()
                candidate.rename(correct_path)
                LOG.info("Renamed existing image: %s -> %s", candidate.name,
                         correct_path.name)
            return correct_path
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            LOG.warning("Ignoring invalid existing file: %s (%s)", candidate.name, exc)
    return None


def wait_for_download(folder: Path, filename: str, timeout: int):
    target = folder / filename
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Chrome uses a .crdownload suffix until download completion.
        if target.is_file() and not (folder / (filename + ".crdownload")).exists():
            if target.stat().st_size > 0:
                return target
        time.sleep(0.4)
    raise TimeoutError(f"Download not completed within {timeout}s: {filename}")


def download_image(driver, url: str, folder: Path, basename: str,
                   suffix: str, timeout: int):
    provisional_name = basename + suffix
    provisional_path = folder / provisional_name
    # A previous attempt may have completed but been rejected as MPO. Reuse it.
    recovered = existing_image(folder, basename)
    if recovered:
        return recovered
    if provisional_path.exists():
        # A broken previous attempt must not trigger Chrome's (1) suffix.
        provisional_path.unlink()

    driver.get(url)
    response = driver.execute_async_script(DOWNLOAD_JS, driver.current_url,
                                           provisional_name, timeout * 1000)
    if not response or not response.get("ok"):
        response = response or {}
        raise DownloadError(response.get("error", "Browser fetch failed"),
                            int(response.get("status", 0)))

    path = wait_for_download(folder, provisional_name, timeout)
    actual_extension = valid_image(path)  # Never trust URL extension or MIME alone.
    final_path = folder / (basename + actual_extension)
    if path != final_path:
        if final_path.exists():
            path.unlink()  # Keep the previously downloaded file.
        else:
            path.rename(final_path)
    return final_path


def configure_logging(folder: Path):
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    for old_handler in LOG.handlers[:]:
        LOG.removeHandler(old_handler)
        old_handler.close()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    for handler in (logging.StreamHandler(),
                    logging.FileHandler(folder / "download.log", encoding="utf-8")):
        handler.setFormatter(formatter)
        LOG.addHandler(handler)


def main():
    parser = argparse.ArgumentParser(description="Download images from urls.txt via UC Chrome")
    parser.add_argument("--input", type=Path, default=ROOT / "urls.txt")
    parser.add_argument("--output", type=Path, default=ROOT / "images")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Seconds for an image fetch and for disk download")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--chrome-path", type=Path, default=None,
                        help="Path to Chrome executable (auto-detected by default)")
    parser.add_argument('--ua-rotate-every', type=int, default=1,
                        help='Rotate User-Agent every N attempted URLs (default: 1; 0 disables)')
    parser.add_argument('--user-agents-file', type=Path, default=None,
                        help='Optional UTF-8 text file with one User-Agent per line')
    args = parser.parse_args()

    if not args.input.is_file():
        parser.error(f"URL list not found: {args.input}")
    if args.timeout < 1 or args.retries < 1:
        parser.error('--timeout and --retries must be positive')
    if args.ua_rotate_every < 0:
        parser.error('--ua-rotate-every must be >= 0')
    if args.user_agents_file is not None and not args.user_agents_file.is_file():
        parser.error(f'User-Agent file not found: {args.user_agents_file}')

    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    configure_logging(folder)
    seen = set()
    results = {"success": 0, "failed": 0, "skipped": 0}
    driver = None
    user_agents = []
    previous_agent = None
    urls_attempted = 0
    rotation_enabled = args.ua_rotate_every > 0

    with (folder / "status.csv").open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(["timestamp", "line", "status", "url", "file", "detail"])

        def record(line_no, status, url, filename="", detail=""):
            results[status] += 1
            writer.writerow([datetime.now().isoformat(timespec="seconds"),
                             line_no, status, url, filename, detail])
            f.flush()
            LOG.info("[%s] line=%d %s %s", status.upper(), line_no, url, detail)

        try:
            for line_no, url in read_urls(args.input):
                try:
                    basename, suffix = image_basename(url)
                except ValueError as exc:
                    record(line_no, "failed", url, detail=str(exc))
                    continue
                if url in seen:
                    record(line_no, "skipped", url, detail="Duplicate URL in input")
                    continue
                seen.add(url)
                existing = existing_image(folder, basename)
                if existing:
                    record(line_no, "skipped", url, existing.name, "Already downloaded")
                    continue

                if driver is None:
                    try:
                        driver = create_driver(folder, page_timeout=45,
                                               script_timeout=args.timeout + 15,
                                               chrome_path=args.chrome_path)
                        if rotation_enabled:
                            try:
                                user_agents = get_user_agents(driver, args.user_agents_file)
                                LOG.info('User-Agent rotation enabled: %d agents, every %d URL(s)',
                                         len(user_agents), args.ua_rotate_every)
                            except Exception as exc:
                                LOG.warning('User-Agent rotation disabled: %s', exc)
                                rotation_enabled = False
                    except Exception as exc:
                        record(line_no, "failed", url, detail=f"Chrome startup: {exc}")
                        LOG.error("Cannot start Chrome; remaining URLs were not attempted")
                        break

                # Change only between URLs, not for retries or after a 429 response.
                if rotation_enabled and urls_attempted % args.ua_rotate_every == 0:
                    try:
                        previous_agent = rotate_user_agent(driver, user_agents, previous_agent)
                        LOG.info('User-Agent for line=%d: %s', line_no, previous_agent)
                    except Exception as exc:
                        LOG.warning('User-Agent override failed; disabling rotation: %s', exc)
                        rotation_enabled = False
                urls_attempted += 1

                error = ""
                downloaded = False
                for attempt in range(1, args.retries + 1):
                    try:
                        # A previous timed-out attempt may have finished meanwhile.
                        previous = existing_image(folder, basename)
                        path = previous or download_image(driver, url, folder,
                                                          basename, suffix, args.timeout)
                        record(line_no, "success", url, path.name,
                               f"{path.stat().st_size} bytes")
                        downloaded = True
                        break
                    except Exception as exc:
                        error = f"attempt {attempt}/{args.retries}: {exc}"
                        LOG.warning("%s (%s)", error, url)
                        if isinstance(exc, DownloadError) and exc.status == 429:
                            record(line_no, "failed", url, detail="HTTP 429: rate limited; stopped")
                            return  # Respect server rate limit; do not rotate identities.
                        if isinstance(exc, DownloadError) and 400 <= exc.status < 500:
                            break  # 404 / 403 etc. are not transient errors.
                        if attempt < args.retries:
                            time.sleep(2 * attempt)
                if not downloaded:
                    record(line_no, "failed", url, detail=error)
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception as exc:
                    LOG.warning("Chrome could not be closed cleanly: %s", exc)
            LOG.info("TOTAL: %s", results)


if __name__ == "__main__":
    main()
