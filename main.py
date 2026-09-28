import io
import os
import asyncio
import secrets
from typing import Optional

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
API_KEY = os.getenv("API_KEY")
API_URL = os.getenv("API_URL")  # bare script URL, e.g. https://example.com/api.php
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")

# Optional: set GUILD_ID in .env for instant slash-command sync while testing
GUILD_ID = os.getenv("GUILD_ID")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Remove default help command to prevent registration collision
bot.remove_command("help")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # must be False when allow_origins is "*"
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Dependencies ----------

async def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    """Only enforced if API_KEY is set in the environment."""
    if API_KEY:
        if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")


async def require_bot_ready():
    if not bot.is_ready():
        raise HTTPException(status_code=503, detail="Bot is not connected to Discord yet.")


# ---------- Payloads ----------

class DMPayload(BaseModel):
    target: str  # User ID (recommended) or exact username
    message: str


class ChannelPayload(BaseModel):
    channel_id: str
    message: str


# ---------- Helpers ----------

async def resolve_user(target: str):
    user = None

    # 1. Try User ID first (numeric string)
    if target.isdigit():
        try:
            user = await bot.fetch_user(int(target))
        except discord.NotFound:
            pass

    # 2. Otherwise search cached server members by username
    if not user:
        for guild in bot.guilds:
            member = guild.get_member_named(target)
            if member:
                user = member
                break

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User '{target}' not found. Make sure the bot shares a server with them or use their User ID.",
        )
    return user


def insert_api_key_into_headers(headers: dict) -> dict:
    """Adds the API key to the headers if it's set in the environment."""
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return headers


async def api_login():
    """Logs into the API using the provided email and password and returns the token."""
    if not EMAIL or not PASSWORD:
        raise HTTPException(status_code=500, detail="API credentials are not set in the environment.")

    headers = insert_api_key_into_headers({})

    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        response = await client.post(
            API_URL,
            params={"resource": "login"},
            json={"email": EMAIL, "password": PASSWORD},
            headers=headers,
        )

    if response.status_code != 200:
        print(f"[API login] {response.status_code} {response.url}\n{response.text[:500]}")
        raise HTTPException(status_code=500, detail="Failed to log into the API.")

    token = response.json().get("token")
    if not token:
        raise HTTPException(status_code=500, detail="API login response did not contain a token.")
    return token


_token: Optional[str] = None  # cached login token


async def login_speaker_student_portal(unique_name: str, password: str) -> bool:
    """Login as a student using unique_name ans password"""
    if not unique_name or not password:
        raise HTTPException(status_code=400, detail="Unique name and password are required.")

    # Use the API to verify credentials instead of scraping the portal directly
    headers = insert_api_key_into_headers({})
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        response = await client.post(
            API_URL,
            params={"resource": "login"},
            json={"unique_name": unique_name, "password": password},
            headers=headers,
        )

    if response.status_code != 200:
        print(f"[API login] {response.status_code} {response.url}\n{response.text[:500]}")
        raise HTTPException(status_code=500, detail="Failed to log into the API.")

    token = response.json().get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Invalid unique name or password.")

    return True  # Login successful


async def get_token(force_refresh: bool = False) -> str:
    """Returns the cached API token, logging in first if needed."""
    global _token
    if force_refresh or not _token:
        _token = await api_login()
    return _token


async def auth_headers(force_refresh: bool = False) -> dict:
    """Builds headers with the optional X-API-Key and the Bearer token."""
    headers = insert_api_key_into_headers({})
    headers["Authorization"] = f"Bearer {await get_token(force_refresh)}"
    return headers


async def fetch_speakers() -> list:
    """Fetches all speakers. The API returns {"success": true, "speakers": [...]}."""
    response = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for attempt in range(2):
            # On the retry, force a fresh login in case the cached token expired
            headers = await auth_headers(force_refresh=(attempt == 1))
            response = await client.get(
                API_URL, params={"resource": "speakers"}, headers=headers
            )
            if response.status_code != 401:
                break

    if response.status_code != 200:
        print(f"[API] {response.status_code} {response.url}\n{response.text[:500]}")
        raise HTTPException(status_code=500, detail="Failed to fetch speakers from the API.")

    return response.json().get("speakers", [])


async def resolve_channel(channel_id: str):
    if not channel_id.isdigit():
        raise HTTPException(status_code=400, detail="Channel ID must be numeric.")

    cid = int(channel_id)
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except discord.NotFound:
            raise HTTPException(status_code=404, detail="Channel not found.")
        except discord.Forbidden:
            raise HTTPException(status_code=403, detail="Bot can't access that channel.")

    if not hasattr(channel, "send"):
        raise HTTPException(status_code=400, detail="That channel can't receive messages.")
    return channel


async def generate_member_pdf(member_id: str, unique_name: str, password: str) -> bytes:
    """Uses Playwright to POST member credentials and export the page as a PDF."""

    # Verify the credentials from the modal before attempting to generate the PDF
    credentials_valid = await login_speaker_student_portal(unique_name, password)
    if not credentials_valid:
        raise HTTPException(status_code=401, detail="Invalid unique name or password.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        export_url = "https://hainu.fr/englishspeakers/public/export_member_pdf.php"
        await page.goto(export_url)

        # Submit POST form with credentials and member_id
        await page.evaluate(f"""
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '{export_url}';
            
            const fields = {{
                'member_id': '{member_id}',
                'unique_name': '{unique_name}',
                'password': '{password}'
            }};
            
            for (const [key, value] of Object.entries(fields)) {{
                const input = document.createElement('input');
                input.type = 'hidden';
                input.name = key;
                input.value = value;
                form.appendChild(input);
            }}
            
            document.body.appendChild(form);
            form.submit();
        """)

        await page.wait_for_load_state("networkidle")
        pdf_bytes = await page.pdf(format="A4", print_background=True)
        await browser.close()
        return pdf_bytes


# ---------- Modals ----------

class PDFAuthModal(discord.ui.Modal, title="Export Member PDF"):
    unique_name = discord.ui.TextInput(
        label="Unique Name",
        placeholder="Enter your unique name...",
        required=True
    )
    password = discord.ui.TextInput(
        label="Password",
        style=discord.TextStyle.short,
        placeholder="Enter your password...",
        required=True
    )

    def __init__(self, member_id: str):
        super().__init__()
        self.member_id = member_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            pdf_bytes = await generate_member_pdf(
                member_id=self.member_id,
                unique_name=self.unique_name.value,
                password=self.password.value
            )

            file = discord.File(
                fp=io.BytesIO(pdf_bytes),
                filename=f"member_report_{self.member_id}.pdf"
            )

            await interaction.followup.send(
                content="Here is your exported PDF report:",
                file=file,
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(
                content=f"Error generating PDF. Please check your credentials and try again.\nDetails: {str(e)}",
                ephemeral=True
            )


# ---------- Bot Commands ----------

@bot.hybrid_command(
    name="points", 
    description="Check your points balance automatically using your linked Discord account."
)
async def points(ctx: commands.Context):
    await ctx.defer()

    user_name = ctx.author.name.lower()
    full_user = str(ctx.author).lower()

    try:
        speakers = await fetch_speakers()
    except HTTPException as e:
        await ctx.send(f"Error fetching data: {e.detail}")
        return

    matched_speaker = None
    for speaker in speakers:
        api_username = (speaker.get("discord_username") or "").lower()
        if api_username and api_username in (user_name, full_user):
            matched_speaker = speaker
            break

    if not matched_speaker:
        await ctx.send("No speaker account is linked to your Discord username.")
        return

    points_value = matched_speaker.get("points", "N/A")

    embed = discord.Embed(title="Your Points Balance", color=discord.Color.blue())
    embed.add_field(name="First Name", value=matched_speaker.get("first_name", "N/A"), inline=True)
    embed.add_field(name="Last Name", value=matched_speaker.get("last_name", "N/A"), inline=True)
    embed.add_field(name="Class", value=matched_speaker.get("class") or "N/A", inline=True)
    embed.add_field(name="Points", value=str(points_value), inline=False)
    embed.add_field(
        name="Login to the website",
        value="[Click here to log in](http://hainu.fr/englishspeakers/student/login.php) to view attendance and achievements!",
        inline=False,
    )

    await ctx.send(embed=embed)


@bot.hybrid_command(name="info", description="Get helpful links and portal resources.")
async def info(ctx: commands.Context):
    links = [
        ("App Download", "https://hainu.fr/public/download.php"),
        ("About Us", "https://hainu.fr/public/about.php"),
        ("Events / Blog", "https://hainu.fr/public/blog.php"),
        ("Points Leaderboard", "https://hainu.fr/public/points.php"),
        ("Contact", "https://hainu.fr/public/contact.php"),
        ("Student Login", "https://hainu.fr/student/login.php"),
        ("MyGES (School Portal)", "https://myges.fr"),
        ("Discord Community", "https://discord.gg/7MWxC8azvM")
    ]
    
    linear_text = "\n".join([f"**{title}** : {url}" for title, url in links])

    embed = discord.Embed(
        title="Useful Links & Resources",
        description=linear_text,
        color=discord.Color.green()
    )

    await ctx.send(embed=embed)


@bot.hybrid_command(name="help", description="Display all available bot commands.")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="Bot Help & Commands",
        description="Here is a list of all available commands:",
        color=discord.Color.purple()
    )
    
    embed.add_field(
        name="`/points`",
        value="Check your points balance linked to your Discord username.",
        inline=False
    )
    embed.add_field(
        name="`/info`",
        value="View all official web pages, student portals, and community links.",
        inline=False
    )
    embed.add_field(
        name="`/pdf`",
        value="Generates and exports your member report as a PDF document.",
        inline=False
    )
    embed.add_field(
        name="`/ping`",
        value="Check the bot's current connection latency.",
        inline=False
    )
    embed.add_field(
        name="`/help`",
        value="Display this help message with command info.",
        inline=False
    )

    await ctx.send(embed=embed)

"""
@bot.hybrid_command(name="ping", description="Check the bot's latency.")
async def ping(ctx: commands.Context):
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"Pong! 🏓 `{latency_ms} ms`")
"""

@bot.tree.command(name="pdf", description="Prompt for credentials and export your member PDF.")
async def pdf_command(interaction: discord.Interaction):
    user_name = interaction.user.name.lower()
    full_user = str(interaction.user).lower()

    try:
        speakers = await fetch_speakers()
    except Exception as e:
        await interaction.response.send_message(f"Error fetching speaker records: {e}", ephemeral=True)
        return

    matched_speaker = None
    for speaker in speakers:
        api_username = (speaker.get("discord_username") or "").lower()
        if api_username and api_username in (user_name, full_user):
            matched_speaker = speaker
            break

    if not matched_speaker:
        await interaction.response.send_message(
            "No speaker account is linked to your Discord account.",
            ephemeral=True
        )
        return

    speaker_id = str(matched_speaker.get("id") or matched_speaker.get("member_id"))
    await interaction.response.send_modal(PDFAuthModal(member_id=speaker_id))

# /attendance command is not there yet, but we send a placeholder message for now
@bot.hybrid_command(name="attendance", description="Check your attendance record.")
async def attendance(ctx: commands.Context):
    await ctx.send(
        "The `/attendance` command is not implemented yet. Please check the website for your attendance record.",
        ephemeral=True
    )

# ---------- Endpoints ----------

@app.post("/send-dm", dependencies=[Depends(require_api_key), Depends(require_bot_ready)])
async def send_dm(payload: DMPayload):
    user = await resolve_user(payload.target)

    try:
        dm_channel = user.dm_channel or await user.create_dm()
        await dm_channel.send(payload.message)
        return {"status": "success", "recipient": str(user), "message": payload.message}
    except discord.Forbidden:
        raise HTTPException(
            status_code=403,
            detail="Cannot send DM to this user. Their DMs may be closed or they blocked the bot.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trigger-command", dependencies=[Depends(require_api_key), Depends(require_bot_ready)])
async def trigger_command(payload: ChannelPayload):
    channel = await resolve_channel(payload.channel_id)

    try:
        await channel.send(payload.message)
        return {"status": "success"}
    except discord.Forbidden:
        raise HTTPException(status_code=403, detail="Missing permission to send messages in that channel.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/trigger-embed", dependencies=[Depends(require_api_key), Depends(require_bot_ready)])
async def trigger_embed(payload: ChannelPayload):
    channel = await resolve_channel(payload.channel_id)

    try:
        embed = discord.Embed(description=payload.message, color=discord.Color.blue())
        await channel.send(embed=embed)
        return {"status": "success"}
    except discord.Forbidden:
        raise HTTPException(
            status_code=403,
            detail="Missing permission in that channel (needs Send Messages + Embed Links).",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------- Bot Events ----------

@bot.event
async def setup_hook():
    """Runs once before the bot connects. Registers and syncs slash commands."""
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash command(s) to Guild ID {GUILD_ID}.")
    else:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} global slash command(s).")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


async def main():
    config = uvicorn.Config(app=app, host="0.0.0.0", port=8000)
    server = uvicorn.Server(config)
    await asyncio.gather(bot.start(TOKEN), server.serve())


if __name__ == "__main__":
    asyncio.run(main())