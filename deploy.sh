#!/bin/bash

# Heroku Deployment Script for Podcast Transcription Web App

set -e

echo "🚀 Starting Heroku deployment setup..."

# Check if Heroku CLI is installed
if ! command -v heroku &> /dev/null; then
    echo "❌ Heroku CLI not found. Please install it first."
    exit 1
fi

# Create Heroku app if it doesn't exist
APP_NAME=${1:-podcast-transcription-web}
echo "📱 Creating Heroku app: $APP_NAME"

heroku create $APP_NAME 2>/dev/null || echo "App $APP_NAME already exists"

# Add required buildpacks
echo "🔧 Adding buildpacks..."
heroku buildpacks:clear -a $APP_NAME
heroku buildpacks:add heroku/python -a $APP_NAME

# Add PostgreSQL addon
echo "🗄️ Adding PostgreSQL addon..."
heroku addons:create heroku-postgresql:mini -a $APP_NAME

# Add Redis addon for Celery
echo "📦 Adding Redis addon..."
heroku addons:create heroku-redis:mini -a $APP_NAME

# Set environment variables
echo "⚙️ Setting environment variables..."

# Generate a secret key
SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
heroku config:set SECRET_KEY="$SECRET_KEY" -a $APP_NAME

# Set Flask environment
heroku config:set FLASK_ENV=production -a $APP_NAME
heroku config:set PYTHONPATH=/app -a $APP_NAME

# Set default models
heroku config:set WHISPER_MODEL=medium -a $APP_NAME
heroku config:set OPENAI_MODEL=gpt-4o-mini -a $APP_NAME
heroku config:set DEFAULT_SUMMARY_PROVIDER=openai -a $APP_NAME
heroku config:set MAX_CONCURRENT=2 -a $APP_NAME
heroku config:set LOG_LEVEL=INFO -a $APP_NAME

echo "⚠️  IMPORTANT: You need to manually set these config vars in Heroku:"
echo "   - OPENAI_API_KEY"
echo "   - STRIPE_PUBLISHABLE_KEY"
echo "   - STRIPE_SECRET_KEY" 
echo "   - STRIPE_WEBHOOK_SECRET"
echo "   - STRIPE_PRICE_ID"
echo "   - MAILGUN_API_KEY"
echo "   - MAILGUN_DOMAIN"

echo ""
echo "Run these commands to set the API keys:"
echo "heroku config:set OPENAI_API_KEY=sk-your-key-here -a $APP_NAME"
echo "heroku config:set STRIPE_PUBLISHABLE_KEY=pk_your-key-here -a $APP_NAME"
echo "heroku config:set STRIPE_SECRET_KEY=sk_your-key-here -a $APP_NAME"
echo "heroku config:set STRIPE_WEBHOOK_SECRET=whsec_your-key-here -a $APP_NAME"
echo "heroku config:set STRIPE_PRICE_ID=price_your-price-id -a $APP_NAME"
echo "heroku config:set MAILGUN_API_KEY=your-mailgun-key -a $APP_NAME"
echo "heroku config:set MAILGUN_DOMAIN=your-domain.com -a $APP_NAME"

# Set up git remote
echo "🔗 Setting up git remote..."
heroku git:remote -a $APP_NAME

echo ""
echo "✅ Deployment setup complete!"
echo ""
echo "Next steps:"
echo "1. Set the required API keys using the commands above"
echo "2. Commit your changes: git add . && git commit -m 'Initial web app version'"
echo "3. Deploy: git push heroku main"
echo "4. Run database migrations: heroku run python migrations.py -a $APP_NAME"
echo "5. Scale workers: heroku ps:scale worker=1 -a $APP_NAME"
echo ""
echo "Your app will be available at: https://$APP_NAME.herokuapp.com"