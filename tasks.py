from celery import Celery
import os
from datetime import datetime, timezone
import logging
from typing import List, Dict, Any
import requests

# Initialize Celery
celery_app = Celery('podcast_transcription')
celery_app.config_from_object('celeryconfig')

# Configure logging
logger = logging.getLogger(__name__)

@celery_app.task(bind=True)
def queue_podcast_processing(self, podcast_id: int):
    """Queue processing for all episodes of a podcast."""
    from app import app, db
    from models import Podcast, Episode
    from episode_fetcher import list_audio_items, filter_items, generate_episode_id
    
    with app.app_context():
        podcast = Podcast.query.get(podcast_id)
        if not podcast:
            logger.error(f"Podcast {podcast_id} not found")
            return
        
        try:
            # Fetch episodes from RSS feed
            items = list_audio_items(podcast.rss_url)
            # Get last 10 episodes by default
            recent_items = filter_items(items, last_n=10)
            
            episodes_created = 0
            for item in recent_items:
                episode_id = generate_episode_id(item['guid'], item['published'])
                
                # Check if episode already exists
                existing = Episode.query.filter_by(
                    podcast_id=podcast_id, 
                    guid=item['guid']
                ).first()
                
                if not existing:
                    episode = Episode(
                        podcast_id=podcast_id,
                        guid=item['guid'],
                        title=item['title'],
                        description=item.get('description', ''),
                        audio_url=item['audio_url'],
                        published_at=item['published'],
                        status='pending',
                        created_at=datetime.now(timezone.utc)
                    )
                    
                    db.session.add(episode)
                    db.session.commit()
                    episodes_created += 1
                    
                    # Queue processing for this episode
                    process_episode.delay(episode.id)
            
            # Update last checked time
            podcast.last_checked = datetime.now(timezone.utc)
            db.session.commit()
            
            logger.info(f"Queued {episodes_created} new episodes for podcast {podcast.name}")
            
        except Exception as e:
            logger.error(f"Error processing podcast {podcast_id}: {e}")
            raise

@celery_app.task(bind=True)
def process_episode(self, episode_id: int):
    """Process a single episode: download, transcribe, summarize."""
    from app import app, db
    from models import Episode, ProcessingJob
    
    with app.app_context():
        episode = Episode.query.get(episode_id)
        if not episode:
            logger.error(f"Episode {episode_id} not found")
            return
        
        try:
            # Update episode status
            episode.status = 'processing'
            db.session.commit()
            
            # Download audio
            download_job = ProcessingJob(
                episode_id=episode_id,
                job_type='download',
                celery_task_id=self.request.id,
                started_at=datetime.now(timezone.utc)
            )
            db.session.add(download_job)
            db.session.commit()
            
            download_episode.delay(episode_id, download_job.id)
            
        except Exception as e:
            episode.status = 'failed'
            episode.error_message = str(e)
            db.session.commit()
            logger.error(f"Error processing episode {episode_id}: {e}")
            raise

@celery_app.task(bind=True)
def download_episode(self, episode_id: int, job_id: int):
    """Download audio file for an episode."""
    from app import app, db
    from models import Episode, ProcessingJob
    from episode_fetcher import download_audio
    from storage import get_file_paths
    
    with app.app_context():
        episode = Episode.query.get(episode_id)
        job = ProcessingJob.query.get(job_id)
        
        if not episode or not job:
            logger.error(f"Episode {episode_id} or job {job_id} not found")
            return
        
        try:
            job.status = 'processing'
            db.session.commit()
            
            # Get file paths
            paths = get_file_paths(episode.podcast.name, episode.guid)
            
            # Download audio
            success = download_audio(episode.audio_url, paths['audio'])
            
            if success:
                episode.audio_path = str(paths['audio'])
                job.status = 'completed'
                job.completed_at = datetime.now(timezone.utc)
                
                # Queue transcription
                transcribe_job = ProcessingJob(
                    episode_id=episode_id,
                    job_type='transcribe',
                    started_at=datetime.now(timezone.utc)
                )
                db.session.add(transcribe_job)
                db.session.commit()
                
                transcribe_episode.delay(episode_id, transcribe_job.id)
            else:
                job.status = 'failed'
                job.error_message = 'Download failed'
                episode.status = 'failed'
            
            db.session.commit()
            
        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)
            episode.status = 'failed'
            db.session.commit()
            logger.error(f"Error downloading episode {episode_id}: {e}")
            raise

@celery_app.task(bind=True)
def transcribe_episode(self, episode_id: int, job_id: int):
    """Transcribe audio file for an episode."""
    from app import app, db
    from models import Episode, ProcessingJob
    from transcribe import transcribe_audio
    from storage import get_file_paths
    
    with app.app_context():
        episode = Episode.query.get(episode_id)
        job = ProcessingJob.query.get(job_id)
        
        if not episode or not job:
            logger.error(f"Episode {episode_id} or job {job_id} not found")
            return
        
        try:
            job.status = 'processing'
            db.session.commit()
            
            # Get file paths
            paths = get_file_paths(episode.podcast.name, episode.guid)
            
            # Transcribe audio
            whisper_model = os.environ.get('WHISPER_MODEL', 'medium')
            result = transcribe_audio(str(paths['audio']), whisper_model)
            
            if result:
                episode.transcript_path = str(paths['transcript'])
                episode.whisper_model = whisper_model
                episode.language = result.language
                episode.language_probability = result.language_probability
                episode.duration_seconds = int(result.duration)
                
                job.status = 'completed'
                job.completed_at = datetime.now(timezone.utc)
                
                # Send transcript ready email
                send_transcript_email.delay(episode_id)
                
                # Queue summarization
                summarize_job = ProcessingJob(
                    episode_id=episode_id,
                    job_type='summarize',
                    started_at=datetime.now(timezone.utc)
                )
                db.session.add(summarize_job)
                db.session.commit()
                
                summarize_episode.delay(episode_id, summarize_job.id)
            else:
                job.status = 'failed'
                job.error_message = 'Transcription failed'
            
            db.session.commit()
            
        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)
            db.session.commit()
            logger.error(f"Error transcribing episode {episode_id}: {e}")
            raise

@celery_app.task(bind=True)
def summarize_episode(self, episode_id: int, job_id: int):
    """Generate summary for an episode."""
    from app import app, db
    from models import Episode, ProcessingJob
    from summarize import generate_and_save_summary
    from storage import get_file_paths
    
    with app.app_context():
        episode = Episode.query.get(episode_id)
        job = ProcessingJob.query.get(job_id)
        
        if not episode or not job:
            logger.error(f"Episode {episode_id} or job {job_id} not found")
            return
        
        try:
            job.status = 'processing'
            db.session.commit()
            
            # Get file paths
            paths = get_file_paths(episode.podcast.name, episode.guid)
            
            # Generate summary
            provider = os.environ.get('DEFAULT_SUMMARY_PROVIDER', 'openai')
            model = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
            
            success = generate_and_save_summary(
                str(paths['transcript']),
                str(paths['summary']),
                provider,
                model
            )
            
            if success:
                episode.summary_path = str(paths['summary'])
                episode.summary_model = model
                episode.status = 'completed'
                
                job.status = 'completed'
                job.completed_at = datetime.now(timezone.utc)
                
                # Send summary ready email
                send_summary_email.delay(episode_id)
            else:
                job.status = 'failed'
                job.error_message = 'Summarization failed'
            
            db.session.commit()
            
        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)
            db.session.commit()
            logger.error(f"Error summarizing episode {episode_id}: {e}")
            raise

@celery_app.task(bind=True)
def send_transcript_email(self, episode_id: int):
    """Send email notification when transcript is ready."""
    from app import app, db
    from models import Episode, EmailNotification
    
    with app.app_context():
        episode = Episode.query.get(episode_id)
        if not episode or not episode.transcript_path:
            return
        
        try:
            # Read transcript
            with open(episode.transcript_path, 'r') as f:
                transcript_content = f.read()
            
            # Send email via Mailgun
            success = send_email_via_mailgun(
                to_email=episode.podcast.user.email,
                subject=f'Transcript Ready: {episode.title}',
                template='transcript_ready',
                context={
                    'episode_title': episode.title,
                    'podcast_name': episode.podcast.name,
                    'transcript_content': transcript_content[:5000] + '...' if len(transcript_content) > 5000 else transcript_content
                }
            )
            
            # Log notification
            notification = EmailNotification(
                user_id=episode.podcast.user_id,
                episode_id=episode_id,
                email_type='transcript_ready',
                status='sent' if success else 'failed',
                sent_at=datetime.now(timezone.utc) if success else None
            )
            db.session.add(notification)
            db.session.commit()
            
        except Exception as e:
            logger.error(f"Error sending transcript email for episode {episode_id}: {e}")

@celery_app.task(bind=True)
def send_summary_email(self, episode_id: int):
    """Send email notification when summary is ready."""
    from app import app, db
    from models import Episode, EmailNotification
    
    with app.app_context():
        episode = Episode.query.get(episode_id)
        if not episode or not episode.summary_path:
            return
        
        try:
            # Read summary
            with open(episode.summary_path, 'r') as f:
                summary_content = f.read()
            
            # Send email via Mailgun
            success = send_email_via_mailgun(
                to_email=episode.podcast.user.email,
                subject=f'Summary Ready: {episode.title}',
                template='summary_ready',
                context={
                    'episode_title': episode.title,
                    'podcast_name': episode.podcast.name,
                    'summary_content': summary_content
                }
            )
            
            # Log notification
            notification = EmailNotification(
                user_id=episode.podcast.user_id,
                episode_id=episode_id,
                email_type='summary_ready',
                status='sent' if success else 'failed',
                sent_at=datetime.now(timezone.utc) if success else None
            )
            db.session.add(notification)
            db.session.commit()
            
        except Exception as e:
            logger.error(f"Error sending summary email for episode {episode_id}: {e}")

@celery_app.task(bind=True)
def send_welcome_email(self, user_email: str):
    """Send welcome email to new subscriber."""
    try:
        success = send_email_via_mailgun(
            to_email=user_email,
            subject='Welcome to Podcast Transcription Service!',
            template='welcome',
            context={'email': user_email}
        )
        logger.info(f"Welcome email sent to {user_email}: {success}")
    except Exception as e:
        logger.error(f"Error sending welcome email to {user_email}: {e}")

def send_email_via_mailgun(to_email: str, subject: str, template: str, context: dict) -> bool:
    """Send email using Mailgun API."""
    api_key = os.environ.get('MAILGUN_API_KEY')
    domain = os.environ.get('MAILGUN_DOMAIN')
    
    if not api_key or not domain:
        logger.error("Mailgun API key or domain not configured")
        return False
    
    try:
        # Load email template
        template_path = f'templates/emails/{template}.html'
        with open(template_path, 'r') as f:
            html_content = f.read()
        
        # Simple template replacement
        for key, value in context.items():
            html_content = html_content.replace(f'{{{key}}}', str(value))
        
        response = requests.post(
            f"https://api.mailgun.net/v3/{domain}/messages",
            auth=("api", api_key),
            data={
                "from": f"Podcast Transcription <noreply@{domain}>",
                "to": [to_email],
                "subject": subject,
                "html": html_content
            }
        )
        
        return response.status_code == 200
        
    except Exception as e:
        logger.error(f"Error sending email via Mailgun: {e}")
        return False