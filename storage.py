import os
import sqlite3
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

import os

# Use /tmp for file storage on Heroku (ephemeral filesystem)
if os.environ.get('DYNO'):  # We're on Heroku
    BASE_DATA_PATH = "/tmp/data"
    DB_PATH = os.environ.get('DATABASE_URL')  # Use PostgreSQL instead of SQLite
else:
    BASE_DATA_PATH = "data"
    DB_PATH = "data/meta.db"

class DatabaseManager:
    """Manages SQLite database for episode processing cache."""
    
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.ensure_database()
    
    def ensure_database(self):
        """Create database and tables if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    podcast TEXT NOT NULL,
                    guid TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    title TEXT,
                    published TEXT,
                    audio_url TEXT,
                    audio_hash TEXT,
                    audio_path TEXT,
                    transcript_path TEXT,
                    summary_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    PRIMARY KEY (podcast, guid)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    podcast TEXT NOT NULL,
                    episodes_processed INTEGER DEFAULT 0,
                    episodes_failed INTEGER DEFAULT 0,
                    total_duration_seconds REAL DEFAULT 0,
                    processing_time_seconds REAL DEFAULT 0,
                    provider TEXT,
                    model TEXT
                )
            """)
            
            # Create indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_podcast ON episodes(podcast)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_created ON episodes(created_at)")
            
            conn.commit()
    
    def get_connection(self) -> sqlite3.Connection:
        """Get database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def episode_exists(self, podcast: str, guid: str) -> bool:
        """Check if episode already exists in database."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM episodes WHERE podcast = ? AND guid = ?",
                (podcast, guid)
            )
            return cursor.fetchone() is not None
    
    def is_episode_processed(self, podcast: str, guid: str) -> bool:
        """Check if episode has been fully processed."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                """SELECT transcript_path, summary_path, status 
                   FROM episodes 
                   WHERE podcast = ? AND guid = ?""",
                (podcast, guid)
            )
            row = cursor.fetchone()
            
            if not row:
                return False
            
            # Check if both paths exist and files are present
            if row['status'] == 'completed' and row['transcript_path'] and row['summary_path']:
                return (os.path.exists(row['transcript_path']) and 
                        os.path.exists(row['summary_path']))
            
            return False
    
    def insert_episode(self, podcast: str, episode_data: Dict[str, Any]) -> str:
        """Insert new episode record."""
        episode_id = generate_episode_id(episode_data.get('guid', ''), episode_data.get('audio_url', ''))
        now = datetime.now(timezone.utc).isoformat()
        
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO episodes (
                    podcast, guid, episode_id, title, published, audio_url,
                    audio_hash, audio_path, transcript_path, summary_path,
                    created_at, updated_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                podcast,
                episode_data.get('guid'),
                episode_id,
                episode_data.get('title'),
                episode_data.get('published').isoformat() if episode_data.get('published') else None,
                episode_data.get('audio_url'),
                episode_data.get('audio_hash'),
                episode_data.get('audio_path'),
                episode_data.get('transcript_path'),
                episode_data.get('summary_path'),
                now,
                now,
                'pending'
            ))
            conn.commit()
        
        return episode_id
    
    def update_episode_status(self, podcast: str, guid: str, status: str, error_message: str = None):
        """Update episode processing status."""
        now = datetime.now(timezone.utc).isoformat()
        
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE episodes 
                SET status = ?, error_message = ?, updated_at = ?
                WHERE podcast = ? AND guid = ?
            """, (status, error_message, now, podcast, guid))
            conn.commit()
    
    def update_episode_paths(self, podcast: str, guid: str, transcript_path: str = None, 
                           summary_path: str = None, audio_hash: str = None):
        """Update file paths for an episode."""
        now = datetime.now(timezone.utc).isoformat()
        
        updates = ["updated_at = ?"]
        values = [now]
        
        if transcript_path:
            updates.append("transcript_path = ?")
            values.append(transcript_path)
        
        if summary_path:
            updates.append("summary_path = ?")
            values.append(summary_path)
        
        if audio_hash:
            updates.append("audio_hash = ?")
            values.append(audio_hash)
        
        values.extend([podcast, guid])
        
        with self.get_connection() as conn:
            conn.execute(f"""
                UPDATE episodes 
                SET {', '.join(updates)}
                WHERE podcast = ? AND guid = ?
            """, values)
            conn.commit()
    
    def get_episode(self, podcast: str, guid: str) -> Optional[Dict[str, Any]]:
        """Get episode record."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM episodes WHERE podcast = ? AND guid = ?",
                (podcast, guid)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_episodes_by_podcast(self, podcast: str) -> List[Dict[str, Any]]:
        """Get all episodes for a podcast."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM episodes WHERE podcast = ? ORDER BY created_at DESC",
                (podcast,)
            )
            return [dict(row) for row in cursor.fetchall()]
    
    def get_processing_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """Get processing statistics for the last N days."""
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM processing_stats 
                WHERE date(run_date) >= date('now', '-{} days')
                ORDER BY run_date DESC
            """.format(days))
            return [dict(row) for row in cursor.fetchall()]
    
    def record_processing_stats(self, run_date: str, podcast: str, stats: Dict[str, Any]):
        """Record processing statistics for a run."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO processing_stats (
                    run_date, podcast, episodes_processed, episodes_failed,
                    total_duration_seconds, processing_time_seconds, provider, model
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_date,
                podcast,
                stats.get('episodes_processed', 0),
                stats.get('episodes_failed', 0),
                stats.get('total_duration_seconds', 0),
                stats.get('processing_time_seconds', 0),
                stats.get('provider'),
                stats.get('model')
            ))
            conn.commit()

def slugify(text: str) -> str:
    """Convert text to filesystem-safe slug."""
    import re
    # Remove/replace problematic characters
    slug = re.sub(r'[^\w\s-]', '', text.lower())
    slug = re.sub(r'[-\s]+', '-', slug)
    return slug.strip('-')[:50]  # Limit length

def generate_episode_id(guid: str, audio_url: str) -> str:
    """Generate a short, filesystem-safe episode ID."""
    source = guid or audio_url
    return hashlib.md5(source.encode("utf-8")).hexdigest()[:12]

def file_hash(file_path: str) -> str:
    """Calculate SHA-256 hash of a file."""
    hash_sha256 = hashlib.sha256()
    
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):  # 1MB chunks
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()
    except Exception as e:
        logger.error(f"Error calculating hash for {file_path}: {e}")
        raise

def get_file_paths(podcast_name: str, episode_id: str) -> Dict[str, str]:
    """Generate standardized file paths for an episode."""
    podcast_slug = slugify(podcast_name)
    
    # Use BASE_DATA_PATH which adapts to Heroku's /tmp filesystem
    base_paths = {
        'raw_dir': f"{BASE_DATA_PATH}/raw/{podcast_slug}/{episode_id}",
        'transcript_dir': f"{BASE_DATA_PATH}/transcripts/{podcast_slug}/{episode_id}",
        'summary_dir': f"{BASE_DATA_PATH}/summaries/{podcast_slug}/{episode_id}"
    }
    
    return {
        **base_paths,
        'audio_path': f"{base_paths['raw_dir']}/audio.mp3",
        'transcript_path': f"{base_paths['transcript_dir']}/transcript.txt",
        'segments_path': f"{base_paths['transcript_dir']}/segments.json",
        'summary_path': f"{base_paths['summary_dir']}/summary.md",
        'episode_metadata_path': f"{base_paths['summary_dir']}/episode.json"
    }

def save_episode_metadata(metadata_path: str, episode_data: Dict[str, Any], 
                         processing_info: Dict[str, Any] = None):
    """Save episode metadata to JSON file."""
    os.makedirs(os.path.dirname(metadata_path), exist_ok=True)
    
    metadata = {
        'episode': episode_data,
        'processing': processing_info or {},
        'generated_at': datetime.now(timezone.utc).isoformat()
    }
    
    try:
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
        
        logger.info(f"Episode metadata saved to: {metadata_path}")
        
    except Exception as e:
        logger.error(f"Failed to save episode metadata to {metadata_path}: {e}")
        raise

def load_episode_metadata(metadata_path: str) -> Dict[str, Any]:
    """Load episode metadata from JSON file."""
    try:
        with open(metadata_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load episode metadata from {metadata_path}: {e}")
        raise

def cleanup_old_files(days: int = 30, dry_run: bool = True) -> Dict[str, int]:
    """
    Clean up old episode files.
    
    Args:
        days: Remove files older than this many days
        dry_run: If True, only report what would be deleted
    
    Returns:
        Dictionary with cleanup statistics
    """
    cutoff_date = datetime.now() - timedelta(days=days)
    stats = {'files_found': 0, 'files_deleted': 0, 'space_freed': 0}
    
    data_dir = Path("data")
    if not data_dir.exists():
        return stats
    
    for file_path in data_dir.rglob("*"):
        if file_path.is_file():
            file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
            
            if file_mtime < cutoff_date:
                stats['files_found'] += 1
                file_size = file_path.stat().st_size
                
                if not dry_run:
                    try:
                        file_path.unlink()
                        stats['files_deleted'] += 1
                        stats['space_freed'] += file_size
                        logger.info(f"Deleted old file: {file_path}")
                    except Exception as e:
                        logger.error(f"Failed to delete {file_path}: {e}")
                else:
                    stats['space_freed'] += file_size
                    logger.info(f"Would delete: {file_path}")
    
    return stats