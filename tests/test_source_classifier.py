import pytest

from source_classifier import classify_source_url


@pytest.mark.parametrize("url", [
    "https://podcasts.apple.com/us/podcast/example/id123456789",
    "https://itunes.apple.com/podcast/id123456789?mt=2",
    "https://feeds.example/podcast.xml", "https://example.org/show/",
])
def test_podcast_sources(url):
    assert classify_source_url(url) == "podcast_rss"


@pytest.mark.parametrize("url", ["", "file:///etc/passwd", "ftp://example.org/feed",
    "https://", "https://user:secret@example.org/feed", "not a url",
    "https://example.org:invalid/feed", "https://example.org/\nfeed"])
def test_invalid_sources_fail_without_echoing_input(url):
    with pytest.raises(ValueError) as error:
        classify_source_url(url)
    assert "secret" not in str(error.value)


def test_apple_host_is_not_matched_by_substring():
    assert classify_source_url("https://podcasts.apple.com.attacker.example/feed") == "podcast_rss"
