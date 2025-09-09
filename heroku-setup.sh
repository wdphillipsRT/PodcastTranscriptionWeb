#!/bin/bash

# Complete Heroku Setup Script for Podcast Transcription Web App
# This script will create and configure everything needed for Heroku deployment

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default app name
APP_NAME=${1:-"podcast-transcription-$(date +%s)"}

echo -e "${GREEN}🚀 Starting Heroku deployment for: $APP_NAME${NC}"

# Check if Heroku CLI is installed
if ! command -v heroku &> /dev/null; then
    echo -e "${RED}❌ Heroku CLI not found. Please install it first:${NC}"
    echo "https://devcenter.heroku.com/articles/heroku-cli"
    exit 1
fi

# Check if logged in to Heroku
echo -e "${YELLOW}🔐 Checking Heroku authentication...${NC}"
if ! heroku auth:whoami &> /dev/null; then
    echo -e "${YELLOW}Please log in to Heroku:${NC}"
    heroku login
fi

echo -e "${GREEN}✅ Heroku authentication successful${NC}"

# Create the app
echo -e "${YELLOW}📱 Creating Heroku app: $APP_NAME${NC}"
if heroku create $APP_NAME 2>/dev/null; then
    echo -e "${GREEN}✅ App created successfully${NC}"
else
    echo -e "${YELLOW}⚠️  App $APP_NAME already exists or name taken. Continuing...${NC}"
fi

# Set the app for all subsequent commands
export HEROKU_APP=$APP_NAME

# Configure buildpacks
echo -e "${YELLOW}🔧 Setting up buildpacks...${NC}"
heroku buildpacks:clear -a $APP_NAME
heroku buildpacks:add heroku/python -a $APP_NAME

# Add PostgreSQL addon
echo -e "${YELLOW}🗄️  Adding PostgreSQL database...${NC}"
if heroku addons:create heroku-postgresql:mini -a $APP_NAME 2>/dev/null; then
    echo -e "${GREEN}✅ PostgreSQL added${NC}"
else
    echo -e "${YELLOW}⚠️  PostgreSQL might already exist${NC}"
fi

# Add Redis addon for Celery
echo -e "${YELLOW}📦 Adding Redis for background tasks...${NC}"
if heroku addons:create heroku-redis:mini -a $APP_NAME 2>/dev/null; then
    echo -e "${GREEN}✅ Redis added${NC}"
else
    echo -e "${YELLOW}⚠️  Redis might already exist${NC}"
fi

# Generate and set secret key
echo -e "${YELLOW}🔑 Setting up application configuration...${NC}"
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
heroku config:set SECRET_KEY="$SECRET_KEY" -a $APP_NAME

# Set Flask environment
heroku config:set FLASK_ENV=production -a $APP_NAME
heroku config:set PYTHONPATH=/app -a $APP_NAME

# Set default AI model configurations
heroku config:set WHISPER_MODEL=medium -a $APP_NAME
heroku config:set OPENAI_MODEL=gpt-4o-mini -a $APP_NAME
heroku config:set DEFAULT_SUMMARY_PROVIDER=openai -a $APP_NAME
heroku config:set MAX_CONCURRENT=2 -a $APP_NAME
heroku config:set LOG_LEVEL=INFO -a $APP_NAME

echo -e "${GREEN}✅ Basic configuration complete${NC}"

# Set up git remote
echo -e "${YELLOW}🔗 Setting up git remote...${NC}"
if git remote get-url heroku &> /dev/null; then
    git remote remove heroku
fi
heroku git:remote -a $APP_NAME

echo -e "${GREEN}✅ Git remote configured${NC}"

# Display next steps
echo -e "${GREEN}"
echo "==============================================="
echo "🎉 HEROKU APP CREATED SUCCESSFULLY!"
echo "==============================================="
echo -e "${NC}"
echo "App Name: $APP_NAME"
echo "URL: https://$APP_NAME.herokuapp.com"
echo ""
echo -e "${YELLOW}🔧 REQUIRED: Set these environment variables with your API keys:${NC}"
echo ""
echo "heroku config:set OPENAI_API_KEY=sk-your-openai-key-here -a $APP_NAME"
echo "heroku config:set STRIPE_PUBLISHABLE_KEY=pk_live_your-stripe-key -a $APP_NAME"
echo "heroku config:set STRIPE_SECRET_KEY=sk_live_your-stripe-key -a $APP_NAME"
echo "heroku config:set STRIPE_WEBHOOK_SECRET=whsec_your-webhook-secret -a $APP_NAME"
echo "heroku config:set STRIPE_PRICE_ID=price_your-subscription-price-id -a $APP_NAME"
echo "heroku config:set MAILGUN_API_KEY=your-mailgun-api-key -a $APP_NAME"
echo "heroku config:set MAILGUN_DOMAIN=your-verified-domain.com -a $APP_NAME"
echo ""
echo -e "${YELLOW}📋 NEXT STEPS:${NC}"
echo "1. Set the required API keys above"
echo "2. Commit your code: git add . && git commit -m 'Deploy to Heroku'"
echo "3. Deploy: git push heroku main"
echo "4. Initialize database: heroku run python migrations.py -a $APP_NAME"
echo "5. Scale worker: heroku ps:scale worker=1 -a $APP_NAME"
echo "6. View logs: heroku logs --tail -a $APP_NAME"
echo ""
echo -e "${GREEN}🎯 Your Heroku app is ready for deployment!${NC}"
echo "Remember to set up your Stripe webhook URL: https://$APP_NAME.herokuapp.com/webhook/stripe"