"""Public YouTube acquisition through a pinned, credential-free yt-dlp process.

No provider media URLs or diagnostics cross this boundary. Each operation owns
a private directory; successful downloads remain there for the caller to remove.
Only the subprocess boundary performs network I/O.
"""

from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from threading import Thread
from urllib.parse import urlsplit

from ingestion_types import DownloadedMedia, IngestionError


MAX_CAPTURE_BYTES = 64 * 1024
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_LANGUAGE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_FORMAT = re.compile(r"[A-Za-z0-9]{1,16}\Z")
_PREFERENCE = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}(?:\.\*)?\Z")
_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})


class YouTubeMediaError(IngestionError):
    """Only a stable category is exposed; upstream details are discarded."""

    def __init__(self, category: str):
        self.category = category
        self.retryable = category in {"timeout", "download_failed", "process_unavailable"}
        super().__init__(category)


def _fail(category):
    raise YouTubeMediaError(category) from None


@dataclass(frozen=True)
class CaptionCandidate:
    kind: str
    language: str
    format: str = "vtt"


@dataclass(frozen=True)
class CaptionInventory:
    video_id: str
    candidates: tuple[CaptionCandidate, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "candidates", tuple(self.candidates))


def _caption(candidate):
    if (not isinstance(candidate, CaptionCandidate) or not isinstance(candidate.kind, str)
            or candidate.kind not in {"creator", "automatic"}
            or not isinstance(candidate.language, str) or not _LANGUAGE.fullmatch(candidate.language)
            or candidate.format != "vtt"):
        _fail("invalid_caption")
    return candidate


def select_caption(inventory, preferred_languages=("en.*", "en")):
    """Creator origin wins first, then language regex order, then lexical order.

Patterns are restricted to literal language tags with an optional trailing .*,
matching yt-dlp's language regex semantics without accepting arbitrary regexes.
Unmatched languages remain usable and sort after preferred languages.
"""
    if (not isinstance(inventory, CaptionInventory) or not isinstance(preferred_languages, (tuple, list))
            or any(not isinstance(value, str) or not _PREFERENCE.fullmatch(value) for value in preferred_languages)):
        _fail("invalid_caption")
    candidates = tuple(_caption(candidate) for candidate in inventory.candidates)

    def order(candidate):
        priority = next((i for i, pattern in enumerate(preferred_languages)
                         if re.fullmatch(pattern, candidate.language)), len(preferred_languages))
        return (candidate.kind == "automatic", priority, candidate.language)

    return min(candidates, key=order) if candidates else None


def _canonical_video(value):
    # Parse raw input, before URL helpers can discard fragments/control bytes or
    # normalize duplicate/encoded selectors into something apparently harmless.
    if (not isinstance(value, str) or not value or value.startswith("-")
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail("invalid_url")
    if _VIDEO.fullmatch(value):
        identity = value
    else:
        try:
            parts = urlsplit(value)
            if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                    or parts.port not in {None, 443} or "#" in value or "%" in value or "\\" in value):
                _fail("invalid_url")
            if parts.hostname in _HOSTS and parts.path == "/watch":
                if not re.fullmatch(r"v=[A-Za-z0-9_-]{11}", parts.query):
                    _fail("invalid_url")
                identity = parts.query[2:]
            elif (parts.hostname in _HOSTS and parts.path.startswith(("/shorts/", "/embed/"))
                  and not parts.query and "?" not in value):
                identity = parts.path.split("/", 2)[2]
            elif parts.hostname in {"youtu.be", "www.youtu.be"} and not parts.query and "?" not in value:
                identity = parts.path[1:]
            else:
                _fail("invalid_url")
        except (ValueError, TypeError, UnicodeError):
            _fail("invalid_url")
        if not _VIDEO.fullmatch(identity):
            _fail("invalid_url")
    return identity, "https://www.youtube.com/watch?v=" + identity


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_inventory")
        result[key] = value
    return result


def _cleanup(directory):
    try:
        if directory.is_symlink():
            directory.unlink()
        else:
            shutil.rmtree(directory)
    except OSError:
        _fail("cleanup_failed")


class YouTubeMediaClient:
    def __init__(self, settings):
        for name in ("YTDLP_TIMEOUT_SECONDS", "MAX_AUDIO_BYTES", "YOUTUBE_CAPTION_MAX_BYTES", "RSS_MAX_BYTES"):
            if type(getattr(settings, name, None)) is not int or getattr(settings, name) <= 0:
                _fail("invalid_configuration")
        expected = getattr(settings, "YTDLP_EXPECTED_VERSION", None)
        if not isinstance(expected, str) or not re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}", expected):
            _fail("invalid_configuration")
        self._settings = settings

    def __repr__(self):
        return "<YouTubeMediaClient public-only>"

    def select_caption(self, inventory, preferred_languages=None):
        if preferred_languages is None:
            preferred_languages = self._settings.YOUTUBE_PREFERRED_CAPTION_LANGUAGES
        return select_caption(inventory, preferred_languages)

    def _check_version(self):
        try:
            installed = version("yt-dlp")
            if tuple(map(int, installed.split("."))) != tuple(map(int, self._settings.YTDLP_EXPECTED_VERSION.split("."))):
                _fail("version_mismatch")
        except (PackageNotFoundError, ValueError, TypeError):
            _fail("version_mismatch")

    @staticmethod
    def _directory(directory=None):
        try:
            if directory is not None:
                parent = Path(directory).absolute()
                if not parent.is_dir() or parent.is_symlink() or parent.resolve() != parent:
                    _fail("invalid_directory")
            else:
                parent = None
            return Path(tempfile.mkdtemp(prefix="youtube-media-", dir=parent))
        except (OSError, ValueError, TypeError):
            _fail("invalid_directory")

    def _run(self, canonical, options, directory, *, machine_output=False):
        # -I excludes current directory/user-site/PYTHONPATH from module loading.
        # The environment is an allowlist: no proxies, account tokens or configs.
        args = [sys.executable, "-I", "-m", "yt_dlp", "--no-config", "--no-plugin-dirs",
                "--no-playlist", "--no-remote-components", "--js-runtimes", "deno",
                "--no-update", "--no-cache-dir", "--no-cookies", "--no-cookies-from-browser",
                "--no-progress", "--output", "media.%(ext)s", *options, "--", canonical]
        # Deno still writes an analysis cache with --no-code-cache. Keep runtime
        # files in a disposable child, separate from the one permitted output.
        runtime = directory / ".runtime"
        environment = {"PATH": str(Path(sys.executable).parent) + ":/usr/local/bin:/usr/bin:/bin",
                       "HOME": str(runtime), "XDG_CONFIG_HOME": str(runtime),
                       "XDG_CACHE_HOME": str(runtime), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        captured = bytearray()
        maximum = self._settings.RSS_MAX_BYTES if machine_output else MAX_CAPTURE_BYTES
        overflow = []
        read_errors = []

        def stop():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    process.kill()
                except OSError:
                    read_errors.append(True)

        def drain(stream, retain):
            try:
                while chunk := stream.read(8192):
                    if retain:
                        available = maximum - len(captured)
                        captured.extend(chunk[:available])
                        if len(chunk) > available and not overflow:
                            overflow.append(True)
                            captured.clear()
                            stop()
                            return
            except (OSError, ValueError):
                read_errors.append(True)

        try:
            process = subprocess.Popen(args, shell=False, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       cwd=directory, env=environment, start_new_session=True, bufsize=0)
        except (OSError, ValueError, subprocess.SubprocessError):
            _fail("process_unavailable")
        readers = [Thread(target=drain, args=(process.stdout, True), daemon=True),
                   Thread(target=drain, args=(process.stderr, False), daemon=True)]
        timed_out = False
        failure = False
        try:
            for reader in readers:
                reader.start()
            try:
                code = process.wait(timeout=self._settings.YTDLP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
            else:
                failure = bool(code)
        except (OSError, ValueError, subprocess.SubprocessError):
            failure = True
        finally:
            # Reap descendants too: FFmpeg must not survive an expired deadline
            # or hold a capture pipe open after its parent has exited.
            if not overflow:
                stop()
            if timed_out or failure:
                try:
                    process.wait(timeout=1)
                except (OSError, subprocess.SubprocessError):
                    failure = True
            for reader in readers:
                reader.join(timeout=1)
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    failure = True
            if directory.is_symlink() or directory.resolve() != directory:
                _fail("invalid_output")
            if runtime.exists() or runtime.is_symlink():
                _cleanup(runtime)
        if overflow:
            captured.clear()
            _fail("inventory_too_large" if machine_output else "output_too_large")
        if timed_out:
            captured.clear()
            _fail("timeout")
        if failure:
            captured.clear()
            _fail("download_failed")
        if read_errors or any(reader.is_alive() for reader in readers):
            captured.clear()
            _fail("download_failed")
        return bytes(captured)

    def inventory(self, video_url):
        identity, canonical = _canonical_video(video_url)
        self._check_version()
        directory = self._directory()
        try:
            raw = self._run(canonical, ["--dump-single-json", "--skip-download"], directory, machine_output=True)
            try:
                data = json.loads(raw, object_pairs_hook=_unique_json,
                                  parse_constant=lambda value: _fail("invalid_inventory"))
            except (ValueError, UnicodeError, RecursionError):
                _fail("invalid_inventory")
            if (not isinstance(data, dict) or data.get("id") != identity
                    or data.get("_type", "video") != "video"):
                _fail("invalid_inventory")
            if any(data.get(field) is not None and not isinstance(data[field], str)
                   for field in ("availability", "live_status")):
                _fail("invalid_inventory")
            if type(data.get("age_limit", 0)) not in {int, float}:
                _fail("invalid_inventory")
            if (data.get("availability") != "public"
                    or data.get("live_status") not in {None, "not_live", "was_live"}
                    or data.get("age_limit", 0) != 0):
                _fail("unavailable")
            candidates = []
            for field, kind in (("subtitles", "creator"), ("automatic_captions", "automatic")):
                languages = data.get(field, {})
                if not isinstance(languages, dict):
                    _fail("invalid_inventory")
                for language, formats in sorted(languages.items()):
                    # Completed public streams may expose chat replay here.
                    # It is a reserved pseudo-track, not a caption language.
                    if language == "live_chat":
                        continue
                    if not _LANGUAGE.fullmatch(language) or not isinstance(formats, list):
                        _fail("invalid_inventory")
                    for format_ in formats:
                        if (not isinstance(format_, dict) or not isinstance(format_.get("ext"), str)
                                or not _FORMAT.fullmatch(format_["ext"])):
                            _fail("invalid_inventory")
                    if any(format_["ext"] == "vtt" for format_ in formats):
                        candidates.append(CaptionCandidate(kind, language))
            return CaptionInventory(identity, tuple(candidates))
        finally:
            _cleanup(directory)

    @staticmethod
    def _output(directory, name, maximum):
        # No glob-selected arbitrary names, nested files, symlinks or partials.
        # O_NOFOLLOW plus fstat protects the final open from symlink replacement.
        try:
            if directory.is_symlink() or directory.resolve() != directory:
                _fail("invalid_output")
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.listdir(directory_fd) != [name]:
                    _fail("invalid_output")
                with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd), "rb") as file_:
                    metadata = os.fstat(file_.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        _fail("invalid_output")
                    if not 0 < metadata.st_size <= maximum:
                        _fail("output_too_large" if metadata.st_size > maximum else "invalid_output")
                    digest = sha256()
                    total = 0
                    while chunk := file_.read(min(65536, maximum - total + 1)):
                        total += len(chunk)
                        if total > maximum:
                            _fail("output_too_large")
                        digest.update(chunk)
                    if total != metadata.st_size:
                        _fail("invalid_output")
                return directory / name, total, digest.hexdigest()
            finally:
                os.close(directory_fd)
        except YouTubeMediaError:
            raise
        except (OSError, ValueError):
            _fail("invalid_output")

    def _download(self, video_url, directory, options, name, maximum):
        _, canonical = _canonical_video(video_url)
        self._check_version()
        isolated = self._directory(directory)
        success = False
        try:
            self._run(canonical, options, isolated)
            result = self._output(isolated, name, maximum)
            success = True
            return result
        finally:
            if not success:
                _cleanup(isolated)

    def download_caption(self, video_url, candidate, directory):
        candidate = _caption(candidate)
        switches = (["--write-subs", "--no-write-auto-subs"] if candidate.kind == "creator"
                    else ["--no-write-subs", "--write-auto-subs"])
        # Upstream supports an 'all' alias and case-insensitive language regexes.
        # A scoped exact regex disables both expansions after tag validation.
        language = "(?-i:" + re.escape(candidate.language) + ")"
        options = ["--skip-download", *switches, "--sub-langs", language, "--sub-format", "vtt"]
        path, _, _ = self._download(video_url, directory, options, "media." + candidate.language + ".vtt",
                                    self._settings.YOUTUBE_CAPTION_MAX_BYTES)
        return path

    def extract_audio(self, video_url, directory):
        options = ["--format", "bestaudio/best", "--extract-audio", "--audio-format", "mp3",
                   "--max-filesize", str(self._settings.MAX_AUDIO_BYTES), "--no-write-subs", "--no-write-auto-subs"]
        path, size, digest = self._download(video_url, directory, options, "media.mp3", self._settings.MAX_AUDIO_BYTES)
        return DownloadedMedia(path, size, "audio/mpeg", digest)
