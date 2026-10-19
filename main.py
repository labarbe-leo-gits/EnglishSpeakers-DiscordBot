import io
import os
import asyncio
import secrets
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

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
API_URL = os.getenv("API_URL")
EMAIL = os.getenv("EMAIL")
PASSWORD = os.getenv("PASSWORD")
GUILD_ID = os.getenv("GUILD_ID")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

# ---------- FastAPI Lifespan ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the Discord bot in the background when Uvicorn starts FastAPI."""
    if not TOKEN:
        print("[CRITICAL] DISCORD_TOKEN is missing from environment variables!")
    else:
        # Run bot.start in an asynchronous background task on the same event loop
        asyncio.create_task(bot.start(TOKEN))
        print("[INFO] Discord bot background task started.")
    yield
    # Cleanup on application shutdown
    if not bot.is_closed():
        await bot.close()
        print("[INFO] Discord bot shut down gracefully.")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Health Check Route ----------

@app.get("/")
async def root_health_check():
    """Satisfies Render's health check monitor to avoid 404 errors."""
    return {
        "status": "online",
        "bot_ready": bot.is_ready(),
        "bot_user": str(bot.user) if bot.user else None
    }

# ---------- Dependencies ----------

async def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if API_KEY:
        if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
            raise HTTPException(status_code=401, detail="Invalid or missing API key.")

async def require_bot_ready():
    if not bot.is_ready():
        raise HTTPException(status_code=503, detail="Bot is not connected to Discord yet.")

# ---------- Payloads ----------

class DMPayload(BaseModel):
    target: str
    message: str

class ChannelPayload(BaseModel):
    channel_id: str
    message: str

# ---------- Helpers ----------

async def resolve_user(target: str):
    user = None
    if target.isdigit():
        try:
            user = await bot.fetch_user(int(target))
        except discord.NotFound:
            pass

    if not user:
        for guild in bot.guilds:
            member = guild.get_member_named(target)
            if member:
                user = member
                break

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User '{target}' not found.",
        )
    return user

def insert_api_key_into_headers(headers: dict) -> dict:
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return headers

async def api_login():
    if not EMAIL or not PASSWORD:
        raise HTTPException(status_code=500, detail="API credentials are not set.")

    headers = insert_api_key_into_headers({})
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        response = await client.post(
            API_URL,
            params={"resource": "login"},
            json={"email": EMAIL, "password": PASSWORD},
            headers=headers,
        )

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to log into the API.")

    token = response.json().get("token")
    if not token:
        raise HTTPException(status_code=500, detail="API login did not return a token.")
    return token

_token: Optional[str] = None

async def api_validate_credentials(payload: dict) -> dict:
    """POSTs a login payload to the API and returns the JSON response
    ({"token": ..., "user_type": ..., "user": {...}}) if the credentials are valid."""
    headers = insert_api_key_into_headers({})
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        response = await client.post(
            API_URL,
            params={"resource": "login"},
            json=payload,
            headers=headers,
        )

    if response.status_code == 401:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="API login error.")

    data = response.json()
    if not data.get("token"):
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    return data

async def login_speaker_student_portal(unique_name: str, password: str) -> bool:
    if not unique_name or not password:
        raise HTTPException(status_code=400, detail="Credentials required.")

    await api_validate_credentials({"unique_name": unique_name, "password": password})
    return True

async def login_admin_portal(email: str, password: str) -> dict:
    """Secondary login used by the admin /pdf form (email + password)."""
    if not email or not password:
        raise HTTPException(status_code=400, detail="Credentials required.")

    # Same request as the standard (unique_name) login, but with the email instead.
    # user_type="admin" restricts it to ADMINS accounts.
    return await api_validate_credentials(
        {"email": email, "password": password, "user_type": "admin"}
    )

async def get_token(force_refresh: bool = False) -> str:
    global _token
    if force_refresh or not _token:
        _token = await api_login()
    return _token

async def auth_headers(force_refresh: bool = False) -> dict:
    headers = insert_api_key_into_headers({})
    headers["Authorization"] = f"Bearer {await get_token(force_refresh)}"
    return headers

async def fetch_speakers() -> list:
    response = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for attempt in range(2):
            headers = await auth_headers(force_refresh=(attempt == 1))
            response = await client.get(
                API_URL, params={"resource": "speakers"}, headers=headers
            )
            if response.status_code != 401:
                break

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch speakers.")

    return response.json().get("speakers", [])

async def fetch_variable(client: httpx.AsyncClient, name: str):
    """Returns the value of a variable from the API's variables route.
    The API answers {"success": true, "variable": {"variable_name": ..., "variable_value": ...}}."""
    response = None
    for attempt in range(2):
        headers = await auth_headers(force_refresh=(attempt == 1))
        response = await client.get(
            API_URL,
            params={"resource": "variables", "name": name},
            headers=headers,
        )
        if response.status_code != 401:
            break
    response.raise_for_status()
    variable = response.json().get("variable") or {}
    return variable.get("variable_value")

async def fetch_admin_attendance(admin_id: str) -> list:
    """Fetches the attendance records for an admin account by ID.
    Always returns a list, whatever shape the API answers with."""
    response = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for attempt in range(2):
            headers = await auth_headers(force_refresh=(attempt == 1))
            response = await client.get(
                API_URL,
                params={"resource": "admins", "id": admin_id, "action": "attendance"},
                headers=headers,
            )
            if response.status_code != 401:
                break

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch admin attendance.")

    data = response.json()

    # Possible shapes:
    #   {"attendance": [...]}
    #   {"admin": [...]}
    #   {"admin": {..., "attendance": [...]}}
    if isinstance(data.get("attendance"), list):
        return data["attendance"]

    admin = data.get("admin")
    if isinstance(admin, list):
        return admin
    if isinstance(admin, dict):
        records = admin.get("attendance")
        return records if isinstance(records, list) else []

    return []

def to_number(value, default):
    """The API returns numbers as strings (e.g. "10.50"). Returns an int when the
    value is whole (9.0 -> 9), otherwise a float, or `default` if it isn't a number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if number == int(number) else number

def find_speaker_by_discord(speakers: list, author) -> Optional[dict]:
    """Returns the speaker whose linked discord_username matches the Discord user."""
    user_name = author.name.lower()
    full_user = str(author).lower()
    for speaker in speakers:
        api_username = (speaker.get("discord_username") or "").lower()
        if api_username and api_username in (user_name, full_user):
            return speaker
    return None

def format_date(value) -> str:
    """Converts YYYY-MM-DD to DD/MM/YYYY. Never raises."""
    if not isinstance(value, str) or not value:
        return "Unknown Date"
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value

def build_attendance_value(records: list) -> str:
    """Builds an embed field value that always respects Discord's 1-1024 char limit."""
    lines = []
    for record in records:
        if isinstance(record, dict):
            date = format_date(record.get("session_date"))
            status = record.get("status") or "Present"
            session = record.get("session_name") or ""
            lines.append(f"• **{date}** - {status}" + (f" ({session})" if session else ""))
        else:
            lines.append(f"• **{record}**")

    if not lines:
        return "No attendance records found."

    value = "\n".join(lines[:15])
    if len(value) > 1024:
        value = value[:1021] + "..."
    return value

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
            raise HTTPException(status_code=403, detail="Bot cannot access channel.")

    if not hasattr(channel, "send"):
        raise HTTPException(status_code=400, detail="Channel cannot receive messages.")
    return channel

async def _post_form_and_print_pdf(export_url: str, fields: dict) -> bytes:
    """Opens the export page, POSTs `fields` through a generated form and prints
    the result to PDF. Values are passed to the browser as arguments (not pasted
    into the JS), so quotes in a password can't break it."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(export_url)

            await page.evaluate(
                """([action, fields]) => {
                    const form = document.createElement('form');
                    form.method = 'POST';
                    form.action = action;

                    for (const [key, value] of Object.entries(fields)) {
                        const input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = key;
                        input.value = value;
                        form.appendChild(input);
                    }

                    document.body.appendChild(form);
                    form.submit();
                }""",
                [export_url, fields],
            )

            await page.wait_for_load_state("networkidle")
            return await page.pdf(format="A4", print_background=True)
        finally:
            await browser.close()

async def render_member_pdf(member_id: str, unique_name: str, password: str) -> bytes:
    """Export request for speakers: POSTs member_id / unique_name / password."""
    return await _post_form_and_print_pdf(
        "https://hainu.fr/englishspeakers/public/export_member_pdf.php",
        {
            "member_id": member_id,
            "unique_name": unique_name,
            "password": password,
        },
    )

async def render_admin_pdf(admin_id: str, email: str, password: str) -> bytes:
    """Export request for admins: POSTs admin_id / email / password."""
    return await _post_form_and_print_pdf(
        "https://hainu.fr/englishspeakers/common/export_admin_pdf.php",
        {
            "admin_id": admin_id,
            "email": email,
            "password": password,
        },
    )

async def generate_member_pdf(member_id: str, unique_name: str, password: str) -> bytes:
    credentials_valid = await login_speaker_student_portal(unique_name, password)
    if not credentials_valid:
        raise HTTPException(status_code=401, detail="Invalid credentials.")

    return await render_member_pdf(member_id, unique_name, password)

async def generate_admin_member_pdf(
    member_id: str, admin_unique_name: str, email: str, password: str
) -> bytes:
    login = await login_admin_portal(email, password)

    # The credentials must belong to the admin account linked to this Discord user
    if str(login.get("user", {}).get("id")) != str(member_id):
        raise HTTPException(
            status_code=401,
            detail="These credentials don't match your linked admin account.",
        )

    return await render_admin_pdf(member_id, email, password)

async def find_admin_by_discord(author) -> Optional[dict]:
    """Look up an admin account by Discord username.
    Returns the admin dict, or None if there is no match."""
    candidates = []
    for name in (author.name, str(author)):
        if name and name not in candidates:
            candidates.append(name)

    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
        for candidate in candidates:
            response = None
            for attempt in range(2):
                headers = await auth_headers(force_refresh=(attempt == 1))
                response = await client.get(
                    API_URL,
                    params={"resource": "admins", "discord_username": candidate},
                    headers=headers,
                )
                if response.status_code != 401:
                    break

            if response.status_code == 404:
                continue  # not an admin under this name, try the next one

            response.raise_for_status()
            data = response.json()
            if data.get("success") and data.get("admin"):
                return data["admin"]

    return None

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
        # Loading message (ephemeral), replaced by the PDF afterwards
        await interaction.response.send_message(
            "⏳ Generating your PDF report, please wait...",
            ephemeral=True
        )

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

            await interaction.edit_original_response(
                content="Here is your exported PDF report:",
                attachments=[file]
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"Error generating PDF. Please check your credentials and try again.\nDetails: {str(e)}",
                attachments=[]
            )

class AdminPDFAuthModal(discord.ui.Modal, title="Export Admin PDF"):
    email = discord.ui.TextInput(
        label="Email",
        placeholder="Enter your admin email...",
        required=True
    )
    password = discord.ui.TextInput(
        label="Password",
        style=discord.TextStyle.short,
        placeholder="Enter your password...",
        required=True
    )

    def __init__(self, member_id: str, admin_unique_name: str = ""):
        super().__init__()
        self.member_id = member_id
        self.admin_unique_name = admin_unique_name

    async def on_submit(self, interaction: discord.Interaction):
        # Loading message (ephemeral), replaced by the PDF afterwards
        await interaction.response.send_message(
            "⏳ Generating your PDF report, please wait...",
            ephemeral=True
        )

        try:
            pdf_bytes = await generate_admin_member_pdf(
                member_id=self.member_id,
                admin_unique_name=self.admin_unique_name,
                email=self.email.value.strip(),
                password=self.password.value
            )

            file = discord.File(
                fp=io.BytesIO(pdf_bytes),
                filename=f"admin_report_{self.member_id}.pdf"
            )

            await interaction.edit_original_response(
                content="Here is your exported PDF report:",
                attachments=[file]
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"Error generating PDF. Please check your credentials and try again.\nDetails: {str(e)}",
                attachments=[]
            )

# ---------- Bot Commands ----------

@bot.hybrid_command(
    name="points",
    description="Check your points balance automatically using your linked Discord account."
)
async def points(ctx: commands.Context):
    # Ephemeral defer: the ephemeral flag of later messages is ignored otherwise.
    await ctx.defer(ephemeral=True)

    try:
        speakers = await fetch_speakers()
    except HTTPException as e:
        await ctx.send(f"Error fetching data: {e.detail}", ephemeral=True)
        return

    matched_speaker = find_speaker_by_discord(speakers, ctx.author)

    if not matched_speaker:
        # Try checking for an admin account linked to this Discord user
        try:
            admin = await find_admin_by_discord(ctx.author)
        except Exception as e:
            print(f"[points] admin lookup failed: {e}")
            admin = None

        if admin:
            points_value = admin.get("points", "N/A")
            embed = discord.Embed(title="Your Points Balance", color=discord.Color.blue())
            embed.add_field(name="First Name", value=admin.get("first_name") or "N/A", inline=True)
            embed.add_field(name="Last Name", value=admin.get("last_name") or "N/A", inline=True)
            embed.add_field(name="Class", value=admin.get("class") or "N/A", inline=True)
            embed.add_field(name="Points", value=str(points_value), inline=False)
            embed.add_field(
                name="Login to the website",
                value="[Click here to log in](http://hainu.fr/englishspeakers/public/login.php) to view attendance and achievements! (Alternatively, you can use the `/attendance` command.)",
                inline=False,
            )
            await ctx.send(embed=embed, ephemeral=True)
        
        return

    points_value = matched_speaker.get("points", "N/A")

    embed = discord.Embed(title="Your Points Balance", color=discord.Color.blue())
    embed.add_field(name="First Name", value=matched_speaker.get("first_name") or "N/A", inline=True)
    embed.add_field(name="Last Name", value=matched_speaker.get("last_name") or "N/A", inline=True)
    embed.add_field(name="Class", value=matched_speaker.get("class") or "N/A", inline=True)
    embed.add_field(name="Points", value=str(points_value), inline=False)
    embed.add_field(
        name="Login to the website",
        value="[Click here to log in](http://hainu.fr/englishspeakers/student/login.php) to view attendance and achievements! (Alternatively, you can use the `/attendance` command.)",
        inline=False,
    )

    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="info", description="Get helpful links and portal resources.")
async def info(ctx: commands.Context):
    links = [
        ("App Download", "https://hainu.fr/englishspeakers/public/download.php"),
        ("About Us", "https://hainu.fr/englishspeakers/public/about.php"),
        ("Events / Blog", "https://hainu.fr/englishspeakers/public/blog.php"),
        ("Points Leaderboard", "https://hainu.fr/englishspeakers/public/points.php"),
        ("Contact", "https://hainu.fr/englishspeakers/public/contact.php"),
        ("Student Login", "https://hainu.fr/englishspeakers/student/login.php"),
        ("MyGES (School Portal)", "https://myges.fr"),
        ("Discord Community", "https://discord.gg/7MWxC8azvM"),
        ("Term of Service", "https://hainu.fr/englishspeakers/legal/tos.php"),
        ("Privacy Policy", "https://hainu.fr/englishspeakers/legal/privacy.php")
    ]

    linear_text = "\n".join([f"**{title}** : {url}" for title, url in links])

    embed = discord.Embed(
        title="Useful Links & Resources",
        description=linear_text,
        color=discord.Color.blue()
    )

    await ctx.send(embed=embed)

@bot.hybrid_command(name="help", description="Display all available bot commands.")
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="Bot Help & Commands",
        description="Here is a list of all available commands:",
        color=discord.Color.blue()
    )

    embed.add_field(name="`/points`", value="Check your points balance linked to your Discord username.", inline=False)
    embed.add_field(name="`/info`", value="View all official web pages, student portals, and community links.", inline=False)
    embed.add_field(name="`/pdf`", value="Generates and exports your member report as a PDF document.", inline=False)
    embed.add_field(name="`/details`", value="Get details about maximum points and points per session.", inline=False)
    embed.add_field(name="`/attendance`", value="Check your attendance linked to your Discord username.", inline=False)
    embed.add_field(name="`/events`", value="Display the upcoming events from the website's event calendar.", inline=False)
    embed.add_field(name="`/me`", value="Get all available information about your English Speakers account.", inline=False)
    embed.add_field(name="`/rgpd`", value="Get information about your data privacy and rights under GDPR.", inline=False)
    embed.add_field(name="`/help`", value="Display this help message with command info.", inline=False)

    await ctx.send(embed=embed)

@bot.tree.command(name="pdf", description="Prompt for credentials and export your member PDF.")
async def pdf_command(interaction: discord.Interaction):
    try:
        speakers = await fetch_speakers()
    except Exception as e:
        await interaction.response.send_message(f"Error fetching speaker records: {e}", ephemeral=True)
        return

    matched_speaker = find_speaker_by_discord(speakers, interaction.user)

    if matched_speaker:
        speaker_id = str(matched_speaker.get("id") or matched_speaker.get("member_id"))
        await interaction.response.send_modal(PDFAuthModal(member_id=speaker_id))
        return

    # Not a speaker -> check whether this Discord account belongs to an admin
    try:
        admin = await find_admin_by_discord(interaction.user)
    except Exception as e:
        print(f"[pdf] admin lookup failed: {e}")
        admin = None

    if admin:
        await interaction.response.send_modal(
            AdminPDFAuthModal(
                member_id=str(admin.get("id")),
                admin_unique_name=admin.get("unique_name") or "",
            )
        )
        return

    await interaction.response.send_message(
        "No account is linked to your Discord account.",
        ephemeral=True
    )

@bot.hybrid_command(name="attendance", description="Check your attendance record.")
async def attendance(ctx: commands.Context):
    # The ephemeral defer already shows the "thinking" state, no extra message needed.
    await ctx.defer(ephemeral=True)

    try:
        speakers = await fetch_speakers()
    except Exception:
        await ctx.send(
            "Unable to retrieve speaker records at this time. Please try again later.",
            ephemeral=True,
        )
        return

    matched_speaker = find_speaker_by_discord(speakers, ctx.author)

    # ----- Admin path -----
    if not matched_speaker:
        try:
            admin = await find_admin_by_discord(ctx.author)
        except Exception as e:
            print(f"[attendance] admin lookup failed: {e}")
            admin = None

        if not admin:
            await ctx.send("No account is linked to your Discord username.", ephemeral=True)
            return

        try:
            attendance_list = await fetch_admin_attendance(str(admin.get("id")))
        except Exception as e:
            print(f"[attendance] admin attendance failed: {e}")
            await ctx.send(
                "Unable to retrieve admin attendance at this time. Please try again later.",
                ephemeral=True,
            )
            return

        if not attendance_list:
            await ctx.send("No attendance record found for this admin account.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Attendance for {admin.get('full_name') or ctx.author.display_name}",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name=f"Attendance ({len(attendance_list)})",
            value=build_attendance_value(attendance_list),
            inline=False,
        )
        embed.set_footer(text="English Speakers Association")

        # This send was missing: without it the deferred response never completes.
        await ctx.send(embed=embed, ephemeral=True)
        return

    # ----- Speaker path -----
    speaker_id = matched_speaker.get("id") or matched_speaker.get("member_id")
    if not speaker_id:
        await ctx.send("Invalid speaker account data (missing ID).", ephemeral=True)
        return

    # Authenticated request (with token refresh on 401)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            response = None
            for attempt in range(2):
                headers = await auth_headers(force_refresh=(attempt == 1))
                response = await client.get(
                    API_URL,
                    params={"resource": "speakers", "id": speaker_id},
                    headers=headers,
                )
                if response.status_code != 401:
                    break
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError, HTTPException):
        await ctx.send(
            "Unable to retrieve attendance details at this time. Please try again later.",
            ephemeral=True,
        )
        return

    if not data.get("success") or not data.get("speaker"):
        await ctx.send("No attendance record found for this ID.", ephemeral=True)
        return

    speaker = data["speaker"]
    attendance_list = speaker.get("attendance") or []

    admin = await find_admin_by_discord(ctx.author)
    full_name = (
        admin.get("full_name")
        or " ".join(filter(None, [admin.get("first_name"), admin.get("last_name")]))
            or admin.get("username")
            or ctx.author.display_name
        )

    embed = discord.Embed(
        title=f"Attendance for {full_name}",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Class", value=speaker.get("class") or "N/A", inline=True)
    embed.add_field(name="Points", value=str(speaker.get("points", 0)), inline=True)
    embed.add_field(
        name=f"Attendance ({len(attendance_list)})",
        value=build_attendance_value(attendance_list),
        inline=False,
    )

    embed.set_footer(text="English Speakers Association")
    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="events", description="Display upcoming events from the website's event calendar.")
async def events(ctx: commands.Context):
    await ctx.defer(ephemeral=True)

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            headers = await auth_headers()
            response = await client.get(
                API_URL,
                params={"resource": "events"},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError, HTTPException):
        await ctx.send(
            "Unable to retrieve events at this time. Please try again later.",
            ephemeral=True,
        )
        return

    events_list = data.get("events", [])
    if not events_list:
        await ctx.send("No upcoming events found.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Upcoming Events",
        color=discord.Color.blue(),
    )

    today = datetime.now().date()
    shown = 0

    for event in events_list:
        if shown >= 10:  # Limit to 10 events
            break

        name = event.get("event_name") or "Unnamed Event"
        raw_date = event.get("event_date") or ""
        description = event.get("description") or "No description available."

        if not description.strip():
            description = "No description available."
        if len(description) > 1024:
            description = description[:1021] + "..."

        # Format date as "DD/MM/YYYY" and keep only events today or in the future
        display_date = raw_date or "Unknown Date"
        try:
            parsed = datetime.strptime(raw_date, "%Y-%m-%d")
            if parsed.date() < today:
                continue  # Skip past events
            display_date = parsed.strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            pass  # Unparseable date: include the event anyway

        field_name = f"{name} - {display_date}"
        if len(field_name) > 256:
            field_name = field_name[:253] + "..."

        embed.add_field(name=field_name, value=description, inline=False)
        shown += 1

    if shown == 0:
        await ctx.send("No upcoming events found.", ephemeral=True)
        return

    embed.set_footer(text="English Speakers Association")
    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="me", description="Get all available information about your English Speakers account.")
async def me(ctx: commands.Context):
    await ctx.defer(ephemeral=True)

    try:
        speakers = await fetch_speakers()
    except Exception as e:
        await ctx.send(f"Error fetching speaker records: {e}", ephemeral=True)
        return

    matched = find_speaker_by_discord(speakers, ctx.author)
    account_type = "Speaker"

    # Not a speaker -> check whether this Discord account belongs to an admin
    if not matched:
        try:
            admin = await find_admin_by_discord(ctx.author)
        except Exception as e:
            print(f"[me] admin lookup failed: {e}")
            admin = None

        if admin:
            matched = admin
            account_type = "Admin"

    if not matched:
        await ctx.send(
            "No account is linked to your Discord username.",
            ephemeral=True,
        )
        return

    full_name = (
        matched.get("full_name")
        or " ".join(filter(None, [matched.get("first_name"), matched.get("last_name")]))
        or matched.get("username")
        or ctx.author.display_name
    )
    points = matched.get("points")

    embed = discord.Embed(
        title=f"Account Information for {full_name}",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Account Type", value=account_type, inline=True)
    embed.add_field(name="Member ID", value=str(matched.get("id") or matched.get("member_id")), inline=True)
    embed.add_field(name="First Name", value=matched.get("first_name") or "N/A", inline=True)
    embed.add_field(name="Last Name", value=matched.get("last_name") or "N/A", inline=True)
    embed.add_field(name="Full Name", value=full_name, inline=True)
    embed.add_field(name="Unique Name", value=matched.get("unique_name") or matched.get("username") or "N/A", inline=True)
    embed.add_field(name="Class", value=matched.get("class") or "N/A", inline=True)

    max_points = 9
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            max_points = to_number(await fetch_variable(client, "max_points"), 9)
    except Exception as e:
        print(f"[me] max_points lookup failed: {e}")

    points_number = to_number(points, None)
    if points_number is None:
        display_points = "N/A"
    else:
        cap = max_points + 3 if account_type == "Admin" else max_points
        display_points = f"{min(points_number, cap)} / {cap}"

    embed.add_field(name="Points", value=display_points, inline=True)
    embed.add_field(name="Discord Username", value=matched.get("discord_username") or "Not Linked", inline=True)
    embed.add_field(
        name="Login to the website",
        value="[Click here to log in](http://hainu.fr/englishspeakers/student/login.php) to view attendance and achievements! (Alternatively, you can use the `/attendance` command.)",
        inline=False,
    )

    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="details", description="Get details about points.")
async def details(ctx: commands.Context):
    await ctx.defer(ephemeral=True)

    max_points = 9
    pts_per_session = 1
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            max_points = to_number(await fetch_variable(client, "max_points"), 9)
            pts_per_session = to_number(await fetch_variable(client, "pts_per_sessions"), 1)
    except Exception as e:
        print(f"[details] variable lookup failed: {e}")

    embed = discord.Embed(
        title="Points Details",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Maximum Points", value=str(max_points), inline=True)
    embed.add_field(name="Points per Session", value=str(pts_per_session), inline=True)
    embed.set_footer(text="English Speakers Association")

    await ctx.send(embed=embed, ephemeral=True)

@bot.hybrid_command(name="rgpd", description="Get information about your data privacy and rights under GDPR.")
async def rgpd(ctx: commands.Context):
    await ctx.defer(ephemeral=True)

    embed = discord.Embed(
        title="Data Privacy & GDPR Information",
        description=(
            "We respect your privacy and comply with the General Data Protection Regulation (GDPR). "
            "You have the right to access, rectify, or request the deletion of your personal data. "
            "Some data is necessary for account management and cannot be deleted if it affects your account functionality, except if you wish to delete your account entirely, meaning any points, attendance, and achievements will be permanently removed.\n"
            "For more information, please visit our [Privacy Policy](https://hainu.fr/englishspeakers/legal/privacy.php) and [Terms of Service](https://hainu.fr/englishspeakers/legal/tos.php). "
            "If you wish to see what data we have on you, you can use the `/me` command to view your account information."
        ),
        color=discord.Color.blue(),
    )

    await ctx.send(embed=embed, ephemeral=True)

# ---------- Error Handlers (prevent "thinking..." forever) ----------

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return

    original = getattr(error, "original", error)
    print(f"[command error] /{getattr(ctx.command, 'name', '?')}: {original!r}")

    try:
        await ctx.send(
            "Something went wrong while running this command. Please try again later.",
            ephemeral=True,
        )
    except Exception as send_error:
        print(f"[command error] could not notify user: {send_error!r}")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    print(f"[app command error] {original!r}")

    message = "Something went wrong while running this command. Please try again later."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception as send_error:
        print(f"[app command error] could not notify user: {send_error!r}")

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
            detail="Cannot send DM to this user. DMs closed or bot blocked.",
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
        raise HTTPException(status_code=403, detail="Missing permission in channel.")
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
            detail="Missing permission in channel.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------- Bot Events ----------

@bot.event
async def setup_hook():
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

# Local execution fallback
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)