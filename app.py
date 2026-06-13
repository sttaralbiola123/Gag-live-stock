import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import json
import os
import asyncio
from threading import Thread
from flask import Flask, jsonify
from datetime import datetime

# ---------- Configuration ----------
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
API_BASE_URL = "https://gagapi2.onrender.com"
CONFIG_FILE = "config.json"

# ---------- Flask app (runs in a separate thread) ----------
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return jsonify({"status": "alive", "service": "Grow a Garden Discord Bot"})

@flask_app.route("/health")
def health():
    return jsonify({"status": "ok", "timestamp": datetime.utcnow().isoformat()})

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

# ---------- Discord Bot Setup ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Helper functions for config ----------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ---------- Global cache for previous data ----------
# Structure: { guild_id: { "weather": {...}, "seeds": {...}, "gear": {...}, "cosmetics": {...} } }
previous_data = {}

# ---------- Background task: monitor API and send alerts ----------
@tasks.loop(seconds=60)   # 1 request per minute → safe for rate limit
async def monitor_api():
    config = load_config()
    if not config:
        return

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{API_BASE_URL}/alldata", timeout=10) as resp:
                if resp.status != 200:
                    return
                new_data = await resp.json()
        except Exception as e:
            print(f"Error fetching data: {e}")
            return

    # Process each guild that has a setup
    for guild_id_str, settings in config.items():
        guild_id = int(guild_id_str)
        channel_id = settings.get("channel_id")
        if not channel_id:
            continue

        channel = bot.get_channel(channel_id)
        if not channel:
            continue   # channel deleted or bot can't see it

        old = previous_data.get(guild_id, {})
        alerts = []   # list of (embed_title, embed_description, color)

        # ---- Weather change ----
        if settings.get("notify_weather", False):
            old_weather = old.get("weather", {}).get("weatherType")
            new_weather = new_data.get("weather", {}).get("weatherType")
            if old_weather and new_weather and old_weather != new_weather:
                embed = discord.Embed(
                    title="🌤️ Weather Update",
                    description=f"**{old_weather}** → **{new_weather}**",
                    color=discord.Color.blue(),
                    timestamp=datetime.utcnow()
                )
                alerts.append(embed)

        # ---- Stock changes helper ----
        def stock_alerts(category_key, display_name, color):
            if not settings.get(f"notify_{category_key}", False):
                return []
            old_items = old.get(category_key, {})
            new_items = new_data.get(category_key, {})
            changes = []
            for item_id, new_stock in new_items.items():
                old_stock = old_items.get(item_id, 0)
                if old_stock != new_stock:
                    changes.append(f"**{item_id}**: {old_stock} → {new_stock}")
            if changes:
                embed = discord.Embed(
                    title=f"📦 {display_name} Stock Update",
                    description="\n".join(changes[:10]),   # limit to 10 items
                    color=color,
                    timestamp=datetime.utcnow()
                )
                return [embed]
            return []

        alerts.extend(stock_alerts("seeds", "Seeds", discord.Color.green()))
        alerts.extend(stock_alerts("gear", "Gears", discord.Color.gold()))    # API uses "gear"
        alerts.extend(stock_alerts("cosmetics", "Cosmetics", discord.Color.purple()))

        # Send all alerts for this guild
        for embed in alerts:
            await channel.send(embed=embed)

        # Update cache for this guild
        previous_data[guild_id] = {
            "weather": new_data.get("weather", {}),
            "seeds": new_data.get("seeds", {}),
            "gear": new_data.get("gear", {}),
            "cosmetics": new_data.get("cosmetics", {})
        }

@monitor_api.before_loop
async def before_monitor():
    await bot.wait_until_ready()

# ---------- Slash Commands ----------
@bot.tree.command(name="setup", description="Set up auto‑notify channel and categories")
@app_commands.describe(
    channel="Channel where alerts will be sent (default: current channel)",
    weather="Notify when weather changes",
    seeds="Notify when seed stock changes",
    gears="Notify when gear stock changes",
    cosmetics="Notify when cosmetic stock changes"
)
async def setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel = None,
    weather: bool = False,
    seeds: bool = False,
    gears: bool = False,
    cosmetics: bool = False
):
    if not channel:
        channel = interaction.channel

    config = load_config()
    guild_id = str(interaction.guild_id)

    guild_config = config.get(guild_id, {})
    guild_config["channel_id"] = channel.id
    guild_config["notify_weather"] = weather
    guild_config["notify_seeds"] = seeds
    guild_config["notify_gears"] = gears
    guild_config["notify_cosmetics"] = cosmetics
    config[guild_id] = guild_config
    save_config(config)

    # Build nice embed for confirmation
    enabled = []
    if weather: enabled.append("🌤️ Weather")
    if seeds: enabled.append("🌱 Seeds")
    if gears: enabled.append("⚙️ Gears")
    if cosmetics: enabled.append("💄 Cosmetics")
    enabled_str = ", ".join(enabled) if enabled else "❌ No categories (alerts are off)"

    embed = discord.Embed(
        title="✅ Auto‑notify configured",
        description=f"**Channel:** {channel.mention}\n**Updates:** {enabled_str}",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="I will check for changes every 60 seconds.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="unsetup", description="Disable auto‑notify for this server")
async def unsetup(interaction: discord.Interaction):
    config = load_config()
    guild_id = str(interaction.guild_id)

    if guild_id in config:
        del config[guild_id]
        save_config(config)
        embed = discord.Embed(
            title="🔕 Auto‑notify disabled",
            description="I will no longer send stock or weather updates in this server.",
            color=discord.Color.red(),
            timestamp=datetime.utcnow()
        )
        await interaction.response.send_message(embed=embed)
    else:
        embed = discord.Embed(
            title="ℹ️ No active setup",
            description="This server doesn't have any auto‑notify configuration.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="health", description="Check bot and API status")
async def health(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{API_BASE_URL}/", timeout=5) as resp:
                if resp.status == 200:
                    api_status = "✅ Online"
                else:
                    api_status = f"⚠️ Status {resp.status}"
    except Exception:
        api_status = "❌ Unreachable"

    embed = discord.Embed(
        title="🩺 Health Report",
        color=discord.Color.green(),
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Discord Bot", value="🟢 Running", inline=True)
    embed.add_field(name="GAGAPI", value=api_status, inline=True)
    embed.add_field(name="Background Monitor", value="🟢 Active (every 60s)", inline=False)
    embed.set_footer(text=f"Uptime • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    await interaction.followup.send(embed=embed)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await bot.tree.sync()
    print("✅ Slash commands synced globally")

# ---------- Startup: start Flask in a thread, then run Discord bot ----------
def start_flask():
    from werkzeug.serving import make_server
    server = make_server('0.0.0.0', int(os.environ.get("PORT", 8080)), flask_app)
    server.serve_forever()

if __name__ == "__main__":
    # Start Flask in a background daemon thread
    flask_thread = Thread(target=start_flask, daemon=True)
    flask_thread.start()
    # Run Discord bot
    bot.run(BOT_TOKEN)
