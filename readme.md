Advanced Auto-MM Bot

Discord middleman bot using discord.py, Groq AI and SQLite.

Required Railway Variables

DISCORD_TOKEN=your_discord_bot_token
GROQ_API_KEY=your_groq_api_key

Optional Variables

GROQ_MODEL=llama-3.3-70b-versatile
PREFIX=$
AI_EVERY_MESSAGES=6
LOG_LEVEL=INFO

Railway SQLite

Attach a Railway Volume and mount it at:

/data

The bot automatically uses:

/data/mm_bot.db

Discord Permissions

The bot needs enough permissions to:

View channels

Send messages

Read message history

Manage channels

Manage messages

Embed links

Attach files

Mention users

Use application commands

Enable these Gateway Intents in the Discord Developer Portal:

Server Members Intent

Message Content Intent

First server setup

$setup
$setcategory #your-category
$setlogs #your-log-channel
$setmmrole @Middleman
$setstaffrole @Staff
$setpanel #your-panel-channel
$setai on
$setaievery 6

Then use:

$mm

or:

/mm
