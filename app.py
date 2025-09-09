from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone
import os
import stripe
import requests
from dotenv import load_dotenv
import logging

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'postgresql://localhost/podcast_transcription')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Fix DATABASE_URL for SQLAlchemy 1.4+
if 'postgresql://' in app.config['SQLALCHEMY_DATABASE_URI']:
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgresql://', 'postgresql+psycopg2://')

# Import models and initialize db
from models import db, User, Subscription, Podcast, Episode, ProcessingJob, EmailNotification
db.init_app(app)

# Initialize other extensions
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Configure Stripe
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        email = data.get('email')
        password = data.get('password')
        
        if User.query.filter_by(email=email).first():
            if request.is_json:
                return jsonify({'error': 'Email already registered'}), 400
            flash('Email already registered')
            return redirect(url_for('register'))
        
        user = User(email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        
        login_user(user)
        if request.is_json:
            return jsonify({'success': True, 'redirect': url_for('dashboard')})
        return redirect(url_for('dashboard'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json() if request.is_json else request.form
        email = data.get('email')
        password = data.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            if request.is_json:
                return jsonify({'success': True, 'redirect': url_for('dashboard')})
            return redirect(url_for('dashboard'))
        
        if request.is_json:
            return jsonify({'error': 'Invalid credentials'}), 401
        flash('Invalid credentials')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/dashboard')
@login_required
def dashboard():
    podcasts = Podcast.query.filter_by(user_id=current_user.id).all()
    recent_episodes = Episode.query.join(Podcast).filter(
        Podcast.user_id == current_user.id
    ).order_by(Episode.created_at.desc()).limit(10).all()
    
    return render_template('dashboard.html', 
                         podcasts=podcasts, 
                         recent_episodes=recent_episodes,
                         user=current_user)

@app.route('/add-podcast', methods=['POST'])
@login_required
def add_podcast():
    if not current_user.has_active_subscription():
        return jsonify({'error': 'Active subscription required'}), 403
    
    data = request.get_json()
    podcast_url = data.get('url')
    name = data.get('name')
    
    # Check if podcast already exists for this user
    existing = Podcast.query.filter_by(user_id=current_user.id, url=podcast_url).first()
    if existing:
        return jsonify({'error': 'Podcast already added'}), 400
    
    # Resolve RSS feed from URL
    try:
        from feed_resolver import resolve_feed
        rss_url = resolve_feed(podcast_url)
        
        podcast = Podcast(
            user_id=current_user.id,
            name=name,
            url=podcast_url,
            rss_url=rss_url,
            created_at=datetime.now(timezone.utc)
        )
        
        db.session.add(podcast)
        db.session.commit()
        
        # Queue episodes for processing
        from tasks import queue_podcast_processing
        queue_podcast_processing.delay(podcast.id)
        
        return jsonify({'success': True, 'podcast_id': podcast.id})
        
    except Exception as e:
        logger.error(f"Error adding podcast: {e}")
        return jsonify({'error': 'Failed to add podcast'}), 500

@app.route('/subscription')
@login_required
def subscription():
    return render_template('subscription.html', 
                         stripe_key=STRIPE_PUBLISHABLE_KEY,
                         user=current_user)

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price': os.environ.get('STRIPE_PRICE_ID'),
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('subscription_success', _external=True),
            cancel_url=url_for('subscription', _external=True),
            client_reference_id=str(current_user.id),
        )
        return jsonify({'checkout_url': checkout_session.url})
    except Exception as e:
        logger.error(f"Error creating checkout session: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/subscription/success')
@login_required
def subscription_success():
    flash('Subscription activated successfully!')
    return redirect(url_for('dashboard'))

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET')
    
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': 'Invalid payload'}), 400
    
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        user_id = int(session['client_reference_id'])
        subscription_id = session['subscription']
        
        user = User.query.get(user_id)
        if user:
            subscription = Subscription(
                user_id=user_id,
                stripe_subscription_id=subscription_id,
                status='active',
                created_at=datetime.now(timezone.utc)
            )
            db.session.add(subscription)
            db.session.commit()
            
            # Send welcome email
            from tasks import send_welcome_email
            send_welcome_email.delay(user.email)
    
    return jsonify({'success': True})

@app.route('/api/episodes/<int:episode_id>')
@login_required
def get_episode(episode_id):
    episode = Episode.query.join(Podcast).filter(
        Episode.id == episode_id,
        Podcast.user_id == current_user.id
    ).first_or_404()
    
    return jsonify({
        'id': episode.id,
        'title': episode.title,
        'transcript_path': episode.transcript_path,
        'summary_path': episode.summary_path,
        'status': episode.status,
        'created_at': episode.created_at.isoformat() if episode.created_at else None
    })

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)