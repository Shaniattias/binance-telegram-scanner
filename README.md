# Binance Telegram Scanner on Render

Files:
- app.py
- requirements.txt
- render.yaml
- .gitignore

Render environment variables:
- TELEGRAM_BOT_TOKEN = your current BotFather token
- TELEGRAM_CHAT_ID = your Telegram chat ID

After deploy:
- Render automatically provides RENDER_EXTERNAL_URL.
- The app automatically registers a Telegram webhook at /telegram/webhook.
- Send /start, /status, or /scan to your Telegram bot.

Free Render note:
The service can sleep after 15 minutes without inbound traffic. The next Telegram command wakes it, so the first command after a sleep can be delayed.
