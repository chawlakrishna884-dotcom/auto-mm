import os
import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands
from groq import AsyncGroq

# ============================================================
# ADVANCED AUTO-MM BOT
# Discord.py + Groq + SQLite
# Designed for Railway
#
# Required environment variables:
#   DISCORD_TOKEN
#   GROQ_API_KEY
#
# Optional:
#   GROQ_MODEL=llama-3.3-70b-versatile
#   PREFIX=$
#   DB_PATH=/data/mm_bot.db
#   AI_EVERY_MESSAGES=6
#   LOG_LEVEL=INFO
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PREFIX = os.getenv("PREFIX", "$")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "mm_bot.db")
)
AI_EVERY_MESSAGES = max(3, int(os.getenv("AI_EVERY_MESSAGES", "6")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is missing.")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY environment variable is missing.")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("advanced-mm")

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
    case_insensitive=True,
)

groq = AsyncGroq(api_key=GROQ_API_KEY)

# ============================================================
# DATABASE
# ============================================================

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA busy_timeout=5000")

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
    auto_claim INTEGER DEFAULT 0,
    ai_every INTEGER DEFAULT 6
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
    next_action TEXT DEFAULT '',
    buyer_confirmed INTEGER DEFAULT 0,
    seller_confirmed INTEGER DEFAULT 0,
    payment_verified INTEGER DEFAULT 0,
    item_verified INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
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

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    actor_id INTEGER,
    action TEXT,
    details TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_guild_status
ON trades(guild_id, status);

CREATE INDEX IF NOT EXISTS idx_messages_trade
ON trade_messages(trade_id, id);

CREATE INDEX IF NOT EXISTS idx_flags_trade
ON flags(trade_id, id);
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
        (guild_id,),
    ).fetchone()

    if not row:
        execute(
            "INSERT INTO guild_config (guild_id) VALUES (?)",
            (guild_id,),
        )
        row = db.execute(
            "SELECT * FROM guild_config WHERE guild_id=?",
            (guild_id,),
        ).fetchone()

    return row


def update_trade(trade_id, **values):
    if not values:
        return
    values["updated_at"] = now()
    fields = ", ".join(f"{key}=?" for key in values)
    params = list(values.values()) + [trade_id]
    execute(f"UPDATE trades SET {fields} WHERE id=?", params)


def get_trade(channel_id):
    return db.execute(
        "SELECT * FROM trades WHERE channel_id=?",
        (channel_id,),
    ).fetchone()


def get_trade_by_id(trade_id):
    return db.execute(
        "SELECT * FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()


def get_open_trade_for_user(guild_id, user_id):
    return db.execute(
        """
        SELECT * FROM trades
        WHERE guild_id=? AND status='OPEN'
        AND (buyer_id=? OR seller_id=?)
        ORDER BY id DESC LIMIT 1
        """,
        (guild_id, user_id, user_id),
    ).fetchone()


def audit(guild_id, actor_id, action, details=""):
    execute(
        """
        INSERT INTO audit_log
        (guild_id, actor_id, action, details, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, actor_id, action, details[:3000], now()),
    )


def is_staff(member):
    cfg = get_config(member.guild.id)

    if member.guild.owner_id == member.id:
        return True

    if member.guild_permissions.administrator:
        return True

    role_ids = {cfg["staff_role"], cfg["mm_role"]}
    return any(r.id in role_ids for r in member.roles if r.id)


def is_mm(member):
    cfg = get_config(member.guild.id)

    if member.guild.owner_id == member.id:
        return True

    if member.guild_permissions.administrator:
        return True

    return bool(
        cfg["mm_role"]
        and any(r.id == cfg["mm_role"] for r in member.roles)
    )


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
        timestamp=datetime.now(timezone.utc),
    )

    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        pass


# ============================================================
# SECURITY / HELPERS
# ============================================================

SENSITIVE_RE = re.compile(
    r"(password|passcode|cookie|token|recovery\s*code|"
    r"2fa\s*code|private\s*key|secret\s*key|auth\s*token)",
    re.I,
)

URL_RE = re.compile(r"https?://\S+", re.I)

SCAM_PHRASES = (
    "skip mm",
    "no middleman",
    "send first",
    "trust me",
    "give me your account",
    "login here",
    "verify your account",
    "free nitro",
    "claim reward",
    "dm me instead",
    "use this link",
)


def sanitize_message(content):
    if not content:
        return ""

    if SENSITIVE_RE.search(content):
        return "[REDACTED SENSITIVE MESSAGE]"

    return content[:2000]


def parse_user_id(value):
    value = value.strip()
    match = re.search(r"<@!?(\d+)>", value)

    if match:
        return int(match.group(1))

    if value.isdigit():
        return int(value)

    return None


def safe_channel_name(value):
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:40] or "user"


def risk_color(risk):
    return {
        "LOW": discord.Color.green(),
        "MEDIUM": discord.Color.gold(),
        "HIGH": discord.Color.orange(),
        "CRITICAL": discord.Color.red(),
    }.get(risk, discord.Color.greyple())


def risk_icon(risk):
    return {
        "LOW": "🟢",
        "MEDIUM": "🟡",
        "HIGH": "🔴",
        "CRITICAL": "🚨",
        "UNKNOWN": "⚪",
    }.get(risk, "⚪")


# ============================================================
# EMBEDS
# ============================================================

def trade_embed(trade):
    status = trade["status"]
    risk = trade["risk"]

    embed = discord.Embed(
        title=f"🤝 MM Trade #{trade['id']}",
        color=discord.Color.green() if status == "OPEN" else discord.Color.red(),
    )

    embed.description = (
        "AI-assisted monitoring is active. "
        "A human MM/staff member makes the final decision."
    )

    embed.add_field(
        name="Status",
        value=f"`{status}`",
        inline=True,
    )
    embed.add_field(
        name="AI Risk",
        value=f"{risk_icon(risk)} `{risk}`",
        inline=True,
    )

    buyer = f"<@{trade['buyer_id']}>" if trade["buyer_id"] else "Not set"
    seller = f"<@{trade['seller_id']}>" if trade["seller_id"] else "Not set"
    mm = f"<@{trade['mm_id']}>" if trade["mm_id"] else "Unassigned"

    embed.add_field(name="🧑 Buyer", value=buyer, inline=True)
    embed.add_field(name="👤 Seller", value=seller, inline=True)
    embed.add_field(name="🛡️ Middleman", value=mm, inline=True)

    embed.add_field(
        name="📦 Buyer Gives",
        value=trade["buyer_offer"] or "Not provided",
        inline=False,
    )

    embed.add_field(
        name="🎁 Seller Gives",
        value=trade["seller_offer"] or "Not provided",
        inline=False,
    )

    checks = (
        f"Buyer confirmation: {'✅' if trade['buyer_confirmed'] else '⏳'}\n"
        f"Seller confirmation: {'✅' if trade['seller_confirmed'] else '⏳'}\n"
        f"Payment verified: {'✅' if trade['payment_verified'] else '⏳'}\n"
        f"Item verified: {'✅' if trade['item_verified'] else '⏳'}"
    )

    embed.add_field(
        name="🔐 Trade Checks",
        value=checks,
        inline=False,
    )

    if trade["risk_reason"]:
        embed.add_field(
            name="🧠 AI Notes",
            value=trade["risk_reason"][:1000],
            inline=False,
        )

    if trade["next_action"]:
        embed.add_field(
            name="➡️ AI Recommendation",
            value=trade["next_action"][:1000],
            inline=False,
        )

    embed.set_footer(
        text="Advanced Auto-MM • AI assists; humans control final verification"
    )

    return embed


# ============================================================
# AI
# ============================================================

AI_SYSTEM = """
You are the security and trade-analysis assistant for a Discord
middleman server.

You NEVER control money, items, accounts, bans, kicks, role changes,
or final trade approval. You assist a human middleman.

Analyze ONLY the supplied trade details and conversation.

Look for:
- changing trade terms
- contradictions between buyer and seller
- fake or unverifiable payment claims
- pressure to skip the middleman
- suspicious links
- credential requests
- impersonation
- attempts to move the trade outside the ticket
- scam-like behavior
- unusual urgency or coercion
- missing confirmations
- disagreement about what was promised
- suspicious account/item/payment language

Important:
- Do not claim a payment is real unless a human/payment system verified it.
- Do not accuse someone of being a scammer as a certainty.
- Distinguish suspicious behavior from proven fraud.
- Recommend human review for meaningful risk.

Return ONLY valid JSON:
{
  "risk": "LOW|MEDIUM|HIGH|CRITICAL",
  "reason": "short explanation",
  "next_action": "short recommended action",
  "flags": ["short flag 1", "short flag 2"]
}
"""


async def ai_analyze_trade(trade_id):
    trade = get_trade_by_id(trade_id)

    if not trade:
        return {
            "risk": "UNKNOWN",
            "reason": "Trade no longer exists.",
            "next_action": "Human review.",
            "flags": [],
        }

    messages = db.execute(
        """
        SELECT username, content, created_at
        FROM trade_messages
        WHERE trade_id=?
        ORDER BY id DESC LIMIT 100
        """,
        (trade_id,),
    ).fetchall()

    conversation = "\n".join(
        f"[{m['username']}] {m['content']}"
        for m in reversed(messages)
    )

    if len(conversation) > 14000:
        conversation = conversation[-14000:]

    prompt = f"""
Trade #{trade_id}

Buyer offer:
{trade['buyer_offer']}

Seller offer:
{trade['seller_offer']}

Current checks:
Buyer confirmed: {bool(trade['buyer_confirmed'])}
Seller confirmed: {bool(trade['seller_confirmed'])}
Payment verified: {bool(trade['payment_verified'])}
Item verified: {bool(trade['item_verified'])}

Conversation:
{conversation or '[No conversation yet]'}
"""

    try:
        result = await groq.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0.1,
            max_tokens=600,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": AI_SYSTEM},
                {"role": "user", "content": prompt},
            ],
        )

        raw = result.choices[0].message.content or "{}"
        data = json.loads(raw)

        risk = str(data.get("risk", "UNKNOWN")).upper()

        if risk not in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            risk = "UNKNOWN"

        flags = data.get("flags", [])
        if not isinstance(flags, list):
            flags = []

        return {
            "risk": risk,
            "reason": str(data.get("reason", ""))[:1500],
            "next_action": str(data.get("next_action", ""))[:1200],
            "flags": [str(x)[:300] for x in flags[:10]],
        }

    except Exception as exc:
        log.exception("Groq analysis failed for trade %s", trade_id)
        return {
            "risk": "UNKNOWN",
            "reason": f"AI analysis failed: {type(exc).__name__}",
            "next_action": "Human MM should review the trade manually.",
            "flags": [],
        }


async def run_ai_for_trade(channel, trade):
    cfg = get_config(trade["guild_id"])

    if not cfg["ai_enabled"]:
        return

    result = await ai_analyze_trade(trade["id"])

    update_trade(
        trade["id"],
        risk=result["risk"],
        risk_reason=result["reason"],
        next_action=result["next_action"],
    )

    for flag in result["flags"]:
        execute(
            """
            INSERT INTO flags
            (trade_id, user_id, reason, severity, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                trade["id"],
                trade["buyer_id"],
                flag,
                result["risk"],
                now(),
            ),
        )

    if result["risk"] in {"HIGH", "CRITICAL"}:
        alert = discord.Embed(
            title=f"🚨 AI TRADE ALERT — {result['risk']}",
            description=(
                f"**Trade:** #{trade['id']}\n"
                f"**Reason:** {result['reason']}\n"
                f"**Recommended action:** {result['next_action']}\n\n"
                "⚠️ AI does not make the final decision. "
                "A human MM/staff member must review this."
            ),
            color=risk_color(result["risk"]),
        )

        try:
            await channel.send(
                embed=alert,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

        await send_log(
            channel.guild,
            f"🚨 AI Flagged Trade #{trade['id']}",
            (
                f"Risk: {result['risk']}\n"
                f"Reason: {result['reason']}\n"
                f"Action: {result['next_action']}"
            ),
            discord.Color.red(),
        )

    await refresh_trade_message(channel)


async def ai_chat(question, trade=None):
    context = "No active trade context."

    if trade:
        messages = db.execute(
            """
            SELECT username, content
            FROM trade_messages
            WHERE trade_id=?
            ORDER BY id DESC LIMIT 40
            """,
            (trade["id"],),
        ).fetchall()

        conversation = "\n".join(
            f"[{m['username']}] {m['content']}"
            for m in reversed(messages)
        )

        context = f"""
Active trade #{trade['id']}
Buyer offer: {trade['buyer_offer']}
Seller offer: {trade['seller_offer']}
Risk: {trade['risk']}
Risk reason: {trade['risk_reason']}
Conversation:
{conversation[-8000:]}
"""

    system = """
You are the helpful AI assistant inside a Discord middleman server.
Be concise and practical.

You may explain trade status, safety concerns, MM procedures,
Discord configuration, or general questions.

Never claim that a payment, item, account, or transaction is verified
unless the trade record explicitly says it is verified.

Never ask users for passwords, cookies, tokens, recovery codes,
private keys, or other credentials.
"""

    result = await groq.chat.completions.create(
        model=GROQ_MODEL,
        temperature=0.3,
        max_tokens=700,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"{context}\n\nUser question:\n{question[:3000]}",
            },
        ],
    )

    return result.choices[0].message.content[:4000]


# ============================================================
# TRADE UI
# ============================================================

class MMPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start MM Trade",
        style=discord.ButtonStyle.green,
        emoji="🤝",
        custom_id="mm_start_trade_v2",
    )
    async def start_trade(self, interaction, button):
        await interaction.response.send_modal(TradeSetupModal())


class TradeSetupModal(discord.ui.Modal, title="Start Middleman Trade"):
    buyer = discord.ui.TextInput(
        label="Buyer",
        placeholder="Discord ID or @mention",
        required=True,
        max_length=100,
    )

    seller = discord.ui.TextInput(
        label="Seller",
        placeholder="Discord ID or @mention",
        required=True,
        max_length=100,
    )

    buyer_offer = discord.ui.TextInput(
        label="Buyer gives",
        placeholder="Example: 25 USD / Roblox item",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph,
    )

    seller_offer = discord.ui.TextInput(
        label="Seller gives",
        placeholder="Example: Dragon",
        required=True,
        max_length=1000,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction):
        guild = interaction.guild

        if not guild:
            return await interaction.response.send_message(
                "❌ This can only be used inside a server.",
                ephemeral=True,
            )

        buyer_id = parse_user_id(self.buyer.value)
        seller_id = parse_user_id(self.seller.value)

        if not buyer_id or not seller_id:
            return await interaction.response.send_message(
                "❌ I couldn't understand one of the user IDs/mentions.",
                ephemeral=True,
            )

        buyer = guild.get_member(buyer_id)
        seller = guild.get_member(seller_id)

        if not buyer or not seller:
            return await interaction.response.send_message(
                "❌ Both users must be in this server.",
                ephemeral=True,
            )

        if buyer.id == seller.id:
            return await interaction.response.send_message(
                "❌ Buyer and seller must be different users.",
                ephemeral=True,
            )

        if get_open_trade_for_user(guild.id, buyer.id):
            return await interaction.response.send_message(
                "❌ The buyer already has an open MM trade.",
                ephemeral=True,
            )

        if get_open_trade_for_user(guild.id, seller.id):
            return await interaction.response.send_message(
                "❌ The seller already has an open MM trade.",
                ephemeral=True,
            )

        cfg = get_config(guild.id)
        category = (
            guild.get_channel(cfg["ticket_category"])
            if cfg["ticket_category"]
            else None
        )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False
            ),
            buyer: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            seller: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            ),
        }

        for role_id in (cfg["mm_role"], cfg["staff_role"]):
            if role_id:
                role = guild.get_role(role_id)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        manage_messages=True,
                    )

        channel_name = (
            f"mm-{safe_channel_name(buyer.name)}-"
            f"{safe_channel_name(seller.name)}"
        )

        try:
            channel = await guild.create_text_channel(
                channel_name[:95],
                category=category,
                overwrites=overwrites,
                topic="Advanced Auto-MM trade ticket",
            )
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I don't have permission to create the MM ticket.",
                ephemeral=True,
            )

        trade_id = execute(
            """
            INSERT INTO trades
            (
                guild_id, channel_id, buyer_id, seller_id,
                buyer_offer, seller_offer, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild.id,
                channel.id,
                buyer.id,
                seller.id,
                self.buyer_offer.value,
                self.seller_offer.value,
                now(),
                now(),
            ),
        )

        trade = get_trade_by_id(trade_id)

        embed = trade_embed(trade)
        embed.description = (
            "Welcome to the **Advanced Auto-MM trade room**.\n\n"
            "🧠 AI monitors the conversation for suspicious behavior.\n"
            "🛡️ A human MM controls final verification.\n"
            "🔐 Never send passwords, cookies, tokens, recovery codes, "
            "or private keys."
        )

        await channel.send(
            content=f"{buyer.mention} {seller.mention}",
            embed=embed,
            view=TradeControls(),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                everyone=False,
                roles=False,
            ),
        )

        audit(
            guild.id,
            interaction.user.id,
            "TRADE_CREATED",
            f"Trade #{trade_id}: buyer={buyer.id}, seller={seller.id}",
        )

        await interaction.response.send_message(
            f"✅ Trade #{trade_id} created: {channel.mention}",
            ephemeral=True,
        )

        await send_log(
            guild,
            "🤝 MM Trade Created",
            (
                f"Trade #{trade_id}\n"
                f"Buyer: {buyer.mention}\n"
                f"Seller: {seller.mention}\n"
                f"Created by: {interaction.user.mention}"
            ),
            discord.Color.green(),
        )


class TradeControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Claim MM",
        style=discord.ButtonStyle.primary,
        emoji="🛡️",
        custom_id="trade_claim_mm_v2",
    )
    async def claim(self, interaction, button):
        trade = get_trade(interaction.channel.id)

        if not trade:
            return await interaction.response.send_message(
                "❌ Trade not found.",
                ephemeral=True,
            )

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ You don't have the Middleman role.",
                ephemeral=True,
            )

        if trade["mm_id"] and trade["mm_id"] != interaction.user.id:
            return await interaction.response.send_message(
                "❌ This trade already has a middleman.",
                ephemeral=True,
            )

        update_trade(trade["id"], mm_id=interaction.user.id)

        execute(
            """
            INSERT INTO mms(guild_id, user_id, trades)
            VALUES (?, ?, 0)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (trade["guild_id"], interaction.user.id),
        )

        audit(
            trade["guild_id"],
            interaction.user.id,
            "MM_CLAIMED",
            f"Trade #{trade['id']}",
        )

        await interaction.response.send_message(
            f"🛡️ {interaction.user.mention} is now the middleman."
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Confirm Trade",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="trade_confirm_v2",
    )
    async def confirm(self, interaction, button):
        trade = get_trade(interaction.channel.id)

        if not trade:
            return await interaction.response.send_message(
                "❌ Trade not found.",
                ephemeral=True,
            )

        if interaction.user.id == trade["buyer_id"]:
            update_trade(trade["id"], buyer_confirmed=1)

        elif interaction.user.id == trade["seller_id"]:
            update_trade(trade["id"], seller_confirmed=1)

        else:
            return await interaction.response.send_message(
                "❌ Only the buyer or seller can confirm.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "✅ Your confirmation has been recorded.",
            ephemeral=True,
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Verify Payment",
        style=discord.ButtonStyle.secondary,
        emoji="💳",
        custom_id="trade_verify_payment_v2",
    )
    async def payment(self, interaction, button):
        trade = get_trade(interaction.channel.id)

        if not trade:
            return await interaction.response.send_message(
                "❌ Trade not found.",
                ephemeral=True,
            )

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ Only an MM can verify payment.",
                ephemeral=True,
            )

        update_trade(trade["id"], payment_verified=1)

        audit(
            trade["guild_id"],
            interaction.user.id,
            "PAYMENT_VERIFIED",
            f"Trade #{trade['id']}",
        )

        await interaction.response.send_message(
            "💳 Payment marked as verified by the MM."
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Verify Item",
        style=discord.ButtonStyle.secondary,
        emoji="📦",
        custom_id="trade_verify_item_v2",
    )
    async def item(self, interaction, button):
        trade = get_trade(interaction.channel.id)

        if not trade:
            return await interaction.response.send_message(
                "❌ Trade not found.",
                ephemeral=True,
            )

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ Only an MM can verify the item.",
                ephemeral=True,
            )

        update_trade(trade["id"], item_verified=1)

        audit(
            trade["guild_id"],
            interaction.user.id,
            "ITEM_VERIFIED",
            f"Trade #{trade['id']}",
        )

        await interaction.response.send_message(
            "📦 Item marked as verified by the MM."
        )
        await refresh_trade_message(interaction.channel)

    @discord.ui.button(
        label="Complete",
        style=discord.ButtonStyle.success,
        emoji="🏁",
        custom_id="trade_complete_v2",
    )
    async def complete(self, interaction, button):
        trade = get_trade(interaction.channel.id)

        if not trade:
            return await interaction.response.send_message(
                "❌ Trade not found.",
                ephemeral=True,
            )

        if not is_mm(interaction.user):
            return await interaction.response.send_message(
                "❌ Only an MM can complete a trade.",
                ephemeral=True,
            )

        if trade["mm_id"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "❌ Only the assigned MM or an administrator can complete this trade.",
                ephemeral=True,
            )

        if not (
            trade["buyer_confirmed"]
            and trade["seller_confirmed"]
            and trade["payment_verified"]
            and trade["item_verified"]
        ):
            return await interaction.response.send_message(
                "⚠️ Buyer confirmation, seller confirmation, "
                "payment verification, and item verification are all required.",
                ephemeral=True,
            )

        update_trade(
            trade["id"],
            status="COMPLETED",
            closed_at=now(),
        )

        if trade["mm_id"]:
            execute(
                """
                INSERT INTO mms(guild_id, user_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                (trade["guild_id"], trade["mm_id"]),
            )

            execute(
                """
                UPDATE mms
                SET trades=trades+1, successful=successful+1
                WHERE guild_id=? AND user_id=?
                """,
                (trade["guild_id"], trade["mm_id"]),
            )

        audit(
            trade["guild_id"],
            interaction.user.id,
            "TRADE_COMPLETED",
            f"Trade #{trade['id']}",
        )

        await interaction.response.send_message(
            "🏁 **Trade completed successfully.** This ticket will close shortly."
        )

        await send_log(
            interaction.guild,
            "🏁 Trade Completed",
            f"Trade #{trade['id']} completed by {interaction.user.mention}.",
            discord.Color.green(),
        )

        await asyncio.sleep(5)

        try:
            await interaction.channel.delete(
                reason=f"Completed MM trade #{trade['id']}"
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="trade_cancel_v2",
    )
    async def cancel(self, interaction, button):
        trade = get_trade(interaction.channel.id)

        if not trade:
            return await interaction.response.send_message(
                "❌ Trade not found.",
                ephemeral=True,
            )

        if not is_staff(interaction.user):
            return await interaction.response.send_message(
                "❌ Only staff/MM can cancel a trade.",
                ephemeral=True,
            )

        update_trade(
            trade["id"],
            status="CANCELLED",
            closed_at=now(),
        )

        if trade["mm_id"]:
            execute(
                """
                UPDATE mms
                SET trades=trades+1, cancelled=cancelled+1
                WHERE guild_id=? AND user_id=?
                """,
                (trade["guild_id"], trade["mm_id"]),
            )

        audit(
            trade["guild_id"],
            interaction.user.id,
            "TRADE_CANCELLED",
            f"Trade #{trade['id']}",
        )

        await interaction.response.send_message(
            "❌ Trade cancelled. The ticket will close shortly."
        )

        await send_log(
            interaction.guild,
            "❌ Trade Cancelled",
            f"Trade #{trade['id']} cancelled by {interaction.user.mention}.",
            discord.Color.red(),
        )

        await asyncio.sleep(5)

        try:
            await interaction.channel.delete(
                reason=f"Cancelled MM trade #{trade['id']}"
            )
        except discord.HTTPException:
            pass


async def refresh_trade_message(channel):
    trade = get_trade(channel.id)

    if not trade:
        return

    async for message in channel.history(limit=30):
        if message.author.id == bot.user.id and message.embeds:
            try:
                await message.edit(
                    embed=trade_embed(trade),
                    view=TradeControls(),
                )
                return
            except discord.HTTPException:
                return


# ============================================================
# BACKGROUND AI / RECOVERY
# ============================================================

@tasks.loop(minutes=15)
async def ai_health_loop():
    for guild in bot.guilds:
        cfg = get_config(guild.id)

        if not cfg["ai_enabled"]:
            continue

        rows = db.execute(
            """
            SELECT * FROM trades
            WHERE guild_id=? AND status='OPEN'
            ORDER BY updated_at DESC LIMIT 20
            """,
            (guild.id,),
        ).fetchall()

        for trade in rows:
            channel = guild.get_channel(trade["channel_id"])

            if not channel:
                continue

            try:
                await run_ai_for_trade(channel, trade)
            except Exception:
                log.exception("Background AI error on trade %s", trade["id"])


@ai_health_loop.before_loop
async def before_ai_health_loop():
    await bot.wait_until_ready()


async def cleanup_stale_channels():
    # Do not automatically close active trades.
    # This only reports database records whose channels disappeared.
    for guild in bot.guilds:
        rows = db.execute(
            """
            SELECT * FROM trades
            WHERE guild_id=? AND status='OPEN'
            """,
            (guild.id,),
        ).fetchall()

        for trade in rows:
            if not guild.get_channel(trade["channel_id"]):
                update_trade(
                    trade["id"],
                    status="CANCELLED",
                    closed_at=now(),
                    risk_reason="Ticket channel disappeared.",
                )
                audit(
                    guild.id,
                    0,
                    "STALE_TRADE_CLOSED",
                    f"Trade #{trade['id']} channel no longer exists.",
                )


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    if not getattr(bot, "_views_added", False):
        bot.add_view(MMPanel())
        bot.add_view(TradeControls())
        bot._views_added = True

    try:
        synced = await bot.tree.sync()
        log.info("Synced %s global slash commands.", len(synced))
    except Exception:
        log.exception("Slash-command sync failed.")

    if not ai_health_loop.is_running():
        ai_health_loop.start()

    await cleanup_stale_channels()

    log.info(
        "Online as %s (%s) | guilds=%s | model=%s | db=%s",
        bot.user,
        bot.user.id,
        len(bot.guilds),
        GROQ_MODEL,
        DB_PATH,
    )


@bot.event
async def on_guild_join(guild):
    get_config(guild.id)
    log.info("Joined guild %s (%s)", guild.name, guild.id)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild:
        trade = get_trade(message.channel.id)

        if trade and trade["status"] == "OPEN":
            content = message.content.strip()
            safe_content = sanitize_message(content)

            if safe_content:
                execute(
                    """
                    INSERT INTO trade_messages
                    (trade_id, user_id, username, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        trade["id"],
                        message.author.id,
                        str(message.author),
                        safe_content,
                        now(),
                    ),
                )

            # Immediate credential warning.
            if SENSITIVE_RE.search(content):
                try:
                    await message.reply(
                        "🛡️ **Security warning:** Never send passwords, "
                        "cookies, tokens, recovery codes, private keys, "
                        "or other account credentials in an MM ticket."
                    )
                except discord.HTTPException:
                    pass

            # Lightweight immediate warning for obvious scam language.
            lowered = content.lower()

            if any(phrase in lowered for phrase in SCAM_PHRASES):
                try:
                    await message.reply(
                        "⚠️ **Safety notice:** This message contains language "
                        "that can be associated with risky trades. "
                        "Please keep the trade inside the MM ticket and wait "
                        "for human MM review.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

            count = db.execute(
                "SELECT COUNT(*) AS c FROM trade_messages WHERE trade_id=?",
                (trade["id"],),
            ).fetchone()["c"]

            cfg = get_config(message.guild.id)
            interval = max(3, int(cfg["ai_every"] or AI_EVERY_MESSAGES))

            if cfg["ai_enabled"] and count % interval == 0:
                asyncio.create_task(
                    run_ai_for_trade(message.channel, trade)
                )

    await bot.process_commands(message)


# ============================================================
# PREFIX COMMANDS
# ============================================================

@bot.command()
async def mm(ctx):
    """Send the public MM panel."""
    embed = discord.Embed(
        title="🤝 Advanced Middleman Service",
        description=(
            "Use the button below to start a protected trade.\n\n"
            "🧠 Groq AI monitors the conversation.\n"
            "🛡️ Human MM/staff controls final verification.\n"
            "📊 Trades and MM statistics are stored persistently.\n"
            "🔐 Never share passwords, cookies, tokens, recovery codes, "
            "or private keys."
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Trade Flow",
        value=(
            "Start → Claim MM → Verify Payment/Item → "
            "Both Confirm → Complete"
        ),
        inline=False,
    )

    await ctx.send(embed=embed, view=MMPanel())


@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="🤖 Advanced Auto-MM Commands",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Members",
        value=(
            f"`{PREFIX}mm` — MM panel\n"
            f"`{PREFIX}mystats` — your MM stats\n"
            f"`{PREFIX}vouch @mm 1-5 <comment>` — vouch\n"
            f"`{PREFIX}tradeinfo` — current trade\n"
            f"`{PREFIX}ask <question>` — ask Groq AI"
        ),
        inline=False,
    )

    embed.add_field(
        name="MM / Staff",
        value=(
            f"`{PREFIX}ai` — analyze current trade\n"
            f"`{PREFIX}risk` — current AI risk\n"
            f"`{PREFIX}assign @user` — assign MM\n"
            f"`{PREFIX}verify payment` — verify payment\n"
            f"`{PREFIX}verify item` — verify item\n"
            f"`{PREFIX}finish` — complete eligible trade\n"
            f"`{PREFIX}canceltrade` — cancel trade\n"
            f"`{PREFIX}transcript` — create transcript"
        ),
        inline=False,
    )

    embed.add_field(
        name="Admin",
        value=(
            f"`{PREFIX}setup` — setup help\n"
            f"`{PREFIX}setcategory #category`\n"
            f"`{PREFIX}setlogs #channel`\n"
            f"`{PREFIX}setmmrole @role`\n"
            f"`{PREFIX}setstaffrole @role`\n"
            f"`{PREFIX}setpanel #channel`\n"
            f"`{PREFIX}setai on/off`\n"
            f"`{PREFIX}setaievery 6`\n"
            f"`{PREFIX}mmlist` — MM leaderboard"
        ),
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    await ctx.send(
        f"⚙️ **Advanced Auto-MM Setup**\n\n"
        f"`{PREFIX}setcategory #category` — ticket category\n"
        f"`{PREFIX}setlogs #channel` — logs\n"
        f"`{PREFIX}setmmrole @role` — MM role\n"
        f"`{PREFIX}setstaffrole @role` — staff role\n"
        f"`{PREFIX}setpanel #channel` — post panel\n"
        f"`{PREFIX}setai on/off` — AI monitoring\n"
        f"`{PREFIX}setaievery 6` — AI analysis interval\n"
        f"`{PREFIX}mm` — send panel manually"
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def setcategory(ctx, category: discord.CategoryChannel):
    execute(
        "UPDATE guild_config SET ticket_category=? WHERE guild_id=?",
        (category.id, ctx.guild.id),
    )
    await ctx.send(f"✅ Ticket category set to {category.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setlogs(ctx, channel: discord.TextChannel):
    execute(
        "UPDATE guild_config SET log_channel=? WHERE guild_id=?",
        (channel.id, ctx.guild.id),
    )
    await ctx.send(f"✅ Log channel set to {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setmmrole(ctx, role: discord.Role):
    execute(
        "UPDATE guild_config SET mm_role=? WHERE guild_id=?",
        (role.id, ctx.guild.id),
    )
    await ctx.send(f"✅ MM role set to {role.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setstaffrole(ctx, role: discord.Role):
    execute(
        "UPDATE guild_config SET staff_role=? WHERE guild_id=?",
        (role.id, ctx.guild.id),
    )
    await ctx.send(f"✅ Staff role set to {role.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setpanel(ctx, channel: discord.TextChannel):
    execute(
        "UPDATE guild_config SET panel_channel=? WHERE guild_id=?",
        (channel.id, ctx.guild.id),
    )

    embed = discord.Embed(
        title="🤝 Advanced Auto-MM",
        description=(
            "AI-assisted trade protection with human MM verification.\n"
            "Click the button below to start."
        ),
        color=discord.Color.blurple(),
    )

    await channel.send(embed=embed, view=MMPanel())
    await ctx.send(f"✅ MM panel posted in {channel.mention}")


@bot.command()
@commands.has_permissions(administrator=True)
async def setai(ctx, state: str):
    state = state.lower()

    if state not in {"on", "off"}:
        return await ctx.send(f"Use `{PREFIX}setai on` or `{PREFIX}setai off`.")

    enabled = 1 if state == "on" else 0

    execute(
        "UPDATE guild_config SET ai_enabled=? WHERE guild_id=?",
        (enabled, ctx.guild.id),
    )

    audit(
        ctx.guild.id,
        ctx.author.id,
        "AI_SETTING_CHANGED",
        f"enabled={bool(enabled)}",
    )

    await ctx.send(
        f"🧠 Groq AI monitoring is now **{'ON' if enabled else 'OFF'}**."
    )


@bot.command()
@commands.has_permissions(administrator=True)
async def setaievery(ctx, amount: int):
    amount = max(3, min(amount, 50))

    execute(
        "UPDATE guild_config SET ai_every=? WHERE guild_id=?",
        (amount, ctx.guild.id),
    )

    await ctx.send(
        f"✅ AI will automatically analyze every **{amount} messages** "
        f"in active trades."
    )


# ============================================================
# TRADE COMMANDS
# ============================================================

@bot.command()
async def tradeinfo(ctx):
    trade = get_trade(ctx.channel.id)

    if not trade:
        return await ctx.send(
            "❌ This channel is not an MM trade ticket."
        )

    await ctx.send(embed=trade_embed(trade))


@bot.command()
async def ai(ctx):
    trade = get_trade(ctx.channel.id)

    if not trade:
        return await ctx.send(
            "❌ Use this command inside an MM ticket."
        )

    if not is_staff(ctx.author):
        return await ctx.send(
            "❌ Only MM/staff can request an AI analysis."
        )

    msg = await ctx.send("🧠 Analyzing the current trade with Groq...")

    result = await ai_analyze_trade(trade["id"])

    update_trade(
        trade["id"],
        risk=result["risk"],
        risk_reason=result["reason"],
        next_action=result["next_action"],
    )

    embed = discord.Embed(
        title=f"🧠 AI Analysis — Trade #{trade['id']}",
        color=risk_color(result["risk"]),
    )

    embed.add_field(
        name="Risk",
        value=f"{risk_icon(result['risk'])} `{result['risk']}`",
        inline=True,
    )

    embed.add_field(
        name="Reason",
        value=result["reason"][:1000] or "No reason returned.",
        inline=False,
    )

    embed.add_field(
        name="Recommended Action",
        value=result["next_action"][:1000] or "Human review.",
        inline=False,
    )

    flags = "\n".join(
        f"• {x}" for x in result["flags"]
    ) or "No specific flags."

    embed.add_field(
        name="Flags",
        value=flags[:1000],
        inline=False,
    )

    embed.set_footer(
        text="AI recommendation only — human MM makes the final decision."
    )

    await msg.edit(content="", embed=embed)
    await refresh_trade_message(ctx.channel)


@bot.command()
async def ask(ctx, *, question: str):
    trade = get_trade(ctx.channel.id)

    if trade and not is_staff(ctx.author):
        # Members can ask AI about the process, but the AI gets only
        # the active trade context and must not expose sensitive data.
        pass

    try:
        answer = await ai_chat(question, trade)
    except Exception as exc:
        log.exception("AI chat failed")
        return await ctx.send(
            f"❌ Groq request failed: `{type(exc).__name__}`"
        )

    embed = discord.Embed(
        title="🤖 Groq MM Assistant",
        description=answer[:4000],
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed)


@bot.command()
async def risk(ctx):
    trade = get_trade(ctx.channel.id)

    if not trade:
        return await ctx.send("❌ Not an MM ticket.")

    await ctx.send(
        f"🧠 Current AI risk for Trade #{trade['id']}: "
        f"**{trade['risk']}**\n"
        f"{trade['risk_reason'] or 'No AI analysis yet.'}"
    )


@bot.command()
async def assign(ctx, member: discord.Member):
    trade = get_trade(ctx.channel.id)

    if not trade:
        return await ctx.send("❌ Not an MM ticket.")

    if not is_staff(ctx.author):
        return await ctx.send("❌ Staff/MM only.")

    if not is_mm(member):
        return await ctx.send(
            "❌ That user doesn't have the MM role."
        )

    update_trade(trade["id"], mm_id=member.id)

    execute(
        """
        INSERT INTO mms(guild_id, user_id, trades)
        VALUES (?, ?, 0)
        ON CONFLICT(guild_id, user_id) DO NOTHING
        """,
        (trade["guild_id"], member.id),
    )

    audit(
        trade["guild_id"],
        ctx.author.id,
        "MM_ASSIGNED",
        f"Trade #{trade['id']} assigned to {member.id}",
    )

    await ctx.send(
        f"🛡️ {member.mention} has been assigned to Trade #{trade['id']}."
    )
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
        update_trade(trade["id"], payment_verified=1)
        action = "PAYMENT_VERIFIED"

    elif kind == "item":
        update_trade(trade["id"], item_verified=1)
        action = "ITEM_VERIFIED"

    else:
        return await ctx.send(
            f"Use `{PREFIX}verify payment` or `{PREFIX}verify item`."
        )

    audit(
        trade["guild_id"],
        ctx.author.id,
        action,
        f"Trade #{trade['id']}",
    )

    await ctx.send(
        f"✅ `{kind}` verification recorded by {ctx.author.mention}."
    )
    await refresh_trade_message(ctx.channel)


@bot.command()
async def finish(ctx):
    trade = get_trade(ctx.channel.id)

    if not trade:
        return await ctx.send("❌ Not an MM ticket.")

    if not is_mm(ctx.author):
        return await ctx.send("❌ MM only.")

    if trade["mm_id"] != ctx.author.id and not ctx.author.guild_permissions.administrator:
        return await ctx.send(
            "❌ Only the assigned MM or an administrator can finish this trade."
        )

    if not (
        trade["buyer_confirmed"]
        and trade["seller_confirmed"]
        and trade["payment_verified"]
        and trade["item_verified"]
    ):
        return await ctx.send(
            "⚠️ All confirmations and verification checks are required."
        )

    update_trade(
        trade["id"],
        status="COMPLETED",
        closed_at=now(),
    )

    if trade["mm_id"]:
        execute(
            """
            INSERT INTO mms(guild_id, user_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (trade["guild_id"], trade["mm_id"]),
        )

        execute(
            """
            UPDATE mms
            SET trades=trades+1, successful=successful+1
            WHERE guild_id=? AND user_id=?
            """,
            (trade["guild_id"], trade["mm_id"]),
        )

    audit(
        trade["guild_id"],
        ctx.author.id,
        "TRADE_COMPLETED",
        f"Trade #{trade['id']}",
    )

    await ctx.send("🏁 **Trade completed successfully.**")

    await send_log(
        ctx.guild,
        "🏁 Trade Completed",
        f"Trade #{trade['id']} completed by {ctx.author.mention}.",
        discord.Color.green(),
    )


@bot.command()
async def canceltrade(ctx):
    trade = get_trade(ctx.channel.id)

    if not trade:
        return await ctx.send("❌ Not an MM ticket.")

    if not is_staff(ctx.author):
        return await ctx.send("❌ Staff/MM only.")

    update_trade(
        trade["id"],
        status="CANCELLED",
        closed_at=now(),
    )

    if trade["mm_id"]:
        execute(
            """
            UPDATE mms
            SET trades=trades+1, cancelled=cancelled+1
            WHERE guild_id=? AND user_id=?
            """,
            (trade["guild_id"], trade["mm_id"]),
        )

    audit(
        trade["guild_id"],
        ctx.author.id,
        "TRADE_CANCELLED",
        f"Trade #{trade['id']}",
    )

    await ctx.send("❌ Trade cancelled.")

    await send_log(
        ctx.guild,
        "❌ Trade Cancelled",
        f"Trade #{trade['id']} cancelled by {ctx.author.mention}.",
        discord.Color.red(),
    )


# ============================================================
# STATS / VOUCHES
# ============================================================

@bot.command()
async def mystats(ctx):
    row = db.execute(
        "SELECT * FROM mms WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, ctx.author.id),
    ).fetchone()

    if not row:
        return await ctx.send(
            "You don't have MM statistics yet."
        )

    success_rate = (
        (row["successful"] / row["trades"]) * 100
        if row["trades"]
        else 0
    )

    embed = discord.Embed(
        title=f"🛡️ MM Statistics — {ctx.author.display_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Trades", value=str(row["trades"]))
    embed.add_field(name="Successful", value=str(row["successful"]))
    embed.add_field(name="Cancelled", value=str(row["cancelled"]))
    embed.add_field(
        name="Success Rate",
        value=f"{success_rate:.1f}%",
    )

    await ctx.send(embed=embed)


@bot.command()
async def mmlist(ctx):
    rows = db.execute(
        """
        SELECT user_id, trades, successful, cancelled
        FROM mms
        WHERE guild_id=?
        ORDER BY successful DESC, trades DESC
        LIMIT 15
        """,
        (ctx.guild.id,),
    ).fetchall()

    if not rows:
        return await ctx.send("No MM statistics yet.")

    lines = []

    for i, row in enumerate(rows, 1):
        rate = (
            (row["successful"] / row["trades"]) * 100
            if row["trades"]
            else 0
        )

        lines.append(
            f"**{i}.** <@{row['user_id']}> — "
            f"{row['successful']} successful / {row['trades']} total "
            f"({rate:.1f}%)"
        )

    embed = discord.Embed(
        title="🏆 Middleman Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )

    await ctx.send(embed=embed)


@bot.command()
async def vouch(
    ctx,
    member: discord.Member,
    rating: int,
    *,
    comment="",
):
    if rating < 1 or rating > 5:
        return await ctx.send(
            "Rating must be between 1 and 5."
        )

    cfg = get_config(ctx.guild.id)

    if not cfg["mm_role"] or not any(
        r.id == cfg["mm_role"] for r in member.roles
    ):
        return await ctx.send(
            "❌ That user is not configured as an MM."
        )

    execute(
        """
        INSERT INTO vouches
        (guild_id, mm_id, user_id, rating, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.guild.id,
            member.id,
            ctx.author.id,
            rating,
            comment[:1000],
            now(),
        ),
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

    async for message in ctx.channel.history(
        limit=1000,
        oldest_first=True,
    ):
        if message.author.bot:
            continue

        content = (
            "[REDACTED]"
            if SENSITIVE_RE.search(message.content)
            else message.content
        )

        messages.append(
            f"[{message.created_at.isoformat()}] "
            f"{message.author} ({message.author.id}): {content}"
        )

    text = (
        f"ADVANCED MM TRADE #{trade['id']}\n"
        f"Status: {trade['status']}\n"
        f"Buyer: {trade['buyer_id']}\n"
        f"Seller: {trade['seller_id']}\n"
        f"MM: {trade['mm_id']}\n"
        f"AI Risk: {trade['risk']}\n"
        f"AI Notes: {trade['risk_reason']}\n"
        f"AI Recommendation: {trade['next_action']}\n\n"
        + "\n".join(messages)
    )

    filename = f"transcript-{trade['id']}.txt"

    with open(filename, "w", encoding="utf-8") as file:
        file.write(text)

    try:
        await ctx.send(
            f"📋 Transcript for Trade #{trade['id']}",
            file=discord.File(filename),
        )
    finally:
        try:
            os.remove(filename)
        except OSError:
            pass


# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(name="mm", description="Open the Advanced Auto-MM panel")
async def slash_mm(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤝 Advanced Auto-MM",
        description=(
            "Start a protected trade using the button below.\n"
            "Groq AI monitors risk while human MM/staff members "
            "control final verification."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(
        embed=embed,
        view=MMPanel(),
    )


@bot.tree.command(name="tradeinfo", description="Show the current MM trade")
async def slash_tradeinfo(interaction: discord.Interaction):
    trade = get_trade(interaction.channel.id)

    if not trade:
        return await interaction.response.send_message(
            "❌ This channel is not an MM trade.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        embed=trade_embed(trade)
    )


@bot.tree.command(name="risk", description="Show the AI risk for this trade")
async def slash_risk(interaction: discord.Interaction):
    trade = get_trade(interaction.channel.id)

    if not trade:
        return await interaction.response.send_message(
            "❌ This channel is not an MM trade.",
            ephemeral=True,
        )

    await interaction.response.send_message(
        f"🧠 Trade #{trade['id']} risk: "
        f"**{trade['risk']}**\n"
        f"{trade['risk_reason'] or 'No analysis yet.'}",
        ephemeral=True,
    )


@bot.tree.command(name="ask", description="Ask the Groq MM assistant")
@app_commands.describe(question="Your question")
async def slash_ask(interaction, question: str):
    await interaction.response.defer()

    trade = get_trade(interaction.channel.id)

    try:
        answer = await ai_chat(question, trade)
    except Exception as exc:
        log.exception("Slash AI failed")
        return await interaction.followup.send(
            f"❌ Groq request failed: `{type(exc).__name__}`"
        )

    embed = discord.Embed(
        title="🤖 Groq MM Assistant",
        description=answer[:4000],
        color=discord.Color.blurple(),
    )

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="ai", description="Run an AI security analysis")
async def slash_ai(interaction):
    trade = get_trade(interaction.channel.id)

    if not trade:
        return await interaction.response.send_message(
            "❌ Use this inside an MM ticket.",
            ephemeral=True,
        )

    if not is_staff(interaction.user):
        return await interaction.response.send_message(
            "❌ MM/staff only.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)

    result = await ai_analyze_trade(trade["id"])

    update_trade(
        trade["id"],
        risk=result["risk"],
        risk_reason=result["reason"],
        next_action=result["next_action"],
    )

    flags = "\n".join(
        f"• {x}" for x in result["flags"]
    ) or "No specific flags."

    embed = discord.Embed(
        title=f"🧠 AI Analysis — Trade #{trade['id']}",
        color=risk_color(result["risk"]),
    )
    embed.add_field(
        name="Risk",
        value=f"{risk_icon(result['risk'])} `{result['risk']}`",
    )
    embed.add_field(
        name="Reason",
        value=result["reason"][:1000] or "None",
        inline=False,
    )
    embed.add_field(
        name="Action",
        value=result["next_action"][:1000] or "Human review.",
        inline=False,
    )
    embed.add_field(
        name="Flags",
        value=flags[:1000],
        inline=False,
    )

    await interaction.followup.send(embed=embed)


# ============================================================
# ERROR HANDLING
# ============================================================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        return await ctx.send(
            "❌ You don't have permission to use this command."
        )

    if isinstance(error, commands.MissingRequiredArgument):
        return await ctx.send(
            f"❌ Missing argument: `{error.param.name}`"
        )

    if isinstance(error, commands.BadArgument):
        return await ctx.send(
            "❌ Invalid user/channel/role argument."
        )

    if isinstance(error, commands.CommandOnCooldown):
        return await ctx.send(
            f"⏳ Try again in {error.retry_after:.1f}s."
        )

    if isinstance(error, commands.CheckFailure):
        return await ctx.send(
            "❌ You don't have permission to use this command."
        )

    log.exception("Command error", exc_info=error)

    try:
        await ctx.send(
            "❌ An unexpected error occurred. Check the bot logs."
        )
    except discord.HTTPException:
        pass


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    bot.run(TOKEN)
