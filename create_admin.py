#!/usr/bin/env python3
"""
Script to create admin user for the podcast transcription application.
"""

import os
from flask import Flask
from werkzeug.security import generate_password_hash
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')

# Fix DATABASE_URL for SQLAlchemy 1.4+ and Heroku
database_url = os.environ.get('DATABASE_URL', 'postgresql://localhost/podcast_transcription')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg2://', 1)
elif database_url.startswith('postgresql://') and '+psycopg2' not in database_url:
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Import models and initialize db
from models import db, User, Subscription

db.init_app(app)

def create_admin_user():
    """Create admin user with specified credentials."""
    
    with app.app_context():
        # Check if admin user already exists
        admin_email = "wdphillips@gmail.com"
        existing_user = User.query.filter_by(email=admin_email).first()
        
        if existing_user:
            print(f"Admin user {admin_email} already exists!")
            return
        
        # Create admin user
        admin_user = User(
            email=admin_email,
            password_hash=generate_password_hash("D@vid"),
            created_at=datetime.now(timezone.utc)
        )
        
        db.session.add(admin_user)
        db.session.commit()
        
        # Create active subscription for admin user
        subscription = Subscription(
            user_id=admin_user.id,
            stripe_subscription_id="admin_sub_" + str(admin_user.id),
            status='active',
            created_at=datetime.now(timezone.utc)
        )
        
        db.session.add(subscription)
        db.session.commit()
        
        print(f"✅ Admin user created successfully!")
        print(f"   Email: {admin_email}")
        print(f"   Password: D@vid")
        print(f"   Subscription: Active")

if __name__ == '__main__':
    create_admin_user()