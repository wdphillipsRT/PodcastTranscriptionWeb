#!/usr/bin/env python3
"""
Podcast Transcription Pipeline

A command-line tool to automatically download, transcribe, and summarize podcast episodes
from RSS feeds, including Apple Podcasts URLs.
"""

import os
import sys
import csv
import asyncio
import argparse
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

# Import our modules
from feed_resolver import resolve_feed
from episode_fetcher import (
    list_audio_items, filter_items, download_audio, 
    generate_episode_id, slugify
)
from transcribe import transcribe_audio, TranscriptionResult
from summarize import generate_and_save_summary, SummarizationError
from storage import (
    DatabaseManager, get_file_paths, file_hash, 
    save_episode_metadata
)

# Setup logging
def setup_logging(level: str = "INFO"):
    """Configure logging for the application."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    
    # Create logs directory
    os.makedirs("logs", exist_ok=True)
    
    # Configure logging
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f"logs/pipeline_{datetime.now().strftime('%Y%m%d')}.log"),
            logging.StreamHandler(sys.stdout)
        ]
    )

logger = logging.getLogger(__name__)

class PipelineConfig:
    """Configuration for the pipeline."""
    
    def __init__(self, args):
        self.csv_path = args.csv
        self.last_n = args.last_n
        self.days = args.days
        self.provider = args.provider
        self.model = args.model
        self.whisper_model = args.whisper_model or os.getenv("WHISPER_MODEL", "medium")
        self.force_reprocess = args.force
        self.dry_run = args.dry_run
        self.max_concurrent = args.max_concurrent
        self.skip_download = args.skip_download
        self.skip_transcription = args.skip_transcription
        self.skip_summarization = args.skip_summarization

class PipelineStats:
    """Track pipeline execution statistics."""
    
    def __init__(self):
        self.start_time = time.time()
        self.podcasts_processed = 0
        self.episodes_found = 0
        self.episodes_downloaded = 0
        self.episodes_transcribed = 0
        self.episodes_summarized = 0
        self.episodes_failed = 0
        self.errors = []
    
    def add_error(self, podcast: str, episode: str, error: str):
        """Record an error."""
        self.episodes_failed += 1
        self.errors.append({
            'podcast': podcast,
            'episode': episode,
            'error': error,
            'timestamp': datetime.now().isoformat()
        })
    
    def get_duration(self) -> float:
        """Get total processing time in seconds."""
        return time.time() - self.start_time
    
    def print_summary(self):
        """Print execution summary."""
        duration = self.get_duration()
        
        print("\n" + "="*60)
        print("PIPELINE EXECUTION SUMMARY")
        print("="*60)
        print(f"Total execution time: {duration:.1f} seconds")
        print(f"Podcasts processed: {self.podcasts_processed}")
        print(f"Episodes found: {self.episodes_found}")
        print(f"Episodes downloaded: {self.episodes_downloaded}")
        print(f"Episodes transcribed: {self.episodes_transcribed}")
        print(f"Episodes summarized: {self.episodes_summarized}")
        print(f"Episodes failed: {self.episodes_failed}")
        
        if self.errors:
            print(f"\nErrors ({len(self.errors)}):")
            for error in self.errors[-5:]:  # Show last 5 errors
                print(f"  - {error['podcast']}: {error['error']}")
            
            if len(self.errors) > 5:
                print(f"  ... and {len(self.errors) - 5} more errors")
        
        print("="*60)

async def process_episode(
    podcast_name: str,
    episode_data: Dict[str, Any],
    config: PipelineConfig,
    db: DatabaseManager,
    stats: PipelineStats
) -> bool:
    """
    Process a single episode through the complete pipeline.
    
    Returns:
        True if processing was successful, False otherwise
    """
    episode_id = generate_episode_id(episode_data['guid'], episode_data['audio_url'])
    
    logger.info(f"Processing episode: {episode_data['title'][:50]}...")
    
    try:
        # Check if already processed
        if not config.force_reprocess and db.is_episode_processed(podcast_name, episode_data['guid']):
            logger.info(f"Episode already processed, skipping: {episode_id}")
            return True
        
        # Get file paths
        paths = get_file_paths(podcast_name, episode_id)
        
        # Insert/update episode record
        episode_data_with_paths = {
            **episode_data,
            'audio_path': paths['audio_path'],
            'transcript_path': paths['transcript_path'],
            'summary_path': paths['summary_path']
        }
        
        db.insert_episode(podcast_name, episode_data_with_paths)
        db.update_episode_status(podcast_name, episode_data['guid'], 'processing')
        
        # Step 1: Download audio
        if not config.skip_download:
            if not os.path.exists(paths['audio_path']) or config.force_reprocess:
                logger.info(f"Downloading audio for episode: {episode_id}")
                
                if not config.dry_run:
                    download_audio(episode_data['audio_url'], paths['audio_path'])
                    
                    # Calculate and store audio hash
                    audio_hash = file_hash(paths['audio_path'])
                    db.update_episode_paths(podcast_name, episode_data['guid'], audio_hash=audio_hash)
                
                stats.episodes_downloaded += 1
            else:
                logger.info(f"Audio file already exists: {paths['audio_path']}")
        
        # Step 2: Transcribe audio
        transcript_result = None
        if not config.skip_transcription:
            if not os.path.exists(paths['transcript_path']) or config.force_reprocess:
                logger.info(f"Transcribing episode: {episode_id}")
                
                if not config.dry_run:
                    transcript_result = transcribe_audio(
                        paths['audio_path'],
                        model_name=config.whisper_model
                    )
                    
                    db.update_episode_paths(
                        podcast_name, 
                        episode_data['guid'], 
                        transcript_path=transcript_result.transcript_path
                    )
                
                stats.episodes_transcribed += 1
            else:
                logger.info(f"Transcript already exists: {paths['transcript_path']}")
        
        # Step 3: Generate summary
        if not config.skip_summarization:
            if not os.path.exists(paths['summary_path']) or config.force_reprocess:
                logger.info(f"Generating summary for episode: {episode_id}")
                
                if not config.dry_run:
                    summary_path = await generate_and_save_summary(
                        paths['transcript_path'],
                        paths['summary_path'],
                        provider=config.provider,
                        model=config.model
                    )
                    
                    db.update_episode_paths(
                        podcast_name,
                        episode_data['guid'],
                        summary_path=summary_path
                    )
                
                stats.episodes_summarized += 1
            else:
                logger.info(f"Summary already exists: {paths['summary_path']}")
        
        # Save episode metadata
        if not config.dry_run:
            processing_info = {
                'whisper_model': config.whisper_model,
                'summary_provider': config.provider,
                'summary_model': config.model,
                'transcription_info': transcript_result.info if transcript_result else None
            }
            
            save_episode_metadata(
                paths['episode_metadata_path'],
                episode_data,
                processing_info
            )
        
        # Mark as completed
        db.update_episode_status(podcast_name, episode_data['guid'], 'completed')
        
        logger.info(f"Successfully processed episode: {episode_id}")
        return True
        
    except Exception as e:
        error_msg = f"Failed to process episode {episode_id}: {str(e)}"
        logger.error(error_msg)
        
        stats.add_error(podcast_name, episode_id, str(e))
        db.update_episode_status(podcast_name, episode_data['guid'], 'failed', str(e))
        
        return False

async def process_podcast(
    podcast_name: str,
    podcast_url: str,
    config: PipelineConfig,
    db: DatabaseManager,
    stats: PipelineStats
) -> List[Dict[str, Any]]:
    """
    Process all episodes for a single podcast.
    
    Returns:
        List of processed episode data
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Processing podcast: {podcast_name}")
    logger.info(f"URL: {podcast_url}")
    logger.info(f"{'='*60}")
    
    try:
        # Resolve RSS feed
        logger.info(f"Resolving RSS feed for: {podcast_name}")
        feed_url = resolve_feed(podcast_url)
        
        if not feed_url:
            raise Exception(f"Unable to resolve RSS feed for: {podcast_url}")
        
        logger.info(f"Found RSS feed: {feed_url}")
        
        # Get episode list
        logger.info(f"Fetching episodes from RSS feed...")
        episodes = list_audio_items(feed_url)
        
        if not episodes:
            logger.warning(f"No audio episodes found in feed: {feed_url}")
            return []
        
        # Filter episodes
        filtered_episodes = filter_items(episodes, last_n=config.last_n, days=config.days)
        
        if not filtered_episodes:
            logger.warning(f"No episodes match filter criteria")
            return []
        
        logger.info(f"Processing {len(filtered_episodes)} episodes for {podcast_name}")
        stats.episodes_found += len(filtered_episodes)
        
        # Process episodes
        processed_episodes = []
        
        # Create semaphore for concurrent processing
        semaphore = asyncio.Semaphore(config.max_concurrent)
        
        async def process_with_semaphore(episode_data):
            async with semaphore:
                success = await process_episode(podcast_name, episode_data, config, db, stats)
                if success:
                    processed_episodes.append(episode_data)
                return success
        
        # Process episodes concurrently
        tasks = [process_with_semaphore(episode) for episode in filtered_episodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Count successful processing
        successful_count = sum(1 for result in results if result is True)
        
        logger.info(f"Completed processing {podcast_name}: {successful_count}/{len(filtered_episodes)} episodes successful")
        
        stats.podcasts_processed += 1
        return processed_episodes
        
    except Exception as e:
        error_msg = f"Failed to process podcast {podcast_name}: {str(e)}"
        logger.error(error_msg)
        stats.add_error(podcast_name, "podcast", str(e))
        return []

async def run_pipeline(config: PipelineConfig):
    """Run the complete podcast processing pipeline."""
    logger.info(f"Starting podcast processing pipeline...")
    logger.info(f"Configuration:")
    logger.info(f"  CSV file: {config.csv_path}")
    logger.info(f"  Filter: {config.last_n if config.last_n else f'{config.days} days'}")
    logger.info(f"  Provider: {config.provider}")
    logger.info(f"  Model: {config.model}")
    logger.info(f"  Whisper model: {config.whisper_model}")
    logger.info(f"  Force reprocess: {config.force_reprocess}")
    logger.info(f"  Dry run: {config.dry_run}")
    
    # Initialize components
    db = DatabaseManager()
    stats = PipelineStats()
    
    # Load CSV file
    try:
        with open(config.csv_path, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            podcast_rows = list(reader)
    except Exception as e:
        logger.error(f"Failed to read CSV file {config.csv_path}: {e}")
        return
    
    if not podcast_rows:
        logger.error("No podcasts found in CSV file")
        return
    
    logger.info(f"Found {len(podcast_rows)} podcasts in CSV file")
    
    # Process each podcast
    for row in podcast_rows:
        podcast_name = row.get('Podcast', '').strip()
        podcast_url = row.get('URL', '').strip()
        
        if not podcast_name or not podcast_url:
            logger.warning(f"Skipping invalid row: {row}")
            continue
        
        await process_podcast(podcast_name, podcast_url, config, db, stats)
    
    # Print final summary
    stats.print_summary()

def load_environment():
    """Load environment variables from .env file if it exists."""
    env_file = Path('.env')
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv()
            logger.info("Loaded environment variables from .env file")
        except ImportError:
            logger.warning("python-dotenv not installed, skipping .env file loading")

def validate_environment(provider: str):
    """Validate required environment variables."""
    required_vars = []
    
    if provider == "openai":
        required_vars = ["OPENAI_API_KEY"]
    elif provider == "openrouter":
        required_vars = ["OPENROUTER_API_KEY"]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.error("Please set them in your environment or .env file")
        return False
    
    return True

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Podcast Transcription Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process last 3 episodes for each podcast using OpenAI
  python podcast_pipeline.py --csv podcasts.csv --last-n 3 --provider openai
  
  # Process episodes from last 14 days using OpenRouter
  python podcast_pipeline.py --csv podcasts.csv --days 14 --provider openrouter
  
  # Dry run to see what would be processed
  python podcast_pipeline.py --csv podcasts.csv --last-n 1 --dry-run
        """
    )
    
    # Required arguments
    parser.add_argument("--csv", required=True, help="Path to CSV file with Podcast,URL columns")
    
    # Episode filtering (mutually exclusive)
    filter_group = parser.add_mutually_exclusive_group(required=True)
    filter_group.add_argument("--last-n", type=int, help="Process the last N episodes for each podcast")
    filter_group.add_argument("--days", type=int, help="Process episodes published in the last X days")
    
    # Provider settings
    parser.add_argument("--provider", choices=["openai", "openrouter"], default="openai",
                       help="API provider for summarization (default: openai)")
    parser.add_argument("--model", help="Specific model to use (provider-dependent)")
    parser.add_argument("--whisper-model", help="Whisper model size (tiny, base, small, medium, large-v3)")
    
    # Processing options
    parser.add_argument("--force", action="store_true", help="Force reprocessing of existing episodes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed without actually doing it")
    parser.add_argument("--max-concurrent", type=int, default=2, help="Maximum concurrent episode processing (default: 2)")
    
    # Skip options
    parser.add_argument("--skip-download", action="store_true", help="Skip audio download step")
    parser.add_argument("--skip-transcription", action="store_true", help="Skip transcription step")
    parser.add_argument("--skip-summarization", action="store_true", help="Skip summarization step")
    
    # Logging
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO",
                       help="Logging level (default: INFO)")
    
    args = parser.parse_args()
    
    # Setup logging
    setup_logging(args.log_level)
    
    # Load environment
    load_environment()
    
    # Validate configuration
    if not Path(args.csv).exists():
        logger.error(f"CSV file not found: {args.csv}")
        sys.exit(1)
    
    if not validate_environment(args.provider):
        sys.exit(1)
    
    # Create configuration
    config = PipelineConfig(args)
    
    # Run pipeline
    try:
        asyncio.run(run_pipeline(config))
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()