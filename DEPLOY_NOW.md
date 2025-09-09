# 🚀 DEPLOY TO HEROKU NOW - Step by Step

Everything is ready! Follow these exact commands to deploy your Podcast Transcription Web Service to Heroku.

## ✅ Pre-Deployment Checklist

All required files are ready:
- ✅ `app.py` - Flask application
- ✅ `requirements.txt` - Python dependencies  
- ✅ `Procfile` - Heroku process definitions
- ✅ `runtime.txt` - Python version
- ✅ `gunicorn.conf.py` - Production server config
- ✅ `heroku-setup.sh` - Automated deployment script
- ✅ Templates and static files for web interface

## 🎯 STEP 1: Get Your API Keys Ready

Before deploying, get these API keys:

### OpenAI API Key
1. Go to: https://platform.openai.com/api-keys
2. Click "Create new secret key"  
3. Copy the key (starts with `sk-`)

### Stripe Keys  
1. Go to: https://dashboard.stripe.com/
2. Get your **Publishable Key** (`pk_test_` or `pk_live_`)
3. Get your **Secret Key** (`sk_test_` or `sk_live_`)
4. Create a subscription product and get the **Price ID** (`price_`)

### Mailgun Credentials
1. Go to: https://app.mailgun.com/
2. Get your **API Key** 
3. Get your verified **Domain**

## 🚀 STEP 2: Deploy to Heroku

### Option A: Run the Automated Script (EASIEST)

```bash
# Make sure you're in the project directory
cd /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb

# Run the automated setup (replace 'my-podcast-app' with your preferred name)
./heroku-setup.sh my-podcast-transcription

# If you get permission denied:
chmod +x heroku-setup.sh
./heroku-setup.sh my-podcast-transcription
```

### Option B: Manual Commands

If the script doesn't work, run these commands:

```bash
# 1. Navigate to project directory
cd /media/wdphillips/BACKUP_A1/PodcastTranscriptionWeb

# 2. Login to Heroku (if not already logged in)
heroku login

# 3. Create the app (replace with your app name)
heroku create my-podcast-transcription

# 4. Add required addons
heroku addons:create heroku-postgresql:mini -a my-podcast-transcription
heroku addons:create heroku-redis:mini -a my-podcast-transcription

# 5. Set basic configuration
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
heroku config:set SECRET_KEY="$SECRET_KEY" -a my-podcast-transcription
heroku config:set FLASK_ENV=production -a my-podcast-transcription
heroku config:set PYTHONPATH=/app -a my-podcast-transcription
heroku config:set WHISPER_MODEL=medium -a my-podcast-transcription
heroku config:set OPENAI_MODEL=gpt-4o-mini -a my-podcast-transcription
heroku config:set DEFAULT_SUMMARY_PROVIDER=openai -a my-podcast-transcription
heroku config:set MAX_CONCURRENT=2 -a my-podcast-transcription
heroku config:set LOG_LEVEL=INFO -a my-podcast-transcription

# 6. Set up git remote
heroku git:remote -a my-podcast-transcription
```

## 🔑 STEP 3: Add Your API Keys

Replace the placeholder values with your actual API keys:

```bash
# OpenAI
heroku config:set OPENAI_API_KEY=sk-your-actual-openai-key-here -a my-podcast-transcription

# Stripe
heroku config:set STRIPE_PUBLISHABLE_KEY=pk_test_your-actual-key -a my-podcast-transcription  
heroku config:set STRIPE_SECRET_KEY=sk_test_your-actual-key -a my-podcast-transcription
heroku config:set STRIPE_PRICE_ID=price_your-actual-price-id -a my-podcast-transcription

# Mailgun
heroku config:set MAILGUN_API_KEY=your-actual-mailgun-key -a my-podcast-transcription
heroku config:set MAILGUN_DOMAIN=your-verified-domain.com -a my-podcast-transcription
```

## 📦 STEP 4: Deploy the Code

```bash
# 1. Initialize git if not already done
git init

# 2. Add all files
git add .

# 3. Commit
git commit -m "Initial deployment to Heroku"

# 4. Push to Heroku (this triggers the build)
git push heroku main

# Or if your branch is called master:
git push heroku master
```

## 🗄️ STEP 5: Initialize Database

```bash
# Create database tables
heroku run python migrations.py -a my-podcast-transcription
```

## ⚙️ STEP 6: Scale Workers

```bash
# Start web and worker dynos
heroku ps:scale web=1 worker=1 -a my-podcast-transcription
```

## 🔗 STEP 7: Configure Stripe Webhook

1. Go to: https://dashboard.stripe.com/webhooks
2. Click "Add endpoint"
3. Enter URL: `https://my-podcast-transcription.herokuapp.com/webhook/stripe`
4. Select event: `checkout.session.completed`
5. Copy the signing secret (starts with `whsec_`)
6. Add it to Heroku:

```bash
heroku config:set STRIPE_WEBHOOK_SECRET=whsec_your-signing-secret -a my-podcast-transcription
```

## ✅ STEP 8: Test Your Deployment

```bash
# Check if app is running
heroku open -a my-podcast-transcription

# View logs
heroku logs --tail -a my-podcast-transcription

# Check dyno status
heroku ps -a my-podcast-transcription
```

## 🎉 SUCCESS!

Your app should now be live at: `https://my-podcast-transcription.herokuapp.com`

### Quick Test:
1. Visit your app URL
2. Register a new account  
3. Try the subscription flow
4. Add a podcast URL
5. Check that background processing works

## 🐛 If Something Goes Wrong

### Build Failed?
```bash
# Check build logs
heroku logs --tail -a my-podcast-transcription

# Common fix: missing dependencies
git add requirements.txt
git commit -m "Fix requirements"
git push heroku main
```

### App Won't Start?
```bash
# Check for startup errors
heroku logs --tail -a my-podcast-transcription

# Restart app
heroku restart -a my-podcast-transcription
```

### Database Issues?
```bash
# Re-run database setup
heroku run python migrations.py -a my-podcast-transcription

# Check database connection
heroku pg:info -a my-podcast-transcription
```

### Worker Not Processing?
```bash
# Check worker status
heroku ps -a my-podcast-transcription

# Restart worker
heroku ps:restart worker -a my-podcast-transcription

# Check worker logs
heroku logs --ps worker -a my-podcast-transcription
```

## 💰 Monthly Costs

- Web dyno: $7/month
- Worker dyno: $7/month  
- PostgreSQL mini: $9/month
- Redis mini: $3/month
- **Total: ~$26/month**

## 🔄 Making Updates

After deployment, to update your app:

```bash
# 1. Make changes to your code
# 2. Commit changes
git add .
git commit -m "Update description"

# 3. Deploy
git push heroku main

# 4. Check logs
heroku logs --tail -a my-podcast-transcription
```

---

**Need Help?** Check the logs first, then review the detailed [DEPLOYMENT.md](DEPLOYMENT.md) guide.

**Ready to deploy?** Run: `./heroku-setup.sh my-podcast-transcription`