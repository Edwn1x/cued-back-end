# Baseline MVP

An AI fitness and nutrition coach that lives in your text messages.

## Quick Start

### 1. Prerequisites
- Python 3.10+
- A Twilio account (twilio.com — free trial gives you $15 credit)
- An Anthropic API key (console.anthropic.com)

### 2. Setup

```bash
# Clone / download this project
cd baseline

# Install dependencies
pip install -r requirements.txt

# Copy env template and fill in your keys
cp .env.example .env
# Edit .env with your Twilio and Anthropic credentials

# Initialize the database
python models.py
```

### 3. Configure Twilio

1. Sign up at twilio.com and get a phone number
2. In Twilio console, go to your phone number's settings
3. Under "Messaging", set the webhook URL:
   - **When a message comes in:** `https://your-app-url.com/webhook`
   - **HTTP Method:** POST

For local development, use ngrok to expose your local server:
```bash
# In a separate terminal
ngrok http 5000
# Copy the https URL and set it as your Twilio webhook
```

### 4. Run

```bash
python app.py
```

The app runs on port 5000 with three endpoints:
- `GET /` — Health check
- `GET /signup` — User signup form
- `POST /webhook` — Twilio SMS webhook
- `GET /admin` — Admin dashboard (see all users + conversations)
- `POST /admin/send` — Manual message override

### 5. Sign Up Your First User

Open `http://localhost:5000/signup` in a browser, fill in the form,
and you'll receive a welcome text. Your first morning briefing arrives
at whatever wake time you set.

## Project Structure

```
baseline/
├── app.py              # Flask app, webhook, signup, admin
├── coach.py            # LLM prompt construction + API calls
├── models.py           # Database models (SQLAlchemy)
├── sms.py              # Twilio send/receive helpers
├── scheduler.py        # APScheduler for timed messages
├── config.py           # Environment config
├── prompts/
│   └── system_prompt.txt   # The coaching system prompt
├── requirements.txt
├── .env.example
└── README.md
```

## How It Works

1. **User signs up** via the web form → stored in DB → daily messages scheduled
2. **Scheduler fires** at each user's configured times → generates coaching
   message via Claude API → sends via Twilio SMS
3. **User replies** → Twilio forwards to webhook → message classified →
   AI generates response with full conversation context → response sent
4. **Admin dashboard** lets you monitor all conversations and manually
   override when the AI gets something wrong

## Key Files to Customize

- `prompts/system_prompt.txt` — The coaching personality and rules.
  This is where 80% of the product quality lives. Tweak it constantly.
- `scheduler.py` — Adjust message timing and which touchpoints fire.
- `coach.py` → `classify_message()` — Improve message classification
  as you see real user patterns.

## Deployment

### Replit (easiest)
1. Create a new Python Repl
2. Upload all files
3. Add secrets (env vars) in Replit's Secrets tab
4. Run `python app.py`
5. Use the Replit URL as your Twilio webhook

### Railway ($5/month)
```bash
# Install Railway CLI
railway login
railway init
railway up
```
Add env vars in Railway dashboard.

## Costs (for 30 beta users)

| Item | Monthly Cost |
|------|-------------|
| Twilio SMS (~450 msgs/user) | ~$35 |
| Claude API (Haiku) | ~$5-10 |
| Hosting (Replit/Railway) | $0-7 |
| **Total** | **~$40-52** |
