# How to Launch the Podcast Transcription Web Interface

This guide explains how to launch and access the web interface for the Podcast Transcription service.

## Prerequisites

Before launching the application, ensure you have:

- Python 3.11+ installed
- PostgreSQL **OR** SQLite (SQLite is simpler for local development)
- Redis server running
- All required Python packages installed

## Quick Start

### Option 1: Local Development (Recommended for Testing)

This is the simplest way to run the application locally using SQLite.

1. **Navigate to the project directory**
   ```bash
   cd /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb
   ```

2. **Check your environment configuration**

   Verify that your `.env` file contains at least these settings:
   ```bash
   # OpenAI API Key (required for transcription and summarization)
   OPENAI_API_KEY=your-openai-api-key-here
   OPENAI_MODEL=gpt-4o-mini

   # Flask Web Application
   DATABASE_URL=sqlite:////absolute/path/to/project/data/podcast_transcription.db
   REDIS_URL=redis://localhost:6379/0
   SECRET_KEY=dev-key-change-in-production-please
   ```

3. **Ensure Redis is running**
   ```bash
   # Check if Redis is running
   redis-cli ping
   # Should return: PONG

   # If not running, start Redis:
   redis-server
   ```

4. **Install Python dependencies** (if not already installed)
   ```bash
   pip install -r requirements.txt
   ```

5. **Launch the Flask application**
   ```bash
   python app.py
   ```

6. **Access the web interface**

   Open your web browser and navigate to:
   ```
   http://127.0.0.1:5000          # From the same machine
   http://localhost:5000           # From the same machine
   http://192.168.68.107:5000     # From any device on your network
   ```

   **Note:** The application is configured to be accessible from other computers on your local network. See `NETWORK_ACCESS.md` for details.

### Option 2: Production-Style with Gunicorn

For a more production-like environment:

1. **Start Gunicorn**
   ```bash
   gunicorn --bind 0.0.0.0:8005 --workers 2 --timeout 120 app:app
   ```

2. **Access the web interface**
   ```
   http://localhost:8005
   ```

### Option 3: Docker Deployment

If you want to use Docker (requires Docker and Docker Compose):

**Note:** The default docker-compose.yml uses ports 5432 (PostgreSQL) and 6379 (Redis). If these are already in use on your host, you'll need to modify the ports in docker-compose.yml.

1. **Start all services with Docker Compose**
   ```bash
   docker compose up --build
   ```

2. **Access the web interface**
   ```
   http://localhost:8005
   ```

3. **Stop services**
   ```bash
   docker compose down
   ```

## Background Task Processing (Optional)

The application uses Celery for background tasks like transcription and summarization. To enable background processing:

1. **Ensure Redis is running** (Celery uses Redis as a message broker)

2. **Start a Celery worker** in a separate terminal:
   ```bash
   cd /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb
   celery -A tasks worker --loglevel=info
   ```

Without Celery running, transcription and summarization tasks will not be processed automatically.

## Current Running Instance

As of this documentation, the application is currently running at:

- **URL:** http://127.0.0.1:5000
- **Process ID:** Check with `ps aux | grep "python app.py"`
- **Database:** SQLite at `/media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb/data/podcast_transcription.db`
- **Redis:** Running on localhost:6379

To stop the current instance:
```bash
# Find the process
ps aux | grep "python app.py"

# Kill the process (replace PID with actual process ID)
kill <PID>
```

## Troubleshooting

### Port Already in Use

If you get an error that the port is already in use:

```bash
# Find what's using the port
lsof -i :5000

# Kill the process or use a different port
FLASK_RUN_PORT=5001 python app.py
```

### Database Connection Issues

**PostgreSQL Authentication Failed:**
- Switch to SQLite for local development (simpler, no password needed)
- Update `.env` to use SQLite:
  ```
  DATABASE_URL=sqlite:////absolute/path/to/data/podcast_transcription.db
  ```

**SQLite Database File Not Found:**
- Use absolute paths in the DATABASE_URL
- Ensure the `data/` directory exists and has write permissions:
  ```bash
  mkdir -p data
  chmod 755 data
  ```

### Redis Connection Failed

```bash
# Check if Redis is running
redis-cli ping

# If not, start Redis server
redis-server

# Or install Redis if not installed:
# Ubuntu/Debian: sudo apt-get install redis-server
# macOS: brew install redis
```

### Missing Python Packages

```bash
pip install -r requirements.txt
```

### Import Errors

If you get module import errors, ensure you're in the correct directory and have activated your virtual environment (if using one):

```bash
cd /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb
# If using virtualenv:
source venv/bin/activate  # Linux/Mac
```

## Application Features

Once the web interface is running, you can:

1. **Register/Login** - Create a user account (free, no payment required)
2. **Add Podcasts** - Add podcasts by URL or RSS feed
3. **View Dashboard** - See your podcasts and recent episodes
4. **Manage Episodes** - View transcripts and summaries
5. **Trigger Processing** - Manually trigger transcription or summarization

## Configuration

### Essential Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `OPENAI_API_KEY` | OpenAI API key for transcription/summarization | `sk-proj-...` |
| `DATABASE_URL` | Database connection string | `sqlite:////path/to/db.db` |
| `REDIS_URL` | Redis connection string | `redis://localhost:6379/0` |
| `SECRET_KEY` | Flask secret key for sessions | Any random string |

### Optional Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENAI_MODEL` | OpenAI model for summaries | `gpt-4o-mini` |
| `MAILGUN_API_KEY` | Mailgun API key for email | None |
| `MAILGUN_DOMAIN` | Mailgun domain | None |

## Next Steps

1. **Configure API Keys** - Add your OpenAI API key to `.env`
2. **Set Up Email** (Optional) - Configure Mailgun for email notifications
3. **Start Celery Worker** - Enable background task processing
4. **Add Podcasts** - Start adding podcasts through the web interface
5. **Monitor Logs** - Watch the console output for any errors

## Support

For issues or questions:
- Check the main README.md for more detailed setup instructions
- Review the application logs in the console output
- Check Redis and database connectivity
- Ensure all environment variables are set correctly

## Security Notes

⚠️ **For Development Only:**
- The current setup uses a development SECRET_KEY
- SQLite is not recommended for production
- HTTPS should be used in production
- Never commit API keys to version control

For production deployment, see `DEPLOYMENT.md` or `DEPLOY_NOW.md`.
