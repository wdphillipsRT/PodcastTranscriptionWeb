import os
import subprocess
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparse
from typing import List, Dict, Optional, Any
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
from urllib.parse import urlparse, urljoin
import hashlib

logger = logging.getLogger(__name__)

def list_audio_items(feed_url: str) -> List[Dict[str, Any]]:
    """Parse RSS feed and extract audio episodes."""
    logger.info(f"Parsing RSS feed: {feed_url}")
    
    try:
        # Parse the feed
        feed = feedparser.parse(feed_url)
        
        if feed.bozo and feed.bozo_exception:
            logger.warning(f"Feed parsing warning: {feed.bozo_exception}")
        
        if not hasattr(feed, 'entries') or not feed.entries:
            logger.error("No entries found in feed")
            return []
        
        items = []
        
        for entry in feed.entries:
            # Parse publication date
            published = None
            if hasattr(entry, "published"):
                try:
                    published = dtparse.parse(entry.published)
                    # Ensure timezone awareness
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                except Exception as e:
                    logger.debug(f"Could not parse published date '{entry.published}': {e}")
            
            # Extract audio enclosures
            enclosures = getattr(entry, "enclosures", [])
            
            for enclosure in enclosures:
                enc_type = enclosure.get("type", "").lower()
                href = enclosure.get("href")
                
                if href and enc_type.startswith("audio"):
                    # Generate a unique ID for this episode
                    episode_id = entry.get("guid") or entry.get("id") or href
                    
                    episode_item = {
                        "title": entry.get("title", "Untitled Episode"),
                        "link": entry.get("link"),
                        "published": published,
                        "audio_url": href,
                        "guid": episode_id,
                        "description": entry.get("summary", ""),
                        "duration": enclosure.get("length"),
                        "file_size": enclosure.get("length"),  # Some feeds use length for file size
                    }
                    
                    items.append(episode_item)
                    break  # Take the first audio enclosure
        
        # Sort by publication date (newest first)
        items.sort(
            key=lambda x: x["published"] or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True
        )
        
        logger.info(f"Found {len(items)} audio episodes in feed")
        return items
        
    except Exception as e:
        logger.error(f"Error parsing RSS feed: {e}")
        raise

def filter_items(items: List[Dict[str, Any]], last_n: Optional[int] = None, days: Optional[int] = None) -> List[Dict[str, Any]]:
    """Filter episodes by count or date range."""
    original_count = len(items)
    
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items = [item for item in items if item["published"] and item["published"] >= cutoff]
        logger.info(f"Filtered to {len(items)} episodes from last {days} days")
    
    if last_n is not None:
        items = items[:last_n]
        logger.info(f"Limited to last {last_n} episodes")
    
    logger.info(f"Filtered {original_count} episodes down to {len(items)}")
    return items

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def download_audio_with_aria2(audio_url: str, dest_path: str) -> str:
    """Download audio file using aria2c for robustness."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    # Check if aria2c is available
    try:
        subprocess.run(["aria2c", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.warning("aria2c not found, falling back to requests")
        return download_audio_with_requests(audio_url, dest_path)
    
    logger.info(f"Downloading with aria2c: {audio_url}")
    
    try:
        cmd = [
            "aria2c",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "-x", "8",  # 8 connections
            "-s", "8",  # 8 splits
            "-o", os.path.basename(dest_path),
            "--user-agent", "Mozilla/5.0 (compatible; PodcastTranscriptionBot/1.0)",
            audio_url
        ]
        
        result = subprocess.run(
            cmd,
            cwd=os.path.dirname(dest_path),
            check=True,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0 and os.path.exists(dest_path):
            file_size = os.path.getsize(dest_path)
            logger.info(f"Successfully downloaded {file_size} bytes to {dest_path}")
            return dest_path
        else:
            raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
            
    except subprocess.CalledProcessError as e:
        logger.error(f"aria2c download failed: {e.stderr}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def download_audio_with_requests(audio_url: str, dest_path: str) -> str:
    """Download audio file using requests as fallback."""
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    
    logger.info(f"Downloading with requests: {audio_url}")
    
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; PodcastTranscriptionBot/1.0)"
        }
        
        with requests.get(audio_url, headers=headers, stream=True, timeout=60) as response:
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            with open(dest_path, 'wb') as f:
                downloaded = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if total_size > 0:
                            progress = (downloaded / total_size) * 100
                            if downloaded % (1024 * 1024) == 0:  # Log every MB
                                logger.debug(f"Download progress: {progress:.1f}%")
            
            file_size = os.path.getsize(dest_path)
            logger.info(f"Successfully downloaded {file_size} bytes to {dest_path}")
            return dest_path
            
    except Exception as e:
        logger.error(f"Requests download failed: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise

def download_audio(audio_url: str, dest_path: str) -> str:
    """
    Download audio file using the best available method.
    Prefers aria2c for robustness, falls back to requests.
    """
    if os.path.exists(dest_path):
        file_size = os.path.getsize(dest_path)
        if file_size > 0:
            logger.info(f"Audio file already exists: {dest_path} ({file_size} bytes)")
            return dest_path
        else:
            logger.warning(f"Removing empty file: {dest_path}")
            os.remove(dest_path)
    
    try:
        return download_audio_with_aria2(audio_url, dest_path)
    except Exception as e:
        logger.warning(f"aria2c download failed, trying requests: {e}")
        return download_audio_with_requests(audio_url, dest_path)

def generate_episode_id(guid: str, audio_url: str) -> str:
    """Generate a short, filesystem-safe episode ID."""
    source = guid or audio_url
    return hashlib.md5(source.encode("utf-8")).hexdigest()[:12]

def slugify(text: str) -> str:
    """Convert text to filesystem-safe slug."""
    import re
    # Remove/replace problematic characters
    slug = re.sub(r'[^\w\s-]', '', text.lower())
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug.strip('-')[:50]  # Limit length