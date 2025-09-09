#!/usr/bin/env python3
"""Debug script to check episodes in database."""

from app import app, db
from models import Podcast, Episode, User

def debug_episodes():
    with app.app_context():
        # Check users
        users = User.query.all()
        print(f"Total users: {len(users)}")
        
        # Check podcasts
        podcasts = Podcast.query.all()
        print(f"Total podcasts: {len(podcasts)}")
        
        for podcast in podcasts:
            print(f"\nPodcast {podcast.id}: {podcast.name}")
            print(f"  User ID: {podcast.user_id}")
            print(f"  URL: {podcast.url}")
            print(f"  RSS URL: {podcast.rss_url}")
            print(f"  Created: {podcast.created_at}")
            
            # Check episodes for this podcast
            episodes = Episode.query.filter_by(podcast_id=podcast.id).all()
            print(f"  Episodes in database: {len(episodes)}")
            
            if episodes:
                for i, episode in enumerate(episodes[:3]):  # Show first 3
                    print(f"    {i+1}. {episode.title[:60]}...")
                    print(f"       Status: {episode.status}")
                    print(f"       GUID: {episode.guid}")
                    print(f"       Published: {episode.published_at}")
            else:
                print("    No episodes found")

if __name__ == '__main__':
    debug_episodes()