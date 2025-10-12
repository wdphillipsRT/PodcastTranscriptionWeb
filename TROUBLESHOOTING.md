# Troubleshooting Guide

This guide helps resolve common issues with the Podcast Transcription Web application.

## Table of Contents

1. [No Episodes Appearing After Adding Podcast](#no-episodes-appearing-after-adding-podcast)
2. [Celery Worker Issues](#celery-worker-issues)
3. [Database Connection Problems](#database-connection-problems)
4. [Redis Connection Issues](#redis-connection-issues)
5. [Transcription Not Starting](#transcription-not-starting)
6. [Network Access Problems](#network-access-problems)
7. [Performance Issues](#performance-issues)

---

## No Episodes Appearing After Adding Podcast

### Symptoms
- Podcast added successfully
- Dashboard shows the podcast
- But clicking "View Episodes" shows no episodes
- Episodes count shows 0

### Root Causes
1. Celery background worker not running
2. Celery task failed silently
3. Redis queue issues
4. Feed parsing errors

### Diagnosis Steps

**Step 1: Check if podcast was added**
```bash
python << 'EOF'
from app import app, db
from models import Podcast

with app.app_context():
    podcasts = Podcast.query.all()
    for p in podcasts:
        print(f"ID: {p.id}, Name: {p.name}, Episodes: {len(p.episodes)}")
EOF
```

**Step 2: Test RSS feed resolution**
```bash
python -c "
from feed_resolver import resolve_feed

url = 'YOUR_PODCAST_URL_HERE'
rss_url = resolve_feed(url)
print(f'RSS URL: {rss_url}')
"
```

**Step 3: Test episode fetching**
```bash
python -c "
from episode_fetcher import list_audio_items, filter_items

rss_url = 'YOUR_RSS_URL_HERE'
items = list_audio_items(rss_url)
recent = filter_items(items, last_n=10)
print(f'Total episodes: {len(items)}')
print(f'Recent episodes: {len(recent)}')
"
```

**Step 4: Check Celery worker status**
```bash
ps aux | grep celery | grep -v grep
```

**Step 5: Check Redis connectivity**
```bash
redis-cli ping
# Should return: PONG
```

### Solutions

#### Solution 1: Manually Trigger Episode Fetching

```bash
python << 'EOF'
import sys
sys.path.insert(0, '/media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb')

from app import app, db
from models import Podcast, Episode
from episode_fetcher import list_audio_items, filter_items, generate_episode_id
from datetime import datetime, timezone

# Replace PODCAST_ID with your podcast ID
PODCAST_ID = 1

with app.app_context():
    podcast = Podcast.query.get(PODCAST_ID)

    if not podcast:
        print("Podcast not found")
        sys.exit(1)

    print(f"Processing podcast: {podcast.name}")

    # Fetch episodes
    items = list_audio_items(podcast.rss_url)
    recent_items = filter_items(items, last_n=10)

    print(f"Found {len(recent_items)} recent episodes")

    episodes_created = 0
    for item in recent_items:
        episode_id = generate_episode_id(item['guid'], item['published'])

        # Check if episode already exists
        existing = Episode.query.filter_by(
            podcast_id=podcast.id,
            guid=item['guid']
        ).first()

        if not existing:
            episode = Episode(
                podcast_id=podcast.id,
                guid=item['guid'],
                title=item['title'],
                description=item.get('description', ''),
                audio_url=item['audio_url'],
                published_at=item['published'],
                status='pending',
                created_at=datetime.now(timezone.utc)
            )

            db.session.add(episode)
            episodes_created += 1
            print(f"Added: {item['title']}")

    db.session.commit()

    print(f"\nCreated {episodes_created} new episodes")
EOF
```

#### Solution 2: Restart Celery Worker

```bash
# Find and kill existing Celery processes
pkill -f "celery.*worker"

# Start Celery worker again
celery -A tasks worker --loglevel=info
```

#### Solution 3: Clear Redis Queue and Retry

```bash
# Clear Redis queues (WARNING: This clears all queued tasks)
redis-cli FLUSHALL

# Trigger episode fetching via Celery
python -c "
from tasks import queue_podcast_processing
result = queue_podcast_processing.delay(PODCAST_ID)
print(f'Task queued: {result.id}')
"
```

---

## Celery Worker Issues

### Symptoms
- Tasks not being processed
- Episodes stuck in 'pending' status
- Background jobs not running

### Diagnosis

**Check if Celery is running:**
```bash
ps aux | grep celery | grep -v grep
```

**Check Celery logs:**
```bash
# If running in foreground, check the terminal output
# If running as service:
journalctl -u celery -n 50
```

**Test Celery connectivity:**
```bash
python -c "
from tasks import celery_app
i = celery_app.control.inspect()
print('Active tasks:', i.active())
print('Registered tasks:', i.registered())
"
```

### Solutions

**Start Celery Worker:**
```bash
# In a separate terminal
cd /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb
celery -A tasks worker --loglevel=info
```

**Run Celery as Background Service:**
```bash
# Create a systemd service file
sudo nano /etc/systemd/system/podcast-celery.service
```

Add this content:
```ini
[Unit]
Description=Celery Worker for Podcast Transcription
After=network.target redis.service

[Service]
Type=forking
User=YOUR_USERNAME
Group=YOUR_USERNAME
WorkingDirectory=/media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb
Environment="PATH=/home/YOUR_USERNAME/anaconda3/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/home/YOUR_USERNAME/anaconda3/bin/celery -A tasks worker --detach --loglevel=info
Restart=always

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable podcast-celery
sudo systemctl start podcast-celery
sudo systemctl status podcast-celery
```

---

## Database Connection Problems

### Symptoms
- "Database connection failed" errors
- "Unable to open database file" (SQLite)
- "Authentication failed" (PostgreSQL)

### SQLite Issues

**Problem: Database file not found**
```bash
# Check if database exists
ls -la /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb/data/

# Create database if missing
mkdir -p /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb/data
python -c "
from app import app, db
with app.app_context():
    db.create_all()
print('Database created')
"
```

**Problem: Permission denied**
```bash
# Fix permissions
chmod 755 /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb/data
chmod 644 /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb/data/*.db
```

### PostgreSQL Issues

**Problem: Connection refused**
```bash
# Check if PostgreSQL is running
sudo systemctl status postgresql

# Start if not running
sudo systemctl start postgresql
```

**Problem: Authentication failed**
```bash
# Update .env with correct credentials
DATABASE_URL=postgresql://username:password@localhost:5432/podcast_transcription

# Or switch to SQLite for local development
DATABASE_URL=sqlite:////absolute/path/to/data/podcast_transcription.db
```

---

## Redis Connection Issues

### Symptoms
- Celery worker can't connect
- "Connection refused" errors
- Tasks not being queued

### Diagnosis

```bash
# Check if Redis is running
ps aux | grep redis | grep -v grep

# Test Redis connection
redis-cli ping
# Should return: PONG

# Check Redis configuration
redis-cli CONFIG GET bind
redis-cli CONFIG GET port
```

### Solutions

**Start Redis:**
```bash
# Ubuntu/Debian
sudo systemctl start redis-server
sudo systemctl status redis-server

# Or run manually
redis-server
```

**Install Redis if missing:**
```bash
# Ubuntu/Debian
sudo apt-get update
sudo apt-get install redis-server

# Enable auto-start
sudo systemctl enable redis-server
```

**Test Redis with Python:**
```bash
python -c "
import redis
r = redis.Redis(host='localhost', port=6379, db=0)
print(r.ping())  # Should print: True
"
```

---

## Transcription Not Starting

### Symptoms
- Episodes stuck in 'pending' status
- No processing jobs being created
- Celery running but tasks not executing

### Diagnosis

**Check episode status:**
```bash
python -c "
from app import app, db
from models import Episode

with app.app_context():
    episodes = Episode.query.filter_by(status='pending').all()
    print(f'Pending episodes: {len(episodes)}')

    for ep in episodes[:5]:
        print(f'  - {ep.title} (ID: {ep.id})')
"
```

**Check processing jobs:**
```bash
python -c "
from app import app, db
from models import ProcessingJob

with app.app_context():
    jobs = ProcessingJob.query.order_by(ProcessingJob.created_at.desc()).limit(10).all()
    print(f'Recent jobs: {len(jobs)}')

    for job in jobs:
        print(f'  - Episode {job.episode_id}: {job.job_type} - {job.status}')
"
```

### Solutions

**Manually trigger transcription for an episode:**
```bash
# Via web interface:
# 1. Go to dashboard
# 2. Find the episode
# 3. Click "View"
# 4. Click "Trigger Transcription"

# Or via CLI:
python -c "
from tasks import process_episode
episode_id = 1  # Replace with your episode ID
result = process_episode.delay(episode_id)
print(f'Transcription task queued: {result.id}')
"
```

**Check if OpenAI API key is configured:**
```bash
python -c "
import os
from dotenv import load_dotenv
load_dotenv()

api_key = os.getenv('OPENAI_API_KEY')
if api_key:
    print(f'API key configured: {api_key[:10]}...')
else:
    print('ERROR: OPENAI_API_KEY not set!')
"
```

---

## Network Access Problems

### Symptoms
- Can't access from other computers
- Connection timeout
- "This site can't be reached"

### Diagnosis

**Check application is listening on correct interface:**
```bash
ss -tlnp | grep :5000
# Should show: 0.0.0.0:5000 (not 127.0.0.1:5000)
```

**Get local IP address:**
```bash
hostname -I | awk '{print $1}'
```

**Test from host machine:**
```bash
curl http://192.168.68.107:5000
# Should return HTML
```

**Test connectivity:**
```bash
# From another device
ping 192.168.68.107
```

### Solutions

**Ensure app is bound to 0.0.0.0:**

Check `app.py` has:
```python
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000, debug=True)
```

**Check firewall (if applicable):**
```bash
# Ubuntu/Debian with UFW
sudo ufw status
sudo ufw allow 5000/tcp

# Or disable for testing
sudo ufw disable
```

**Use production server (Gunicorn):**
```bash
gunicorn --bind 0.0.0.0:8005 --workers 2 --timeout 120 app:app
```

For more details, see [NETWORK_ACCESS.md](NETWORK_ACCESS.md).

---

## Performance Issues

### Symptoms
- Slow page loads
- Transcription taking too long
- High CPU/memory usage

### Diagnosis

**Check system resources:**
```bash
# CPU and memory usage
top -b -n 1 | head -20

# Disk space
df -h

# Check specific processes
ps aux | grep python | head -10
```

**Check database size:**
```bash
ls -lh /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb/data/*.db

# For PostgreSQL
psql -U postgres -c "SELECT pg_size_pretty(pg_database_size('podcast_transcription'));"
```

**Check Redis memory:**
```bash
redis-cli INFO memory
```

### Solutions

**Optimize database:**
```bash
# SQLite
python -c "
from app import app, db
with app.app_context():
    db.session.execute('VACUUM')
    db.session.commit()
print('Database optimized')
"
```

**Limit concurrent Celery workers:**
```bash
# Edit celeryconfig.py or start with:
celery -A tasks worker --loglevel=info --concurrency=2
```

**Clean up old data:**
```bash
python -c "
from app import app, db
from models import Episode
from datetime import datetime, timedelta, timezone

with app.app_context():
    # Delete episodes older than 90 days with 'failed' status
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    old_failed = Episode.query.filter(
        Episode.status == 'failed',
        Episode.created_at < cutoff
    ).delete()

    db.session.commit()
    print(f'Deleted {old_failed} old failed episodes')
"
```

**Use production WSGI server:**
```bash
# Instead of Flask development server, use Gunicorn
pip install gunicorn
gunicorn --bind 0.0.0.0:8005 --workers 2 --timeout 120 app:app
```

---

## Quick Diagnostic Script

Run this comprehensive diagnostic to check all components:

```bash
cat > diagnostic.sh << 'SCRIPT'
#!/bin/bash
echo "=== Podcast Transcription Diagnostic ==="
echo

echo "1. Python version:"
python --version
echo

echo "2. Working directory:"
pwd
echo

echo "3. Database file:"
ls -lh data/*.db 2>/dev/null || echo "No database file found"
echo

echo "4. Redis status:"
redis-cli ping 2>/dev/null || echo "Redis not responding"
echo

echo "5. Celery workers:"
ps aux | grep celery | grep -v grep | wc -l
echo

echo "6. Flask app running:"
ss -tlnp | grep :5000 || echo "No Flask app on port 5000"
echo

echo "7. Network interface:"
hostname -I | awk '{print $1}'
echo

echo "8. Environment variables:"
python -c "
import os
from dotenv import load_dotenv
load_dotenv()
print(f'DATABASE_URL: {os.getenv(\"DATABASE_URL\", \"Not set\")[:50]}...')
print(f'REDIS_URL: {os.getenv(\"REDIS_URL\", \"Not set\")}')
print(f'OPENAI_API_KEY: {\"Set\" if os.getenv(\"OPENAI_API_KEY\") else \"Not set\"}')
"
echo

echo "9. Database contents:"
python -c "
from app import app, db
from models import Podcast, Episode
with app.app_context():
    print(f'Podcasts: {Podcast.query.count()}')
    print(f'Episodes: {Episode.query.count()}')
    print(f'Pending: {Episode.query.filter_by(status=\"pending\").count()}')
    print(f'Processing: {Episode.query.filter_by(status=\"processing\").count()}')
    print(f'Completed: {Episode.query.filter_by(status=\"completed\").count()}')
" 2>/dev/null || echo "Database query failed"

echo
echo "=== Diagnostic Complete ==="
SCRIPT

chmod +x diagnostic.sh
./diagnostic.sh
```

---

## Getting Help

If you've tried all the above solutions and still have issues:

1. **Check the logs:**
   - Flask app output in terminal
   - Celery worker output
   - System logs: `journalctl -xe`

2. **Gather diagnostic information:**
   - Run the diagnostic script above
   - Note exact error messages
   - Check what changed before the issue started

3. **Common fixes that work:**
   - Restart everything (Flask, Celery, Redis)
   - Check .env file is properly configured
   - Ensure all dependencies are installed: `pip install -r requirements.txt`
   - Verify file permissions in data/ directory

4. **Review documentation:**
   - [README.md](README.md) - Main documentation
   - [LAUNCH.md](LAUNCH.md) - Launch instructions
   - [NETWORK_ACCESS.md](NETWORK_ACCESS.md) - Network setup
   - [CHANGES.md](CHANGES.md) - Recent changes

---

## Preventive Maintenance

**Daily Checks:**
- Verify Celery worker is running
- Check disk space
- Monitor Redis memory usage

**Weekly Checks:**
- Review failed episodes and retry if needed
- Clean up old processing jobs
- Backup database

**Monthly Checks:**
- Update dependencies: `pip install -U -r requirements.txt`
- Review system logs for recurring errors
- Optimize database: `VACUUM` (SQLite) or `VACUUM ANALYZE` (PostgreSQL)

---

## Emergency Recovery

If everything is broken and you need to start fresh:

```bash
# 1. Stop all services
pkill -f "python.*app.py"
pkill -f "celery.*worker"

# 2. Backup current database
cp data/podcast_transcription.db data/podcast_transcription.db.backup

# 3. Clear Redis
redis-cli FLUSHALL

# 4. Recreate database (WARNING: Deletes all data!)
rm data/podcast_transcription.db
python -c "from app import app, db;
with app.app_context(): db.create_all()"

# 5. Restart everything
python app.py &  # Or use Gunicorn
celery -A tasks worker --loglevel=info &

# 6. Restore data from backup if needed
```

---

Last updated: 2025-10-12
