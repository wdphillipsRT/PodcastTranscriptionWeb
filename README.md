# 🎙️ Podcast Transcription Web Service

A comprehensive web application that automatically downloads, transcribes, and summarizes podcast episodes with AI. Built with Flask, PostgreSQL, Celery, Stripe payments, and Mailgun email notifications.

## ✨ Features

- **🔐 User Authentication**: Secure user registration and login
- **💳 Stripe Integration**: Monthly subscription payments  
- **🎙️ Podcast Management**: Add podcasts via Apple Podcasts URLs or RSS feeds
- **🤖 AI Transcription**: High-quality transcription using OpenAI Whisper API
- **📝 AI Summarization**: Intelligent summaries with key takeaways and quotes
- **📧 Email Notifications**: Automatic delivery of transcripts and summaries via Mailgun
- **🔄 Background Processing**: Async task processing with Celery and Redis
- **☁️ Heroku Ready**: Production deployment configuration included
- **📱 Responsive UI**: Mobile-friendly Bootstrap interface

## 🏗️ Architecture

- **Frontend**: Flask with Bootstrap 5 and vanilla JavaScript
- **Backend**: Flask with SQLAlchemy ORM
- **Database**: PostgreSQL (Heroku Postgres)
- **Task Queue**: Celery with Redis
- **Payments**: Stripe Checkout and webhooks
- **Email**: Mailgun API
- **AI Services**: OpenAI Whisper API (transcription) and GPT models (summarization)
- **Deployment**: Heroku with multiple worker dynos

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL
- Redis
- Heroku CLI
- API keys for OpenAI, Stripe, and Mailgun

### Local Development

1. **Clone and setup**
   ```bash
   git clone <your-repo>
   cd PodcastTranscriptionWeb
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   # or venv\\Scripts\\activate  # Windows
   pip install -r requirements.txt
   ```

2. **Environment setup**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys and database URL
   ```

3. **Database setup**
   ```bash
   # Ensure PostgreSQL is running
   createdb podcast_transcription
   python migrations.py
   ```

4. **Start services**
   ```bash
   # Terminal 1: Flask app
   flask run
   
   # Terminal 2: Celery worker
   celery -A tasks.celery_app worker --loglevel=info
   
   # Terminal 3: Redis (if not running as service)
   redis-server
   ```

### Heroku Deployment

1. **Run deployment script**
   ```bash
   chmod +x deploy.sh
   ./deploy.sh your-app-name
   ```

2. **Set API keys**
   ```bash
   heroku config:set OPENAI_API_KEY=sk-your-key -a your-app-name
   heroku config:set STRIPE_PUBLISHABLE_KEY=pk_your-key -a your-app-name
   heroku config:set STRIPE_SECRET_KEY=sk_your-key -a your-app-name
   heroku config:set STRIPE_WEBHOOK_SECRET=whsec_your-secret -a your-app-name
   heroku config:set STRIPE_PRICE_ID=price_your-price-id -a your-app-name
   heroku config:set MAILGUN_API_KEY=your-mailgun-key -a your-app-name
   heroku config:set MAILGUN_DOMAIN=your-domain.com -a your-app-name
   ```

3. **Deploy**
   ```bash
   git add .
   git commit -m "Initial deployment"
   git push heroku main
   ```

4. **Initialize database and scale workers**
   ```bash
   heroku run python migrations.py -a your-app-name
   heroku ps:scale worker=1 -a your-app-name
   ```

## 🔧 Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | PostgreSQL connection string | Yes |
| `REDIS_URL` | Redis connection string | Yes |
| `SECRET_KEY` | Flask secret key | Yes |
| `OPENAI_API_KEY` | OpenAI API key | Yes |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key | Yes |
| `STRIPE_SECRET_KEY` | Stripe secret key | Yes |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook secret | Yes |
| `STRIPE_PRICE_ID` | Stripe subscription price ID | Yes |
| `MAILGUN_API_KEY` | Mailgun API key | Yes |
| `MAILGUN_DOMAIN` | Mailgun domain | Yes |
| `WHISPER_MODEL` | OpenAI Whisper model (whisper-1) | No (default: whisper-1) |
| `OPENAI_MODEL` | OpenAI model for summaries | No (default: gpt-4o-mini) |
| `MAX_CONCURRENT` | Max concurrent tasks | No (default: 2) |

### Stripe Setup

1. Create a Stripe account and get your API keys
2. Create a subscription product and price in Stripe Dashboard
3. Set up a webhook endpoint pointing to `/webhook/stripe`
4. Configure webhook to listen for `checkout.session.completed` events

### Mailgun Setup

1. Create a Mailgun account and verify your domain
2. Get your API key and domain from Mailgun dashboard
3. Update email templates in `templates/emails/` with your domain

## 📊 Database Schema

The application uses PostgreSQL with the following main tables:

- **users**: User accounts and authentication
- **subscriptions**: Stripe subscription data
- **podcasts**: User's podcast subscriptions
- **episodes**: Individual podcast episodes
- **processing_jobs**: Background task tracking
- **email_notifications**: Email delivery logs

## 🔄 Background Processing

The app uses Celery for background task processing:

### Queue Structure
- **downloads**: Audio file downloading
- **transcription**: OpenAI Whisper API transcription processing  
- **summarization**: AI summary generation
- **emails**: Email notifications

### Task Flow
1. User adds podcast → Episodes discovered
2. Download audio → Queue transcription
3. Transcribe audio → Email transcript → Queue summarization
4. Generate summary → Email summary
5. Mark episode as completed

## 🎨 Frontend Structure

- **templates/**: Jinja2 HTML templates
- **static/css/**: Custom CSS and Bootstrap overrides
- **static/js/**: JavaScript for interactivity
- **templates/emails/**: HTML email templates

## 🧪 Testing

```bash
# Run tests (when implemented)
pytest tests/

# Test Stripe webhooks locally
stripe listen --forward-to localhost:5000/webhook/stripe
```

## 📈 Monitoring & Logs

### Heroku Logs
```bash
heroku logs --tail -a your-app-name
heroku logs --ps worker -a your-app-name  # Worker logs
```

### Database Access
```bash
heroku pg:psql -a your-app-name
```

### Celery Monitoring
Use Flower for Celery monitoring (not included but can be added):
```bash
pip install flower
celery -A tasks.celery_app flower
```

## 🔐 Security Considerations

- All API keys stored in environment variables
- User passwords hashed with Werkzeug
- CSRF protection via Flask-WTF (recommended addition)
- SQL injection protection via SQLAlchemy ORM
- Stripe webhooks verified with signature validation
- HTTPS enforced in production

## 🎯 Usage Flow

1. **User Registration**: Create account with email/password
2. **Subscription**: Pay via Stripe Checkout
3. **Add Podcasts**: Paste Apple Podcasts or RSS feed URLs
4. **Automatic Processing**: 
   - System fetches latest episodes
   - Downloads and transcribes audio
   - Generates AI summaries
   - Emails results to user
5. **Dashboard**: View processing status and episode history

## 🛠️ Development

### Adding New Features

1. **Database changes**: Update models in `models.py`
2. **Background tasks**: Add to `tasks.py` with proper error handling
3. **API endpoints**: Add routes to `app.py`
4. **Frontend**: Update templates and static files
5. **Email templates**: Add to `templates/emails/`

### Code Structure

```
PodcastTranscriptionWeb/
├── app.py                 # Main Flask application
├── models.py             # Database models  
├── tasks.py              # Celery background tasks
├── celeryconfig.py       # Celery configuration
├── migrations.py         # Database setup script
├── requirements.txt      # Python dependencies
├── Procfile             # Heroku process definitions
├── runtime.txt          # Python version for Heroku
├── deploy.sh            # Deployment automation script
├── templates/           # HTML templates
├── static/             # CSS, JS, images
└── templates/emails/   # Email templates
```

## 📝 API Endpoints

- `GET /` - Landing page
- `POST /register` - User registration
- `POST /login` - User login
- `GET /dashboard` - User dashboard
- `POST /add-podcast` - Add new podcast
- `GET /subscription` - Subscription management
- `POST /create-checkout-session` - Stripe checkout
- `POST /webhook/stripe` - Stripe webhook handler
- `GET /api/episodes/<id>` - Episode details API

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🙏 Acknowledgments

- OpenAI for Whisper API and GPT models
- Stripe for payment processing
- Mailgun for email delivery
- Heroku for cloud hosting

---

For support or questions, please open an issue on GitHub.