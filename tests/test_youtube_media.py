"""Offline safety contract: only the subprocess boundary is replaced."""

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
from unittest.mock import Mock

import pytest

from config import load_settings
from youtube_media import (CaptionCandidate, CaptionInventory, YouTubeMediaClient,
                           YouTubeMediaError, select_caption)


VIDEO = "dQw4w9WgXcQ"
URL = "https://www.youtube.com/watch?v=" + VIDEO
VTT = (Path(__file__).parent / "fixtures/youtube/captions.vtt").read_bytes()


@pytest.fixture
def settings(app_environment):
    return load_settings(app_environment)


@pytest.fixture
def process(monkeypatch):
    calls = []
    state = {"payload": {"id": VIDEO, "availability": "public", "subtitles": {}, "automatic_captions": {}},
             "stderr": b"", "code": 0, "action": None, "timeout": False}

    def start(args, **kwargs):
        calls.append((args, kwargs))
        if state["action"]:
            state["action"](Path(kwargs["cwd"]), args)
        stdout = state.get("stdout", json.dumps(state["payload"]).encode())
        child = Mock(pid=234567890, stdout=state.get("stdout_stream", BytesIO(stdout)), stderr=BytesIO(state["stderr"]))
        child.returncode = state["code"]
        if state["timeout"]:
            child.wait.side_effect = [subprocess.TimeoutExpired("secret URL token", 1), -9]
        else:
            child.wait.return_value = state["code"]
        state["child"] = child
        return child

    monkeypatch.setattr("youtube_media.subprocess.Popen", start)
    monkeypatch.setattr("youtube_media.os.killpg", Mock())
    return state, calls


def tracks(*languages):
    return {language: [{"ext": "vtt", "url": "https://signed.invalid/?token=SECRET"}] for language in languages}


def test_inventory_preserves_kind_and_discards_signed_urls(settings, process):
    state, _ = process
    state["payload"]["subtitles"] = tracks("fr", "en-US")
    state["payload"]["automatic_captions"] = tracks("en")
    inventory = YouTubeMediaClient(settings).inventory(URL)
    assert inventory.video_id == VIDEO
    assert inventory.candidates == (CaptionCandidate("creator", "en-US"), CaptionCandidate("creator", "fr"),
                                    CaptionCandidate("automatic", "en"))
    assert "SECRET" not in repr(inventory)


@pytest.mark.parametrize(("candidates", "preferred", "expected"), [
    ((CaptionCandidate("automatic", "en"), CaptionCandidate("creator", "fr")), ("en.*", "en"), CaptionCandidate("creator", "fr")),
    ((CaptionCandidate("creator", "en"), CaptionCandidate("creator", "en-US")), ("en.*", "en"), CaptionCandidate("creator", "en")),
    ((CaptionCandidate("creator", "en-US"), CaptionCandidate("creator", "en")), ("en", "en.*"), CaptionCandidate("creator", "en")),
    ((CaptionCandidate("creator", "fr"), CaptionCandidate("creator", "de")), ("en",), CaptionCandidate("creator", "de")),
    ((CaptionCandidate("automatic", "en"),), ("en",), CaptionCandidate("automatic", "en")),
    ((), ("en",), None),
])
def test_selection_priority(candidates, preferred, expected):
    assert select_caption(CaptionInventory(VIDEO, candidates), preferred) == expected


@pytest.mark.parametrize("value", [VIDEO, URL, "https://youtu.be/" + VIDEO, "https://m.youtube.com/shorts/" + VIDEO,
                                    "https://www.youtube.com/embed/" + VIDEO])
def test_canonicalizes_single_video(settings, process, value):
    YouTubeMediaClient(settings).inventory(value)
    assert process[1][0][0][-2:] == ["--", URL]


@pytest.mark.parametrize("value", [
    "--help", "-dQw4w9WgXc", VIDEO + " " + VIDEO, " " + VIDEO, URL + "#fragment", URL + "#",
    URL + "&list=PL1234567890", URL + "&v=" + VIDEO, URL + "&v=aaaaaaaaaaa", URL + "&token=secret",
    URL + "&t=12", URL + "&", URL + "&unknown=value", URL + "%26v=aaaaaaaaaaa",
    "https://user:secret@www.youtube.com/watch?v=" + VIDEO, "https://evil.invalid/watch?v=" + VIDEO,
    "http://www.youtube.com/watch?v=" + VIDEO, "https://www.youtube.com:444/watch?v=" + VIDEO,
    "https://www.youtube.com/playlist?list=PL1234567890", "https://youtu.be/" + VIDEO + "?v=" + VIDEO,
    "https://www.youtube.com/watch?%76=" + VIDEO, None, [VIDEO],
])
def test_rejects_ambiguous_or_secret_input_before_subprocess(settings, process, value):
    with pytest.raises(YouTubeMediaError, match="^invalid_url$"):
        YouTubeMediaClient(settings).inventory(value)
    assert process[1] == []


def assert_safety(args, kwargs, settings):
    assert isinstance(args, list) and all(isinstance(v, str) for v in args)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
    assert kwargs["start_new_session"] is True
    assert "--no-config" in args and "--no-plugin-dirs" in args and "--no-playlist" in args
    assert "--no-remote-components" in args and "--no-update" in args
    assert args[args.index("--js-runtimes") + 1] == "deno"
    assert args[args.index("--output") + 1] == "media.%(ext)s"
    assert args[-2:] == ["--", URL]
    assert not {"--cookies", "--cookies-from-browser", "--netrc", "--username", "--password", "--plugin-dirs",
                "--remote-components", "--update", "-U", "--exec"}.intersection(args)
    assert set(kwargs["env"]) == {"PATH", "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL"}
    assert kwargs["env"]["HOME"] == str(Path(kwargs["cwd"]) / ".runtime")


def test_inventory_command_is_safe_isolated_and_bounded(settings, process, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "https://user:SECRET@proxy.invalid")
    monkeypatch.setenv("YTDLP_CONFIG", "/secret/config")
    YouTubeMediaClient(settings).inventory(URL)
    args, kwargs = process[1][0]
    assert_safety(args, kwargs, settings)
    assert "--dump-single-json" in args and "--skip-download" in args
    assert process[0]["child"].wait.call_args_list[0].kwargs["timeout"] == settings.YTDLP_TIMEOUT_SECONDS
    assert not Path(kwargs["cwd"]).exists()


@pytest.mark.parametrize("kind", ["creator", "automatic"])
def test_downloads_exact_selected_vtt(settings, process, tmp_path, kind):
    process[0]["action"] = lambda cwd, args: (cwd / "media.en-US.vtt").write_bytes(VTT)
    path = YouTubeMediaClient(settings).download_caption(URL, CaptionCandidate(kind, "en-US"), tmp_path)
    assert path.read_bytes() == VTT
    assert path.parent.parent == tmp_path
    args, kwargs = process[1][0]
    assert_safety(args, kwargs, settings)
    assert args[args.index("--sub-langs") + 1] == r"(?-i:en\-US)"
    assert args[args.index("--sub-format") + 1] == "vtt"
    assert "--skip-download" in args
    assert ("--write-subs" in args) is (kind == "creator")
    assert ("--write-auto-subs" in args) is (kind == "automatic")
    assert ("--no-write-auto-subs" in args) is (kind == "creator")
    assert ("--no-write-subs" in args) is (kind == "automatic")


@pytest.mark.parametrize("candidate", [CaptionCandidate("creator", "--exec"), CaptionCandidate("automatic", "en,fr"),
                                       CaptionCandidate("creator", "../en"), CaptionCandidate("unknown", "en"),
                                       CaptionCandidate("creator", "en", "srt"), CaptionCandidate("creator", "en.*")])
def test_rejects_unsafe_caption_arguments(settings, process, tmp_path, candidate):
    with pytest.raises(YouTubeMediaError, match="^invalid_caption$"):
        YouTubeMediaClient(settings).download_caption(URL, candidate, tmp_path)
    assert process[1] == []


def test_extracts_audio_checks_hash_and_limits(settings, process, tmp_path):
    body = b"synthetic mp3 bytes"
    process[0]["action"] = lambda cwd, args: (cwd / "media.mp3").write_bytes(body)
    media = YouTubeMediaClient(settings).extract_audio(URL, tmp_path)
    assert media.path.read_bytes() == body
    assert media.size_bytes == len(body) and media.sha256 == sha256(body).hexdigest()
    assert media.content_type == "audio/mpeg"
    args, kwargs = process[1][0]
    assert_safety(args, kwargs, settings)
    assert args[args.index("--format") + 1] == "bestaudio/best"
    assert "--extract-audio" in args
    assert args[args.index("--audio-format") + 1] == "mp3"
    assert args[args.index("--max-filesize") + 1] == str(settings.MAX_AUDIO_BYTES)
    assert "--no-write-subs" in args and "--no-write-auto-subs" in args


@pytest.mark.parametrize("mode", ["caption", "audio"])
@pytest.mark.parametrize("failure", ["missing", "multiple", "symlink", "nested", "partial", "empty", "oversized",
                                     "hardlink", "fifo", "directory", "wrong_name", "partial_with_output"])
def test_output_validation_and_failure_cleanup(settings, process, tmp_path, mode, failure):
    settings = replace(settings, MAX_AUDIO_BYTES=32, YOUTUBE_CAPTION_MAX_BYTES=32)
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not touch")
    name = "media.en.vtt" if mode == "caption" else "media.mp3"

    def action(cwd, args):
        target = cwd / name
        if failure == "multiple":
            target.write_bytes(b"ok")
            (cwd / ("other.vtt" if mode == "caption" else "other.mp3")).write_bytes(b"ok")
        elif failure == "symlink":
            target.symlink_to(outside)
        elif failure == "hardlink":
            os.link(outside, target)
        elif failure == "fifo":
            os.mkfifo(target)
        elif failure == "directory":
            target.mkdir()
        elif failure == "wrong_name":
            (cwd / ("media.fr.vtt" if mode == "caption" else "media.webm")).write_bytes(b"wrong")
        elif failure == "partial_with_output":
            target.write_bytes(b"ok")
            (cwd / (name + ".part")).write_bytes(b"partial")
        elif failure == "nested":
            (cwd / "nested").mkdir()
            (cwd / "nested" / name).write_bytes(b"ok")
        elif failure == "partial":
            (cwd / (name + ".part")).write_bytes(b"partial")
        elif failure in {"empty", "oversized"}:
            target.write_bytes(b"" if failure == "empty" else b"x" * 33)

    process[0]["action"] = action
    with pytest.raises(YouTubeMediaError):
        client = YouTubeMediaClient(settings)
        if mode == "caption":
            client.download_caption(URL, CaptionCandidate("creator", "en"), tmp_path)
        else:
            client.extract_audio(URL, tmp_path)
    assert outside.read_bytes() == b"do not touch"
    assert list(tmp_path.iterdir()) == [outside]


@pytest.mark.parametrize("failure,category", [("timeout", "timeout"), ("nonzero", "download_failed"),
                                               ("malformed", "invalid_inventory"), ("large", "inventory_too_large")])
def test_failures_are_typed_redacted_and_cleanup(settings, process, failure, category):
    settings = replace(settings, RSS_MAX_BYTES=65536)
    state, calls = process
    state["stderr"] = (b"SECRET token https://user:password@host.invalid /local/secret " * 4096)
    if failure == "timeout":
        state["timeout"] = True
    elif failure == "nonzero":
        state["code"] = 1
    else:
        state["stdout"] = b"invalid SECRET" if failure == "malformed" else b"x" * 65537
    with pytest.raises(YouTubeMediaError) as error:
        YouTubeMediaClient(settings).inventory(URL)
    assert str(error.value) == category and error.value.category == category
    assert not any(secret in repr(error.value) for secret in ("SECRET", "password", "http", "/local", VIDEO))
    assert error.value.__suppress_context__
    assert not Path(calls[0][1]["cwd"]).exists()


@pytest.mark.parametrize("payload", [None, [], {}, {"id": "aaaaaaaaaaa"}, {"id": VIDEO, "_type": "playlist"},
                                     {"id": VIDEO, "subtitles": []}, {"id": VIDEO, "subtitles": {"--exec": []}},
                                     {"id": VIDEO, "subtitles": {"en": "not-tracks"}},
                                     {"id": VIDEO, "subtitles": {"en": [{"ext": "--exec"}]}},
                                     {"id": VIDEO, "availability": "private"}, {"id": VIDEO, "age_limit": 18},
                                     {"id": VIDEO, "live_status": "is_live"}])
def test_inventory_schema_and_restrictions(settings, process, payload):
    process[0]["payload"] = payload
    with pytest.raises(YouTubeMediaError):
        YouTubeMediaClient(settings).inventory(URL)


def test_ignores_non_vtt_tracks(settings, process):
    process[0]["payload"]["subtitles"] = {"en": [{"ext": "srt"}]}
    assert select_caption(YouTubeMediaClient(settings).inventory(URL)) is None


def test_pinned_version_mismatch_prevents_subprocess(settings, process, monkeypatch):
    monkeypatch.setattr("youtube_media.version", lambda package: "0.0.0")
    with pytest.raises(YouTubeMediaError, match="^version_mismatch$"):
        YouTubeMediaClient(settings).inventory(URL)
    assert not process[1]


@pytest.mark.parametrize("field", ["availability", "live_status"])
@pytest.mark.parametrize("value", [[], {}, 123, True])
def test_nested_restriction_values_are_typed(settings, process, field, value):
    process[0]["payload"][field] = value
    with pytest.raises(YouTubeMediaError, match="^invalid_inventory$"):
        YouTubeMediaClient(settings).inventory(URL)


@pytest.mark.parametrize("candidate", [CaptionCandidate([], "en"), CaptionCandidate("creator", None),
                                       CaptionCandidate("creator", "en", []), object()])
def test_malformed_candidate_values_are_typed(settings, process, tmp_path, candidate):
    with pytest.raises(YouTubeMediaError, match="^invalid_caption$"):
        YouTubeMediaClient(settings).download_caption(URL, candidate, tmp_path)
    assert not process[1]


@pytest.mark.parametrize("preferred", [[".*"], ["en|fr"], ["(en)+"], [None], "en", None])
def test_rejects_unsafe_language_patterns(preferred):
    with pytest.raises(YouTubeMediaError, match="^invalid_caption$"):
        select_caption(CaptionInventory(VIDEO, (CaptionCandidate("creator", "en"),)), preferred)


def test_timeout_reaps_process_group_with_bounded_wait(settings, process, monkeypatch):
    kill = Mock()
    monkeypatch.setattr("youtube_media.os.killpg", kill)
    process[0]["timeout"] = True
    with pytest.raises(YouTubeMediaError, match="^timeout$"):
        YouTubeMediaClient(settings).inventory(URL)
    kill.assert_called_once()
    assert all(call.kwargs.get("timeout", 0) > 0 for call in process[0]["child"].wait.call_args_list)


@pytest.mark.parametrize("mode", ["caption", "audio"])
def test_post_download_oversize_category_is_preserved(settings, process, tmp_path, mode):
    settings = replace(settings, MAX_AUDIO_BYTES=1, YOUTUBE_CAPTION_MAX_BYTES=1)
    name = "media.en.vtt" if mode == "caption" else "media.mp3"
    process[0]["action"] = lambda cwd, args: (cwd / name).write_bytes(b"xx")
    with pytest.raises(YouTubeMediaError, match="^output_too_large$"):
        client = YouTubeMediaClient(settings)
        if mode == "caption":
            client.download_caption(URL, CaptionCandidate("creator", "en"), tmp_path)
        else:
            client.extract_audio(URL, tmp_path)
    assert not list(tmp_path.iterdir())


def test_subprocess_start_errors_are_redacted(settings, process, monkeypatch):
    monkeypatch.setattr("youtube_media.subprocess.Popen", Mock(side_effect=OSError("SECRET /local/path https://invalid")))
    with pytest.raises(YouTubeMediaError, match="^process_unavailable$") as error:
        YouTubeMediaClient(settings).inventory(URL)
    assert "SECRET" not in repr(error.value)


def test_cleanup_errors_are_redacted(settings, process, monkeypatch):
    def remove(directory):
        # Really clean the temporary fixture before simulating a cleanup error.
        directory.rmdir()
        raise OSError("SECRET /local/path https://invalid")

    monkeypatch.setattr("youtube_media.shutil.rmtree", remove)
    with pytest.raises(YouTubeMediaError, match="^cleanup_failed$"):
        YouTubeMediaClient(settings).inventory(URL)


@pytest.mark.parametrize("raw", [b'{"id":"dQw4w9WgXcQ","id":"dQw4w9WgXcQ"}', b'{"id":"dQw4w9WgXcQ","age_limit":NaN}'])
def test_rejects_nonstandard_or_duplicate_json(settings, process, raw):
    process[0]["stdout"] = raw
    with pytest.raises(YouTubeMediaError, match="^invalid_inventory$"):
        YouTubeMediaClient(settings).inventory(URL)


def test_full_stderr_is_discarded_without_hiding_valid_inventory(settings, process):
    process[0]["stderr"] = b"SECRET" * 100_000
    assert YouTubeMediaClient(settings).inventory(URL).video_id == VIDEO


def test_client_selection_uses_settings(settings):
    settings = replace(settings, YOUTUBE_PREFERRED_CAPTION_LANGUAGES=("fr", "en"))
    inventory = CaptionInventory(VIDEO, (CaptionCandidate("creator", "en"), CaptionCandidate("creator", "fr")))
    assert YouTubeMediaClient(settings).select_caption(inventory).language == "fr"


def test_inventory_machine_output_can_exceed_diagnostic_limit(settings, process):
    process[0]["payload"]["description"] = "x" * 100_000
    process[0]["payload"]["subtitles"] = tracks("en")
    inventory = YouTubeMediaClient(settings).inventory(URL)
    assert select_caption(inventory) == CaptionCandidate("creator", "en")


def test_inventory_over_machine_limit_kills_without_reading_secret_tail(settings, process, monkeypatch):
    class TrackedStream(BytesIO):
        read_bytes = 0

        def read(self, count):
            body = super().read(count)
            self.read_bytes += len(body)
            return body

    stream = TrackedStream(b"x" * (settings.RSS_MAX_BYTES + 8192) + b"SECRET_TAIL_DO_NOT_READ")
    process[0]["stdout_stream"] = stream
    kill = Mock()
    monkeypatch.setattr("youtube_media.os.killpg", kill)
    with pytest.raises(YouTubeMediaError, match="^inventory_too_large$") as error:
        YouTubeMediaClient(settings).inventory(URL)
    assert stream.read_bytes <= settings.RSS_MAX_BYTES + 8192
    assert kill.called
    assert "SECRET" not in repr(error.value)


def test_inventory_respects_injected_machine_limit(settings, process):
    settings = replace(settings, RSS_MAX_BYTES=100)
    process[0]["stdout"] = b"x" * 101
    with pytest.raises(YouTubeMediaError, match="^inventory_too_large$"):
        YouTubeMediaClient(settings).inventory(URL)


@pytest.mark.parametrize("language,available", [("en-US", ["en-US", "EN-US", "en"]), ("all", ["all", "en", "fr"])])
def test_caption_language_uses_exact_case_sensitive_selection(settings, process, tmp_path, language, available):
    process[0]["action"] = lambda cwd, args: (cwd / ("media." + language + ".vtt")).write_bytes(VTT)
    YouTubeMediaClient(settings).download_caption(URL, CaptionCandidate("creator", language), tmp_path)
    args = process[1][0][0]
    pattern = args[args.index("--sub-langs") + 1]
    # The pinned upstream selector treats 'all' as an alias and compiles all
    # language regexes with re.I; neither behavior may expand our selection.
    assert pattern != "all"
    assert [value for value in available if re.fullmatch(pattern, value, re.I)] == [language]


def test_rejects_symlink_directory_escape(settings, process, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "media.en.vtt").write_bytes(VTT)

    def action(cwd, args):
        cwd.rmdir()
        cwd.symlink_to(outside, target_is_directory=True)

    process[0]["action"] = action
    with pytest.raises(YouTubeMediaError, match="^(invalid_output|cleanup_failed)$"):
        YouTubeMediaClient(settings).download_caption(URL, CaptionCandidate("creator", "en"), tmp_path)
    assert (outside / "media.en.vtt").read_bytes() == VTT


@pytest.mark.parametrize("method", ["caption", "audio"])
def test_download_errors_remove_partials(settings, process, tmp_path, method):
    process[0]["code"] = 1
    process[0]["action"] = lambda cwd, args: (cwd / "media.mp3.part").write_bytes(b"partial")
    with pytest.raises(YouTubeMediaError, match="^download_failed$"):
        client = YouTubeMediaClient(settings)
        if method == "caption":
            client.download_caption(URL, CaptionCandidate("creator", "en"), tmp_path)
        else:
            client.extract_audio(URL, tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("method", ["caption", "audio"])
def test_download_rejects_bad_url_before_subprocess(settings, process, tmp_path, method):
    with pytest.raises(YouTubeMediaError, match="^invalid_url$"):
        client = YouTubeMediaClient(settings)
        if method == "caption":
            client.download_caption(URL + "&token=SECRET", CaptionCandidate("creator", "en"), tmp_path)
        else:
            client.extract_audio(URL + "&token=SECRET", tmp_path)
    assert not process[1] and not list(tmp_path.iterdir())


def test_rejects_symlink_parent_before_subprocess(settings, process, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "link"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(YouTubeMediaError, match="^invalid_directory$"):
        YouTubeMediaClient(settings).extract_audio(URL, linked)
    assert not process[1] and not list(outside.iterdir())


def test_runtime_cache_does_not_pollute_download_output(settings, process, tmp_path):
    def action(cwd, args):
        runtime = Path(process[1][-1][1]["env"]["XDG_CACHE_HOME"])
        (runtime / "deno/gen").mkdir(parents=True)
        (runtime / "deno/gen/analysis.bin").write_bytes(b"local runtime cache")
        (cwd / "media.mp3").write_bytes(b"synthetic mp3")

    process[0]["action"] = action
    media = YouTubeMediaClient(settings).extract_audio(URL, tmp_path)
    assert media.path.read_bytes() == b"synthetic mp3"
    assert list(media.path.parent.iterdir()) == [media.path]
