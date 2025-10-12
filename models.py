from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime, timezone

# Initialize db here - will be imported by app.py
db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    podcasts = db.relationship('Podcast', backref='user', lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<User {self.email}>'

class Podcast(db.Model):
    __tablename__ = 'podcasts'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(500), nullable=False)  # Original URL (could be Apple Podcasts)
    rss_url = db.Column(db.String(500), nullable=False)  # Resolved RSS feed URL
    description = db.Column(db.Text)
    image_url = db.Column(db.String(500))
    last_checked = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    episodes = db.relationship('Episode', backref='podcast', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Podcast {self.name}>'

class Episode(db.Model):
    __tablename__ = 'episodes'
    
    id = db.Column(db.Integer, primary_key=True)
    podcast_id = db.Column(db.Integer, db.ForeignKey('podcasts.id'), nullable=False)
    guid = db.Column(db.String(500), nullable=False)  # RSS GUID
    title = db.Column(db.String(500), nullable=False)
    description = db.Column(db.Text)
    audio_url = db.Column(db.String(500), nullable=False)
    audio_hash = db.Column(db.String(64))  # SHA-256 hash for deduplication
    duration_seconds = db.Column(db.Integer)
    published_at = db.Column(db.DateTime(timezone=True))
    
    # File paths
    audio_path = db.Column(db.String(500))
    transcript_path = db.Column(db.String(500))
    summary_path = db.Column(db.String(500))
    
    # Processing status
    status = db.Column(db.String(50), default='pending')  # pending, processing, completed, failed
    error_message = db.Column(db.Text)
    
    # Metadata
    whisper_model = db.Column(db.String(50))
    summary_model = db.Column(db.String(100))
    language = db.Column(db.String(10))
    language_probability = db.Column(db.Float)
    
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    processing_jobs = db.relationship('ProcessingJob', backref='episode', lazy=True, cascade='all, delete-orphan')
    
    # Unique constraint on podcast_id + guid
    __table_args__ = (db.UniqueConstraint('podcast_id', 'guid', name='unique_podcast_episode'),)
    
    def __repr__(self):
        return f'<Episode {self.title}>'

class ProcessingJob(db.Model):
    __tablename__ = 'processing_jobs'
    
    id = db.Column(db.Integer, primary_key=True)
    episode_id = db.Column(db.Integer, db.ForeignKey('episodes.id'), nullable=False)
    job_type = db.Column(db.String(50), nullable=False)  # download, transcribe, summarize
    status = db.Column(db.String(50), default='pending')  # pending, processing, completed, failed
    started_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    error_message = db.Column(db.Text)
    
    # Celery task ID for tracking
    celery_task_id = db.Column(db.String(255))
    
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def __repr__(self):
        return f'<ProcessingJob {self.job_type} for Episode {self.episode_id}>'

class EmailNotification(db.Model):
    __tablename__ = 'email_notifications'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    episode_id = db.Column(db.Integer, db.ForeignKey('episodes.id'), nullable=False)
    email_type = db.Column(db.String(50), nullable=False)  # transcript_ready, summary_ready
    status = db.Column(db.String(50), default='pending')  # pending, sent, failed
    sent_at = db.Column(db.DateTime(timezone=True))
    error_message = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    user = db.relationship('User', backref='email_notifications')
    episode = db.relationship('Episode', backref='email_notifications')
    
    def __repr__(self):
        return f'<EmailNotification {self.email_type} for Episode {self.episode_id}>'