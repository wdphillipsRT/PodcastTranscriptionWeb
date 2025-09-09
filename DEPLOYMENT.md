# 🚀 Heroku Deployment Guide

This guide will walk you through deploying your Podcast Transcription Web Service to Heroku step by step.

## 📋 Prerequisites

Before you begin, ensure you have:

- [ ] **Heroku CLI installed** - [Download here](https://devcenter.heroku.com/articles/heroku-cli)
- [ ] **Git installed** and project committed
- [ ] **OpenAI API key** - [Get one here](https://platform.openai.com/api-keys)
- [ ] **Stripe account** - [Sign up here](https://dashboard.stripe.com/register)
- [ ] **Mailgun account** - [Sign up here](https://signup.mailgun.com/)

## 🎯 Step 1: API Setup

### OpenAI Setup
1. Go to [OpenAI API Keys](https://platform.openai.com/api-keys)
2. Create a new API key
3. Save it securely (starts with `sk-...`)

### Stripe Setup
1. Go to [Stripe Dashboard](https://dashboard.stripe.com/)
2. Get your **Publishable Key** (`pk_test_...` or `pk_live_...`)
3. Get your **Secret Key** (`sk_test_...` or `sk_live_...`)
4. Create a **Subscription Product**:
   - Go to Products → Create Product
   - Name: "Podcast Transcription Subscription"
   - Billing: Recurring monthly
   - Price: $9.99/month
   - Copy the **Price ID** (starts with `price_...`)
5. Set up **Webhooks** (we'll configure the URL after deployment)

### Mailgun Setup
1. Go to [Mailgun Dashboard](https://app.mailgun.com/)
2. Add and verify your domain
3. Get your **API Key** and **Domain** from the dashboard

## 🚀 Step 2: Deploy to Heroku

### Option A: Automated Setup (Recommended)

Run our automated setup script:

```bash
# Make sure you're in the project directory
cd PodcastTranscriptionWeb

# Run the automated setup
./heroku-setup.sh my-podcast-app

# Follow the prompts for Heroku login if needed
```

### Option B: Manual Setup

If you prefer manual control:

```bash
# 1. Create Heroku app
heroku create my-podcast-app

# 2. Add addons
heroku addons:create heroku-postgresql:mini -a my-podcast-app
heroku addons:create heroku-redis:mini -a my-podcast-app

# 3. Set basic config
SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
heroku config:set SECRET_KEY="$SECRET_KEY" -a my-podcast-app
heroku config:set FLASK_ENV=production -a my-podcast-app
heroku config:set PYTHONPATH=/app -a my-podcast-app

# 4. Set git remote
heroku git:remote -a my-podcast-app
```

## 🔑 Step 3: Configure Environment Variables

Set your API keys (replace with your actual keys):

```bash
# OpenAI Configuration
heroku config:set OPENAI_API_KEY=sk-your-openai-key-here -a my-podcast-app
heroku config:set OPENAI_MODEL=gpt-4o-mini -a my-podcast-app
heroku config:set WHISPER_MODEL=medium -a my-podcast-app

# Stripe Configuration
heroku config:set STRIPE_PUBLISHABLE_KEY=pk_test_your-key-here -a my-podcast-app
heroku config:set STRIPE_SECRET_KEY=sk_test_your-key-here -a my-podcast-app
heroku config:set STRIPE_PRICE_ID=price_your-price-id-here -a my-podcast-app

# Mailgun Configuration
heroku config:set MAILGUN_API_KEY=your-mailgun-key-here -a my-podcast-app
heroku config:set MAILGUN_DOMAIN=your-verified-domain.com -a my-podcast-app

# Optional: Performance tuning
heroku config:set MAX_CONCURRENT=2 -a my-podcast-app
heroku config:set LOG_LEVEL=INFO -a my-podcast-app
```

## 📦 Step 4: Deploy Your Code

```bash
# 1. Commit any changes
git add .
git commit -m "Deploy to Heroku"

# 2. Push to Heroku
git push heroku main

# 3. Initialize database
heroku run python migrations.py -a my-podcast-app

# 4. Scale workers
heroku ps:scale web=1 worker=1 -a my-podcast-app
```

## 🔗 Step 5: Complete Stripe Webhook Setup

1. Go to [Stripe Webhooks](https://dashboard.stripe.com/webhooks)
2. Click "Add endpoint"
3. Set URL to: `https://my-podcast-app.herokuapp.com/webhook/stripe`
4. Select events: `checkout.session.completed`
5. Copy the **Signing Secret** (starts with `whsec_...`)
6. Add it to Heroku:

```bash
heroku config:set STRIPE_WEBHOOK_SECRET=whsec_your-signing-secret -a my-podcast-app
```

## ✅ Step 6: Test Your Deployment

1. **Visit your app**: `https://my-podcast-app.herokuapp.com`
2. **Check logs**: `heroku logs --tail -a my-podcast-app`
3. **Test registration** and **subscription flow**
4. **Add a test podcast** to verify processing

## 🐛 Troubleshooting

### Common Issues

**1. Build Failed**
```bash
# Check build logs
heroku logs --tail -a my-podcast-app

# Common fix: ensure all dependencies are in requirements.txt
pip freeze > requirements.txt
git add requirements.txt
git commit -m "Update requirements"
git push heroku main
```

**2. Database Connection Error**
```bash
# Check if PostgreSQL addon is attached
heroku addons -a my-podcast-app

# Recreate database tables
heroku run python migrations.py -a my-podcast-app
```

**3. Worker Not Processing Jobs**
```bash
# Check worker status
heroku ps -a my-podcast-app

# Restart workers
heroku ps:restart worker -a my-podcast-app

# Check worker logs
heroku logs --ps worker -a my-podcast-app
```

**4. Email Not Sending**
```bash
# Verify Mailgun configuration
heroku config:get MAILGUN_API_KEY -a my-podcast-app
heroku config:get MAILGUN_DOMAIN -a my-podcast-app

# Check if domain is verified in Mailgun dashboard
```

**5. Stripe Payments Not Working**
```bash
# Test webhook endpoint
curl -X POST https://my-podcast-app.herokuapp.com/webhook/stripe

# Check Stripe webhook logs in dashboard
# Verify webhook secret is correctly set
```

## 📊 Monitoring

### View Logs
```bash
# All logs
heroku logs --tail -a my-podcast-app

# Specific dyno
heroku logs --ps worker -a my-podcast-app
heroku logs --ps web -a my-podcast-app

# Search logs
heroku logs --grep "ERROR" -a my-podcast-app
```

### Database Access
```bash
# Connect to PostgreSQL
heroku pg:psql -a my-podcast-app

# Useful queries
SELECT * FROM users;
SELECT * FROM subscriptions;
SELECT * FROM episodes WHERE status = 'processing';
```

### Performance Monitoring
```bash
# View dyno usage
heroku ps -a my-podcast-app

# Scale dynos
heroku ps:scale web=2 worker=2 -a my-podcast-app
```

## 💰 Cost Optimization

### Heroku Costs
- **Basic dyno**: $7/month per dyno
- **PostgreSQL mini**: $9/month
- **Redis mini**: $3/month
- **Total base cost**: ~$19-26/month

### Scaling Strategy
```bash
# Development: Minimal setup
heroku ps:scale web=1 worker=0 -a my-podcast-app

# Production: Full processing
heroku ps:scale web=1 worker=1 -a my-podcast-app

# High traffic: Scale up
heroku ps:scale web=2 worker=2 -a my-podcast-app
```

## 🔄 Continuous Deployment

Set up automatic deployments from GitHub:

1. Go to your Heroku app dashboard
2. Navigate to "Deploy" tab
3. Connect to GitHub repository
4. Enable "Automatic deploys" from main branch
5. Enable "Wait for CI to pass" if you have tests

## 🔒 Security Checklist

- [ ] All API keys are set as environment variables
- [ ] Stripe webhook signature validation is enabled
- [ ] HTTPS is enforced (automatic on Heroku)
- [ ] Database uses connection pooling
- [ ] Error messages don't expose sensitive information
- [ ] User passwords are properly hashed

## 📈 Next Steps

1. **Set up monitoring** with services like LogDNA or Datadog
2. **Add automated tests** and CI/CD pipeline
3. **Implement caching** with Redis for better performance
4. **Add user analytics** to track usage patterns
5. **Optimize costs** by scheduling worker dynos

---

🎉 **Congratulations!** Your Podcast Transcription Web Service is now live on Heroku!

Your app is available at: `https://my-podcast-app.herokuapp.com`

For support, check the logs first, then refer to the [main README](README.md) or open an issue on GitHub.