Advanced Auto-MM Discord Bot

Environment variables

DISCORD_TOKEN=your_discord_bot_token
GROQ_API_KEY=your_groq_api_key

Install

pip install -r requirements.txt

Start

python bot.py

First Discord setup

Invite the bot with Bot + applications.commands permissions.

Enable MESSAGE CONTENT INTENT and SERVER MEMBERS INTENT in the Discord Developer Portal.

Run $setup.

Configure:
$setcategory #your-ticket-category
$setlogs #your-log-channel
$setmmrole @Middleman
$setstaffrole @Staff
$setpanel #your-panel-channel

Run $mm if you want to post the panel manually.

Main commands

$mm
$ai
$risk
$tradeinfo
$assign @user
$verify payment
$verify item
$finish
$canceltrade
$transcript
$mystats
$mmlist
$vouch @mm 5 Great MM
$help

The AI is an assistant. It flags suspicious behavior and recommends actions; human middlemen retain final control over trade completion and moderation.
