import re
import requests
from bs4 import BeautifulSoup
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential
import logging

logger = logging.getLogger(__name__)

APPLE_ID_RE = re.compile(r'id(\d{6,})')

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def resolve_from_apple(url: str) -> Optional[str]:
    """Resolve Apple Podcasts URL to RSS feed URL using iTunes Lookup API."""
    m = APPLE_ID_RE.search(url)
    if not m:
        return None
    
    itunes_id = m.group(1)
    logger.info(f"Resolving Apple Podcasts ID: {itunes_id}")
    
    try:
        response = requests.get(
            "https://itunes.apple.com/lookup", 
            params={"id": itunes_id}, 
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PodcastTranscriptionBot/1.0)"}
        )
        response.raise_for_status()
        
        data = response.json()
        results = data.get("results", [])
        
        if results:
            feed_url = results[0].get("feedUrl")
            if feed_url:
                logger.info(f"Found RSS feed: {feed_url}")
                return feed_url
        
        logger.warning(f"No feed URL found for iTunes ID: {itunes_id}")
        return None
        
    except Exception as e:
        logger.error(f"Error resolving Apple Podcasts URL: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def discover_feed(url: str) -> Optional[str]:
    """Discover RSS feed from a generic website URL."""
    logger.info(f"Attempting feed discovery for: {url}")
    
    # First, try the URL directly as it might already be an RSS feed
    try:
        head_response = requests.head(
            url, 
            allow_redirects=True, 
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PodcastTranscriptionBot/1.0)"}
        )
        content_type = head_response.headers.get("Content-Type", "").lower()
        
        if any(feed_type in content_type for feed_type in ["xml", "rss", "atom"]):
            logger.info(f"URL is already a feed: {head_response.url}")
            return head_response.url
            
    except Exception as e:
        logger.debug(f"HEAD request failed, continuing with HTML discovery: {e}")
    
    # Try HTML discovery
    try:
        response = requests.get(
            url, 
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PodcastTranscriptionBot/1.0)"}
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Look for feed links in HTML
        for link in soup.find_all("link", rel="alternate"):
            link_type = (link.get("type") or "").lower()
            href = link.get("href")
            
            if href and any(feed_type in link_type for feed_type in ["rss", "atom", "xml", "json"]):
                feed_url = requests.compat.urljoin(response.url, href)
                logger.info(f"Found feed link in HTML: {feed_url}")
                return feed_url
        
        # Fallback: try common feed paths
        common_paths = ["/feed", "/rss", "/podcast.xml", "/feed.xml", "/rss.xml", "/feeds/all.rss.xml"]
        
        for path in common_paths:
            test_url = requests.compat.urljoin(response.url, path)
            
            try:
                test_response = requests.head(
                    test_url, 
                    allow_redirects=True, 
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; PodcastTranscriptionBot/1.0)"}
                )
                
                if test_response.status_code < 400:
                    content_type = test_response.headers.get("Content-Type", "").lower()
                    if any(feed_type in content_type for feed_type in ["xml", "rss", "atom"]):
                        logger.info(f"Found feed at common path: {test_response.url}")
                        return test_response.url
                        
            except Exception:
                continue
        
        logger.warning(f"No RSS feed found for URL: {url}")
        return None
        
    except Exception as e:
        logger.error(f"Error during feed discovery: {e}")
        raise

def resolve_feed(url: str) -> Optional[str]:
    """
    Resolve any URL to an RSS feed URL.
    Handles Apple Podcasts URLs and generic website URLs.
    """
    if not url or not url.strip():
        return None
    
    url = url.strip()
    
    # Try Apple Podcasts resolution first
    if "podcasts.apple.com" in url or "itunes.apple.com" in url:
        apple_feed = resolve_from_apple(url)
        if apple_feed:
            return apple_feed
    
    # Try generic feed discovery
    return discover_feed(url)