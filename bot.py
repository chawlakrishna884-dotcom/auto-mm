
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands
from groq import AsyncGroq

# ============================================================
# ADVANCED AUTO-MM BOT
# Prefix: $
# AI: Groq
# Database: SQLite
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PREFIX = "$"
DB_FILE = "mm_bot.db"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True
)

groq = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(DB_FILE)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")

db.executescript("""
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    ticket_category INTEGER,
    log_channel INTEGER,
    mm_role INTEGER,
    staff_role INTEGER,
    panel_channel INTEGER,
    transcript_channel INTEGER,
    ai_enabled INTEGER DEFAULT 1,
    auto_claim INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER UNIQUE,
    buyer_id INTEGER,
    seller_id INTEGER,
    mm_id INTEGER,
    buyer_offer TEXT DEFAULT '',
    seller_offer TEXT DEFAULT '',
    status TEXT DEFAULT 'OPEN',
    risk TEXT DEFAULT 'UNKNOWN',
    risk_reason TEXT DEFAULT '',
    buyer_confirmed INTEGER DEFAULT 0,
    seller_confirmed INTEGER DEFAULT 0,
    payment_verified INTEGER DEFAULT 0,
    item_verified INTEGER DEFAULT 0,
    created_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS trade_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    user_id INTEGER,
    username TEXT,
    content TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS mms (
    guild_id INTEGER,
    user_id INTEGER,
    trades INTEGER DEFAULT 0,
    successful INTEGER DEFAULT 0,
    cancelled INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS vouches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    mm_id INTEGER,
    user_id INTEGER,
    rating INTEGER,
    comment TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    user_id INTEGER,
    reason TEXT,
    severity TEXT,
    created_at TEXT
);
""")
db.commit()


def now():
    return datetime.now(timezone.utc).isoformat()


def execute(sql, params=(), fetch=False):
    cur = db.execute(sql, params)
    db.commit()
    return cur.fetchall() if fetch else cur.lastrowid


def get_config(guild_id):
    row = db.execute(
        "SELECT * FROM guild_config WHERE guild_id=?",
        (guild_id,)
    ).fetchone()
    if not row:
        execute(
            "INSERT INTO guild_config (guild_id) VALUES (?)",
            (guild_id,)
        )
        row = db.execute(
            "SELECT * FROM guild_config WHERE guild_id=?",
            (guild_id,)
        ).fetchone()
    return row


def get_trade(channel_id):
    return db.execute(
        "SELECT * FROM trades WHERE channel_id=?",
        (channel_id,)
    ).fetchone()


def get_trade_by_id(trade_id):
    return db.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,)
    ).fetchone()


def is_staff(member):
    cfg = get_config(member.guild.id)
    if member.guild.owner_id == member.id:
        return True
    if member.guild_permissions.administrator:
        return True
    ids = [cfg["staff_role"], cfg["mm_role"]]
    return any(role.id in ids for role in member.roles if role.id)


def is_mm(member):
    cfg = get_config(member.guild.id)
    if member.guild.owner_id == member.id or member.guild_permissions.administrator:
        return True
    return cfg["mm_role"] and any(r.id == cfg["mm_role"] for r in member.roles)


async def send_log(guild, title, description, color=discord.Color.blurple()):
    cfg = get_config(guild.id)
    channel_id = cfg["log_channel"]
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not channel:
        return
    embed = discord.Embed(
        title=title,
        description=description[:4000],
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


# ============================================================
# EMBEDS / UI
# ============================================================

def trade_embed(trade):
    status = trade["status"]
    risk = trade["risk"]

    risk_icon = {
        "LOW": "🟢",
        "MEDIUM": "🟡",
        "HIGH": "🔴",
        "CRITICAL": "🚨",
        "UNKNOWN": "⚪"
    }.get(risk, "⚪")

    embed = discord.Embed(
        title=f"🤝 MM Trade #{trade['id']}",
        color=discord.Color.green() if status == "OPEN" else discord.Color.red()
    )
    embed.add_field(name="Status", value=f"`{status}`", inline=True)
    embed.add_field(name="AI Risk", value=f"{risk_icon} `{risk}`", inline=True)

    buyer = f"<@{trade['buyer_id']}>" if trade["buyer_id"] else "Not set"
    seller = f"<@{trade['seller_id']}>" if trade["seller_id"] else "Not set"
    mm = f"<@{trade['mm_id']}>" if trade["mm_id"] else "Unassigned"

    embed.add_field(name="🧑 Buyer", value=buyer, inline=True)
    embed.add_field(name="👤 Seller", value=seller, inline=True)
    embed.add_field(name="🛡️ Middleman", value=mm, inline=True)

    embed.add_field(
        name="📦 Buyer Gives",
        value=trade["buyer_offer"] or "Not provided",
        inline=False
    )
    embed.add_field(
        name="🎁 Seller Gives",
        value=trade["seller_offer"] or "Not provided",
        inline=False
    )

    checks = (
        f"Buyer confirmation: {'✅' if trade['buyer_confirmed'] else '⏳'}\n"
        f"Seller confirmation: {'✅' if trade['seller_confirmed'] else '⏳'}\n"
        f"Payment verified: {'✅' if trade['payment_verified'] else '⏳'}\n"
        f"Item verified: {'✅' if trade['item_verified'] else '⏳'}"
    )
    embed.add_field(name="🔐 Trade Checks", value=checks, inline=False)

    if trade["risk_reason"]:
        embed.add_field(
            name="🧠 AI Notes",
            value=trade["risk_reason"][:1000],
            inline=False
        )

    embed.set_footer(text="Auto-MM • AI assists the human middleman")
    return embed


class MMPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start MM Trade",
        style=discord.ButtonStyle.green,
        emoji="🤝",
        custom_id="mm_start_trade"
    )
    async def start_trade(self, interaction: discord.Interaction, button):
        await interaction.response.send_modal(TradeSetupModal())


class TradeSetupModal(discord.ui.Modal, title="Start Middleman Trade"):
    buyer = discord.ui.TextInput(
        label="Buyer",
        placeholder="Discord ID or @mention",
        required=True,
        max_length=100
    )
    seller = discord.ui.TextInput(
        label="Seller",
        placeholder="Discord ID or @mention",
        required=True,
        max_length=100
    )
    buyer_offer = discord.ui.TextInput(
        label="Buyer gives",
        placeholder="Example: $25 / Roblox item",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph
    )
    seller_offer = discord.ui.TextInput(
        label="Seller gives",
        placeholder="Example: Dragon",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True
            )

        buyer_id = parse_user_id(self.buyer.value)
        seller_id = parse_user_id(self.seller.value)

        if not buyer_id or not seller_id:
            return await interaction.response.send_message(
                "❌ I couldn't understand one of the user IDs/mentions.",
                ephemeral=True
            )

        buyer = guild.get_member(buyer_id)
        seller = guild.get_member(seller_id)

        if not buyer or not seller:
            return await interaction.response.send_message(
                "❌ Both users must be in this server.",
                ephemeral=True
            )

        if buyer.id == seller.id:
            return await interaction.response.send_message(
                "❌ Buyer and seller must be different users.",
                ephemeral=True
            )

        cfg = get_config(guild.id)
        category = guild.get_channel(cfg["ticket_category"]) if cfg["ticket_category"] else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            buyer: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            seller: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        }

        if cfg["mm_role"]:
            role = guild.get_role(cfg["mm_role"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True
                )

        if cfg["staff_role"]:
            role = guild.get_role(cfg["staff_role"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True
                )

        channel_name = f"mm-{buyer.name[:10]}-{seller.name[:10]}".lower()
        channel_name = re.sub(r"[^a-z0-9-]", "", channel_name)[:90]

        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            overwrites=overwrites,
            topic="Auto-MM trade ticket"
        )

        trade_id = execute(
            """INSERT INTO trades
            (guild_id, channel_id, buyer_id, seller_id, buyer_offer, seller_offer, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                guild.id,
                channel.id,
                buyer.id,
                seller.id,
                self.buyer_offer.value,
                self.seller_offer.value,
                now()
            )
        )

        trade = get_trade_by_id(trade_id)

        embed = trade_embed(trade)
        embed.description = (
            "Welcome to the **Auto-MM trade room**.\n\n"
            "🛡️ A human middleman should control the final exchange.\n"
            "🧠 AI will monitor the conversation for inconsistencies and suspicious behavior.\n\n"
            "Please do **not** send passwords, cookies, tokens, recovery codes, or other sensitive credentials."
        )

        await channel.send(
            content=f"{buyer.mention} {seller.mention}",
            embed=embed,
            view=TradeControls()
        )

        await interaction.response.send_message(
            f"✅ Trade #{trade_id} created: {channel.mention}",
            ephemeral=True
        )

        await send_log(
            guild,
            "🤝 MM Trade Created",
            f"Trade #{trade_id}\nBuyer: {buyer.mention}\nSeller: {seller.mention}\nCreated by: {interaction.user.mention}",
            discord.Color.green()
        )


class TradeControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim MM",
        style=discord.ButtonStyle.primary,
        emoji="🛡️",
        custom_id="trade_claim_mm"
    )
    async def claim(self, interaction, button):
        trade = get_trade(interaction.channel.id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ You don't have the Middleman role.",
                ephemeral=True
            )

        if trade["mm_id"] and trade["mm_id"] != interaction.user.id:
            return await interaction.response.send_message(
                "❌ This trade already has a middleman.",
                ephemeral=True
            )

        execute(
            "UPDATE trades SET mm_id=? WHERE id=?",
            (interaction.user.id, trade["id"])
        )
        await interaction.response.send_message(
            f"🛡️ {interaction.user.mention} is now the middleman.",
            ephemeral=False
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Confirm Trade",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="trade_confirm"
    )
    async def confirm(self, interaction, button):
        trade = get_trade(interaction.channel.id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if interaction.user.id == trade["buyer_id"]:
            execute(
                "UPDATE trades SET buyer_confirmed=1 WHERE id=?",
                (trade["id"],)
            )
        elif interaction.user.id == trade["seller_id"]:
            execute(
                "UPDATE trades SET seller_confirmed=1 WHERE id=?",
                (trade["id"],)
            )
        else:
            return await interaction.response.send_message(
                "Only the buyer or seller can confirm.",
                ephemeral=True
            )

        await interaction.response.send_message(
            "✅ Your confirmation has been recorded.",
            ephemeral=True
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Verify Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳",
        custom_id="trade_verify_payment"
    )
    async def payment(self, interaction, button):
        trade = get_trade(interaction.channel.id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ Only an MM can verify payment.",
                ephemeral=True
            )

        execute(
            "UPDATE trades SET payment_verified=1 WHERE id=?",
            (trade["id"],)
        )
        await interaction.response.send_message(
            "💳 Payment marked as verified by the MM.",
            ephemeral=False
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Verify Item",
        style=discord.ButtonStyle.secondary,
        emoji="📦",
        custom_id="trade_verify_item"
    )
    async def item(self, interaction, button):
        trade = get_trade(interaction.channel.id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ Only an MM can verify the item.",
                ephemeral=True
            )

        execute(
            "UPDATE trades SET item_verified=1 WHERE id=?",
            (trade["id"],)
        )
        await interaction.response.send_message(
            "📦 Item marked as verified by the MM.",
            ephemeral=False
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Complete",
        style=discord.ButtonStyle.success,
        emoji="🏁",
        custom_id="trade_complete"
    )
    async def complete(self, interaction, button):
        trade = get_trade(interaction.channel.id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ Only an MM can complete a trade.",
                ephemeral=True
            )

        if not (
            trade["buyer_confirmed"]
            and trade["seller_confirmed"]
            and trade["payment_verified"]
            and trade["item_verified"]
        ):
            return await interaction.response.send_message(
                "⚠️ All four checks must be complete before finishing this trade.",
                ephemeral=True
            )

        execute(
            "UPDATE trades SET status='COMPLETED', closed_at=? WHERE id=?",
            (now(), trade["id"])
        )

        if trade["mm_id"]:
            execute(
                """UPDATE mms
                   SET trades=trades+1, successful=successful+1
                   WHERE guild_id=? AND user_id=?""",
                (trade["guild_id"], trade["mm_id"])
            )

        await interaction.response.send_message(
            "🏁 **Trade completed successfully.** This ticket will close shortly.",
            ephemeral=False
        )

        await send_log(
            interaction.guild,
            "🏁 Trade Completed",
            f"Trade #{trade['id']} completed by {interaction.user.mention}.",
            discord.Color.green()
        )

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Completed MM trade #{trade['id']}")
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="trade_cancel"
    )
    async def cancel(self, interaction, button):
        trade = get_trade(interaction.channel.id)
        if not trade:
            return await interaction.response.send_message("Trade not found.", ephemeral=True)

        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff/MM can cancel a trade.",
                ephemeral=True
            )

        execute(
            "UPDATE trades SET status='CANCELLED', closed_at=? WHERE id=?",
            (now(), trade["id"])
        )

        if trade["mm_id"]:
            execute(
                """UPDATE mms
                   SET trades=trades+1, cancelled=cancelled+1
                   WHERE guild_id=? AND user_id=?""",
                (trade["guild_id"], trade["mm_id"])
            )

        await interaction.response.send_message(
            "❌ Trade cancelled. The ticket will close shortly."
        )

        await send_log(
            interaction.guild,
            "❌ Trade Cancelled",
            f"Trade #{trade['id']} cancelled by {interaction.user.mention}.",
            discord.Color.red()
        )

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Cancelled MM trade #{trade['id']}")
        except discord.HTTPException:
            pass


async def refresh_trade_message(channel):
    trade = get_trade(channel.id)
    if not trade:
        return
    async for message in channel.history(limit=20):
        if message.author.id == bot.user.id and message.embeds:
            try:
                await message.edit(embed=trade_embed(trade), view=TradeControls())
                return
            except discord.HTTPException:
                return


# ============================================================
# AI
# ============================================================

AI_SYSTEM = """
You are the AI safety assistant for a Discord Roblox middleman trading server.
You do NOT control money, items, accounts, bans, kicks, or final trade approval.
You assist a human middleman.

Analyze only the provided conversation.
Look for:
- changing trade terms
- contradictions
- fake payment claims
- pressure to skip the MM
- suspicious links
- requests for passwords, cookies, tokens, recovery codes
- impersonation
- attempts to move the trade outside the MM
- scam-like behavior
- missing confirmations

Return JSON only:
{
  "risk": "LOW|MEDIUM|HIGH|CRITICAL",
  "reason": "short explanation",
  "next_action": "short recommended action",
  "flags": ["flag1", "flag2"]
}
"""


async def ai_analyze_trade(trade_id):
    if not groq:
        return {
            "risk": "UNKNOWN",
            "reason": "Groq is not configured.",
            "next_action": "Human MM should review the trade.",
            "flags": []
        }

    messages = db.execute(
        """SELECT username, content, created_at
           FROM trade_messages
           WHERE trade_id=?
           ORDER BY id DESC LIMIT 80""",
        (trade_id,)
    ).fetchall()

    if not messages:
        return {
            "risk": "LOW",
            "reason": "No conversation history yet.",
            "next_action": "Continue normal MM verification.",
            "flags": []
        }

    conversation = "\n".join(
        f"[{m['username']}] {m['content']}" for m in reversed(messages)
    )

    prompt = f"""
Trade #{trade_id}

Conversation:
{conversation[-12000:]}
"""

    try:
        result = await groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            temperature=0.1,
            max_tokens=500,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": AI_SYSTEM},
                {"role": "user", "content": prompt}
            ]
        )

        data = json.loads(result.choices[0].message.content)

        risk = str(data.get("risk", "UNKNOWN")).upper()
        if risk not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            risk = "UNKNOWN"

        return {
            "risk": risk,
            "reason": str(data.get("reason", ""))[:1500],
            "next_action": str(data.get("next_action", ""))[:1000],
            "flags": data.get("flags", [])[:10]
        }

    except Exception as e:
        return {
            "risk": "UNKNOWN",
            "reason": f"AI request failed: {type(e).__name__}",
            "next_action": "Human MM should review the trade.",
            "flags": []
        }


async def run_ai_for_trade(channel, trade):
    result = await ai_analyze_trade(trade["id"])

    execute(
        """UPDATE trades
           SET risk=?, risk_reason=?
           WHERE id=?""",
        (result["risk"], result["reason"], trade["id"])
    )

    for flag in result["flags"]:
        execute(
            """INSERT INTO flags
               (trade_id, user_id, reason, severity, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                trade["id"],
                trade["buyer_id"],
                str(flag)[:500],
                result["risk"],
                now()
            )
        )

    if result["risk"] in {"HIGH", "CRITICAL"}:
        await channel.send(
            f"🚨 **AI TRADE ALERT — {result['risk']}**\n"
            f"**Reason:** {result['reason']}\n"
            f"**Recommended action:** {result['next_action']}\n"
            f"⚠️ AI does not make the final decision. A human MM must review this.",
            allowed_mentions=discord.AllowedMentions.none()
        )

        await send_log(
            channel.guild,
            f"🚨 AI Flagged Trade #{trade['id']}",
            f"Risk: {result['risk']}\nReason: {result['reason']}\nAction: {result['next_action']}",
            discord.Color.red()
        )

    await refresh_trade_message(channel)


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    bot.add_view(MMPanel())
    bot.add_view(TradeControls())
    print(f"Logged in as {bot.user} ({bot.user.id})")
    print("Advanced Auto-MM bot is online.")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild:
        trade = get_trade(message.channel.id)

        if trade and trade["status"] == "OPEN":
            content = message.content.strip()

            # Never store obvious credentials/tokens.
            sensitive = re.compile(
                r"(password|cookie|token|recovery\s*code|2fa\s*code|private\s*key)",
                re.I
            )

            safe_content = (
                "[REDACTED SENSITIVE MESSAGE]"
                if sensitive.search(content)
                else content
            )

            if safe_content:
                execute(
                    """INSERT INTO trade_messages
                       (trade_id, user_id, username, content, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        trade["id"],
                        message.author.id,
                        str(message.author),
                        safe_content[:2000],
                        now()
                    )
                )

            # Lightweight immediate safety warning.
            if sensitive.search(content):
                await message.reply(
                    "🛡️ **Security warning:** Never send passwords, cookies, "
                    "tokens, recovery codes, or other account credentials in an MM ticket."
                )

            # AI is intentionally throttled to avoid an API call on every message.
            count = db.execute(
                "SELECT COUNT(*) AS c FROM trade_messages WHERE trade_id=?",
                (trade["id"],)
            ).fetchone()["c"]

            if groq and count % 8 == 0:
                asyncio.create_task(run_ai_for_trade(message.channel, trade))

    await bot.process_commands(message)


# ============================================================
# HELP / PANEL
# ============================================================

@bot.command()
async def mm(ctx):
    """Send the public MM panel."""
    embed = discord.Embed(
        title="🤝 Advanced Middleman Service",
        description=(
            "Use the button below to start a protected trade.\n\n"
            "🧠 AI monitors the conversation for suspicious behavior.\n"
            "🛡️ Human middlemen perform final verification.\n"
            "📋 Every trade gets a persistent trade record.\n"
            "🔐 Never share passwords, cookies, tokens, or recovery codes."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Trade Flow",
        value="Start → Verify Users → Claim MM → Verify Payment/Item → Both Confirm → Complete",
        inline=False
    )
    await ctx.send(embed=embed, view=MMPanel())


@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🤖 Auto-MM Commands",
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="Member",
        value=(
            "`$mm` — MM panel\n"
            "`$mystats` — your MM stats\n"
            "`$vouch @mm 1-5 <comment>` — vouch\n"
            "`$tradeinfo` — current trade information"
        ),
        inline=False
    )
    embed.add_field(
        name="MM / Staff",
        value=(
            "`$ai` — analyze current trade\n"
            "`$risk` — show current AI risk\n"
            "`$assign @user` — assign MM\n"
            "`$verify payment` — verify payment\n"
            "`$verify item` — verify item\n"
            "`$finish` — complete eligible trade\n"
            "`$canceltrade` — cancel current trade\n"
            "`$transcript` — create transcript"
        ),
        inline=False
    )
    embed.add_field(
        name="Admin",
        value=(
            "`$setup` — show setup commands\n"
            "`$setcategory #category`\n"
            "`$setlogs #channel`\n"
            "`$setmmrole @role`\n"
            "`$setstaffrole @role`\n"
            "`$setpanel #channel`\n"
            "`$mmlist` — MM leaderboard"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


# ============================================================
# ADMIN SETUP
# ============================================================

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    await ctx.send(
        "⚙️ **Auto-MM setup**\n\n"
        "`$setcategory #category` — ticket category\n"
        "`$setlogs #channel` — trade/AI logs\n"
        "`$setmmrole @role` — middleman role\n"
        "`$setstaffrole @role` — staff role\n"
        "`$setpanel #channel` — MM panel channel\n"
        "`$mm` — send panel manually"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def setcategory(ctx, category: discord.CategoryChannel):
    execute(
        "UPDATE guild_config SET ticket_category=? WHERE guild_id=?",
        (category.id, ctx.guild.id)
    )
    await ctx.send(f"✅ Ticket category set to {category.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlogs(ctx, channel: discord.TextChannel):
    execute(
        "UPDATE guild_config SET log_channel=? WHERE guild_id=?",
        (channel.id, ctx.guild.id)
    )
    await ctx.send(f"✅ Log channel set to {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setmmrole(ctx, role: discord.Role):
    execute(
        "UPDATE guild_config SET mm_role=? WHERE guild_id=?",
        (role.id, ctx.guild.id)
    )
    await ctx.send(f"✅ MM role set to {role.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setstaffrole(ctx, role: discord.Role):
    execute(
        "UPDATE guild_config SET staff_role=? WHERE guild_id=?",
        (role.id, ctx.guild.id)
    )
    await ctx.send(f"✅ Staff role set to {role.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setpanel(ctx, channel: discord.TextChannel):
    execute(
        "UPDATE guild_config SET panel_channel=? WHERE guild_id=?",
        (channel.id, ctx.guild.id)
    )
    await channel.send(
        "🤝 **Official Middleman Panel**\n"
        "Start a protected trade below.",
        embed=discord.Embed(
            title="Advanced Auto-MM",
            description="AI-assisted trade protection with human MM verification.",
            color=discord.Color.blurple()
        ),
        view=MMPanel()
    )
    await ctx.send(f"✅ MM panel posted in {channel.mention}")


# ============================================================
# TRADE COMMANDS
# ============================================================

@bot.command()
async def tradeinfo(ctx):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ This channel is not an MM trade ticket.")
    await ctx.send(embed=trade_embed(trade))


@bot.command()
async def ai(ctx):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Use this command inside an MM ticket.")

    if not is_staff(ctx.author):
        return await ctx.send("❌ Only MM/staff can request an AI analysis.")

    msg = await ctx.send("🧠 Analyzing the current trade...")
    result = await ai_analyze_trade(trade["id"])

    execute(
        "UPDATE trades SET risk=?, risk_reason=? WHERE id=?",
        (result["risk"], result["reason"], trade["id"])
    )

    embed = discord.Embed(
        title=f"🧠 AI Analysis — Trade #{trade['id']}",
        color={
            "LOW": discord.Color.green(),
            "MEDIUM": discord.Color.gold(),
            "HIGH": discord.Color.orange(),
            "CRITICAL": discord.Color.red()
        }.get(result["risk"], discord.Color.greyple())
    )
    embed.add_field(name="Risk", value=result["risk"], inline=True)
    embed.add_field(name="Reason", value=result["reason"][:1000], inline=False)
    embed.add_field(
        name="Recommended Action",
        value=result["next_action"][:1000],
        inline=False
    )

    flags = "\n".join(f"• {x}" for x in result["flags"]) or "No specific flags."
    embed.add_field(name="Flags", value=flags[:1000], inline=False)
    embed.set_footer(text="AI recommendation only — human MM makes the final decision.")

    await msg.edit(content="", embed=embed)


@bot.command()
async def risk(ctx):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Not an MM ticket.")
    await ctx.send(
        f"🧠 Current AI risk for Trade #{trade['id']}: "
        f"**{trade['risk']}**\n{trade['risk_reason'] or 'No AI analysis yet.'}"
    )


@bot.command()
async def assign(ctx, member: discord.Member):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Not an MM ticket.")
    if not is_staff(ctx.author):
        return await ctx.send("❌ Staff/MM only.")
    if not is_mm(member):
        return await ctx.send("❌ That user doesn't have the MM role.")

    execute(
        "UPDATE trades SET mm_id=? WHERE id=?",
        (member.id, trade["id"])
    )
    await ctx.send(f"🛡️ {member.mention} has been assigned to Trade #{trade['id']}.")
    await refresh_trade_message(ctx.channel)


@bot.command()
async def verify(ctx, kind: str):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Not an MM ticket.")
    if not is_mm(ctx.author):
        return await ctx.send("❌ MM only.")

    kind = kind.lower()
    if kind == "payment":
        execute("UPDATE trades SET payment_verified=1 WHERE id=?", (trade["id"],))
        await ctx.send("💳 Payment verified.")
    elif kind == "item":
        execute("UPDATE trades SET item_verified=1 WHERE id=?", (trade["id"],))
        await ctx.send("📦 Item verified.")
    else:
        await ctx.send("Use `$verify payment` or `$verify item`.")
        return

    await refresh_trade_message(ctx.channel)


@bot.command()
async def finish(ctx):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Not an MM ticket.")
    if not is_mm(ctx.author):
        return await ctx.send("❌ MM only.")

    if not (
        trade["buyer_confirmed"]
        and trade["seller_confirmed"]
        and trade["payment_verified"]
        and trade["item_verified"]
    ):
        return await ctx.send("⚠️ All confirmations and verification checks are required.")

    execute(
        "UPDATE trades SET status='COMPLETED', closed_at=? WHERE id=?",
        (now(), trade["id"])
    )

    if trade["mm_id"]:
        execute(
            """UPDATE mms
               SET trades=trades+1, successful=successful+1
               WHERE guild_id=? AND user_id=?""",
            (trade["guild_id"], trade["mm_id"])
        )

    await ctx.send("🏁 **Trade completed successfully.**")
    await send_log(
        ctx.guild,
        "🏁 Trade Completed",
        f"Trade #{trade['id']} completed by {ctx.author.mention}.",
        discord.Color.green()
    )


@bot.command()
async def canceltrade(ctx):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Not an MM ticket.")
    if not is_staff(ctx.author):
        return await ctx.send("❌ Staff/MM only.")

    execute(
        "UPDATE trades SET status='CANCELLED', closed_at=? WHERE id=?",
        (now(), trade["id"])
    )
    await ctx.send("❌ Trade cancelled. Staff should review the reason before closing.")
    await send_log(
        ctx.guild,
        "❌ Trade Cancelled",
        f"Trade #{trade['id']} cancelled by {ctx.author.mention}.",
        discord.Color.red()
    )


# ============================================================
# STATS / VOUCHES
# ============================================================

@bot.command()
async def mystats(ctx):
    row = db.execute(
        "SELECT * FROM mms WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, ctx.author.id)
    ).fetchone()

    if not row:
        return await ctx.send("You don't have MM statistics yet.")

    await ctx.send(
        f"🛡️ **MM Statistics — {ctx.author.display_name}**\n"
        f"Trades: **{row['trades']}**\n"
        f"Successful: **{row['successful']}**\n"
        f"Cancelled: **{row['cancelled']}**"
    )


@bot.command()
async def mmlist(ctx):
    rows = db.execute(
        """SELECT user_id, trades, successful, cancelled
           FROM mms WHERE guild_id=?
           ORDER BY successful DESC, trades DESC LIMIT 15""",
        (ctx.guild.id,)
    ).fetchall()

    if not rows:
        return await ctx.send("No MM statistics yet.")

    lines = []
    for i, row in enumerate(rows, 1):
        lines.append(
            f"**{i}.** <@{row['user_id']}> — "
            f"{row['successful']} successful / {row['trades']} total"
        )

    embed = discord.Embed(
        title="🏆 Middleman Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    await ctx.send(embed=embed)


@bot.command()
async def vouch(ctx, member: discord.Member, rating: int, *, comment=""):
    if rating < 1 or rating > 5:
        return await ctx.send("Rating must be between 1 and 5.")

    cfg = get_config(ctx.guild.id)
    if not cfg["mm_role"] or not any(r.id == cfg["mm_role"] for r in member.roles):
        return await ctx.send("❌ That user is not configured as an MM.")

    execute(
        """INSERT INTO vouches
           (guild_id, mm_id, user_id, rating, comment, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ctx.guild.id, member.id, ctx.author.id, rating, comment[:1000], now())
    )

    await ctx.send(
        f"⭐ Vouch recorded for {member.mention}: **{rating}/5**"
    )


# ============================================================
# TRANSCRIPT
# ============================================================

@bot.command()
async def transcript(ctx):
    trade = get_trade(ctx.channel.id)
    if not trade:
        return await ctx.send("❌ Not an MM ticket.")
    if not is_staff(ctx.author):
        return await ctx.send("❌ Staff/MM only.")

    messages = []
    async for m in ctx.channel.history(limit=500, oldest_first=True):
        if not m.author.bot:
            messages.append(
                f"[{m.created_at.isoformat()}] {m.author} ({m.author.id}): {m.content}"
            )

    text = (
        f"MM TRADE #{trade['id']}\n"
        f"Status: {trade['status']}\n"
        f"Buyer: {trade['buyer_id']}\n"
        f"Seller: {trade['seller_id']}\n"
        f"MM: {trade['mm_id']}\n"
        f"AI Risk: {trade['risk']}\n"
        f"AI Notes: {trade['risk_reason']}\n\n"
        + "\n".join(messages)
    )

    filename = f"transcript-{trade['id']}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(text)

    await ctx.send(
        f"📋 Transcript for Trade #{trade['id']}",
        file=discord.File(filename)
    )

    try:
        os.remove(filename)
    except OSError:
        pass


# ============================================================
# UTILITIES / ERROR HANDLING
# ============================================================

def parse_user_id(value):
    value = value.strip()
    match = re.search(r"<@!?(\d+)>", value)
    if match:
        return int(match.group(1))
    if value.isdigit():
        return int(value)
    return None


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("❌ You don't have permission to use this command.")

    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(f"❌ Missing argument: `{error.param.name}`")

    if isinstance(error, commands.BadArgument):
        return await ctx.send("❌ Invalid user/channel/role argument.")

    if isinstance(error, commands.CheckFailure):
        return await ctx.send("❌ You don't have permission to use this command.")

    print(f"Command error: {repr(error)}")
    await ctx.send("❌ An unexpected error occurred. Check the bot console.")


# ============================================================
# START
# ============================================================

bot.run(TOKEN)
