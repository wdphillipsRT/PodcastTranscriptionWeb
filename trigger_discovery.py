#!/usr/bin/env python3
"""Manually trigger episode discovery for existing podcasts."""

from app import app
from tasks import queue_podcast_processing
from models import Podcast

def trigger_discovery():
    with app.app_context():
        podcasts = Podcast.query.all()
        print(f"Found {len(podcasts)} podcasts to process")
        
        for podcast in podcasts:
            print(f"Triggering episode discovery for: {podcast.name}")
            print(f"  RSS URL: {podcast.rss_url}")
            
            try:
                # Queue the podcast processing task
                result = queue_podcast_processing.delay(podcast.id)
                print(f"  Task queued with ID: {result.id}")
            except Exception as e:
                print(f"  Error queuing task: {e}")

if __name__ == '__main__':
    trigger_discovery()