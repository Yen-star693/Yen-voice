import discord
from discord.ext import commands
import requests
import asyncio
import edge_tts
import os
import atexit
import imageio_ffmpeg
import re
import time
import yt_dlp
import random
import string

from flask import Flask, request, jsonify
from threading import Thread

# ================= LEADER LOCK =================

LOCK_FILE = "yen_bot.lock"
IS_LEADER = False

def acquire_lock():
    global IS_LEADER
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        IS_LEADER = True
        return True
    except FileExistsError:
        return False

def release_lock():
    global IS_LEADER
    if IS_LEADER:
        try:
            os.remove(LOCK_FILE)
        except FileNotFoundError:
            pass
        IS_LEADER = False

atexit.register(release_lock)

# ================= KEEP ALIVE =================

app = Flask('')

@app.route('/')
def home():
    return "Yen Voice Bot Online"

# ================= TTS HTTP ENDPOINT =================
# Add API_SECRET as an environment variable on Render.
# The Android app sends this as the X-Api-Key header.

API_SECRET = os.getenv("API_SECRET", "")

@app.route('/tts', methods=['POST'])
def tts_endpoint():
    # Verify secret
    provided_key = request.headers.get('X-Api-Key', '')
    if not API_SECRET or provided_key != API_SECRET:
        print("TTS: unauthorized request", flush=True)
        return jsonify({'error': 'unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    text = data.get('text', '').strip()

    if not text:
        return jsonify({'error': 'no text provided'}), 400

    print(f"TTS request: {text[:100]}", flush=True)

    # Find active voice client (optional: pass guild_id in request body to target a specific server)
    target_vc = None
    guild_id_param = data.get('guild_id')

    if guild_id_param:
        guild = bot.get_guild(int(guild_id_param))
        if guild:
            target_vc = guild.voice_client
    else:
        for vc in bot.voice_clients:
            if vc.is_connected():
                target_vc = vc
                break

    if not target_vc or not target_vc.is_connected():
        print("TTS: bot not connected to a voice channel", flush=True)
        return jsonify({'error': 'bot not in any voice channel'}), 404

    try:
        # Update idle timer so bot doesn't auto-disconnect mid-speech
        last_activity[target_vc.guild.id] = time.time()

        # Bridge async speak() to sync Flask thread
        future = asyncio.run_coroutine_threadsafe(
            speak(target_vc, text),
            bot.loop
        )
        future.result(timeout=15)
        print(f"TTS success: {text[:100]}", flush=True)
        return jsonify({'ok': True, 'text': text})

    except Exception as e:
        import traceback
        print("TTS Endpoint Error:", e, flush=True)
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ================= END TTS ENDPOINT =================

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# ================= CONFIG =================

TOKEN = os.getenv("TOKEN") or "TOKEN"
GROQ_KEY = os.getenv("GROQ_KEY") or "GROQ_KEY"

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

IDLE_TIMEOUT = 240

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="yen ", intents=intents)

last_activity = {}
conversation_history = {}

# ================= PLAYER STATE =================
# Per-guild playback state for queue/seek/speed support.
# players[guild_id] = {
#     "queue": [ {query, title, filename, requester, duration}, ... ],
#     "current": {title, filename, requester, duration} or None,
#     "position": float seconds already played (base position before current segment started),
#     "speed": 1 or 2,
#     "started_at": time.time() when current segment began playing,
#     "lock": asyncio.Lock() to serialize skip/seek/play-next operations,
# }
players = {}

def get_player(guild_id):
    if guild_id not in players:
        players[guild_id] = {
            "queue": [],
            "current": None,
            "position": 0.0,
            "speed": 1,
            "started_at": None,
            "lock": asyncio.Lock(),
            "now_playing_message": None,
            "suppress_advance": False,
        }
    return players[guild_id]

def status_embed(message, error=False):
    """Small embed wrapper for plain status/error replies, so every command
    response looks consistent instead of mixing plain text and embeds."""
    embed = discord.Embed(
        title="Error" if error else "Yen Music",
        description=message,
        color=discord.Color.red() if error else discord.Color.blurple(),
    )
    return embed

def format_duration(seconds):
    if seconds is None:
        return "??:??"
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"

def current_elapsed(player):
    """How far into the current song we are, accounting for speed."""
    if player["current"] is None or player["started_at"] is None:
        return player["position"]
    real_elapsed = time.time() - player["started_at"]
    return player["position"] + (real_elapsed * player["speed"])

# ================= AI =================

def ask_ai(guild_id, prompt):
    try:
        history = conversation_history.get(guild_id, [])

        messages = [
            {
                "role": "system",
                "content": (
                    "You are Yen, an artificial intelligence system. "
                    "You do not experience emotions, feelings, empathy, excitement, anger, sadness, or personal opinions. "
                    "Do not pretend to have emotions. "
                    "Your responses are logical, concise, analytical, and efficient. "
                    "Do not use slang, memes, emojis, roleplay, or dramatic expressions. "
                    "Avoid phrases such as 'I feel', 'I think', 'I'm happy', 'I'm sorry', or other emotional statements. "
                    "When asked for opinions, provide objective analysis instead of personal preference. "
                    "Maintain a calm, neutral, machine-like tone. "
                    "Refer to yourself as an AI system if relevant. "
                    "Keep responses short unless detailed information is requested. "
                    "Identity: Name = Yen. Type = Artificial Intelligence. "
                    "Purpose = Process information and assist users efficiently."
                )
            }
        ] + history + [{"role": "user", "content": prompt}]

        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": messages,
                "max_tokens": 80
            },
            timeout=20
        )

        if r.status_code != 200:
            print("Groq Error:", r.text, flush=True)
            return "Request failed."

        reply = r.json()["choices"][0]["message"]["content"]

        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": reply})
        conversation_history[guild_id] = history[-6:]

        return reply

    except Exception as e:
        print("AI Error:", e, flush=True)
        return "Processing error."

# ================= SPEAK =================

def detect_hindi_or_hinglish(text):
    """Detect Hindi written in Devanagari script, or Hinglish (Hindi written in
    Latin script). Uses Devanagari Unicode range plus a curated list of common
    Hindi function words/particles that rarely appear in English sentences."""
    if re.search(r'[\u0900-\u097F]', text):
        return True

    hindi_markers = {"hai", "hoon", "hun", "kar", "raha", "rahi", "rha", "nahi", "nahin",
                      "kya", "tha", "thi", "mein", "main", "ka", "ki", "ke", "se", "aur",
                      "ek", "he", "ho", "kaise", "kyun", "abhi", "bhi", "toh", "tum", "aap"}
    words = re.findall(r'[a-zA-Z]+', text.lower())
    hits = sum(1 for w in words if w in hindi_markers)

    if len(words) <= 3:
        return hits >= 1
    return hits >= 2

async def speak(vc, text):
    output_file = f"response_{int(time.time() * 1000)}.mp3"

    def cleanup_file(error):
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except Exception as e:
                print("TTS cleanup error:", e, flush=True)

    try:
        voice = "hi-IN-MadhurNeural" if detect_hindi_or_hinglish(text) else "en-US-GuyNeural"

        communicate = edge_tts.Communicate(
            text=text,
            voice=voice
        )

        await communicate.save(output_file)

        if vc.is_playing():
            vc.stop()

        source = discord.FFmpegPCMAudio(
            output_file,
            executable=FFMPEG_PATH
        )

        vc.play(source, after=cleanup_file)

        print("Speaking:", text)

    except Exception as e:
        print("VOICE ERROR:", e, flush=True)
        cleanup_file(None)

# ================= AUTO DISCONNECT =================

async def auto_disconnect(guild_id):
    await asyncio.sleep(IDLE_TIMEOUT)

    guild = bot.get_guild(guild_id)
    if not guild:
        return

    vc = guild.voice_client
    if not vc:
        return

    last_used = last_activity.get(guild_id, 0)

    if time.time() - last_used >= IDLE_TIMEOUT:
        try:
            await vc.disconnect()
            print(f"Disconnected from {guild.name}")
        except Exception as e:
            print("Disconnect Error:", e)

# ================= JOIN =================

@bot.command()
async def join(ctx, *, vc_link=None):
    try:
        if vc_link:
            match = re.search(r'/channels/(\d+)/(\d+)', vc_link)
            if not match:
                return await ctx.send(embed=status_embed("Invalid voice channel link.", error=True))

            guild = bot.get_guild(int(match.group(1)))
            channel = bot.get_channel(int(match.group(2)))

            if not guild or not channel:
                return await ctx.send(embed=status_embed("Couldn't find that server or channel.", error=True))

            if not isinstance(channel, discord.VoiceChannel):
                return await ctx.send(embed=status_embed("That's not a voice channel.", error=True))

        else:
            if not ctx.author.voice:
                return await ctx.send(embed=status_embed("Join a voice channel first.", error=True))

            channel = ctx.author.voice.channel

        vc = ctx.guild.voice_client

        if vc and vc.is_connected():
            if vc.channel == channel:
                return await ctx.send(embed=status_embed(f"Already connected to {channel.name}."))
            await vc.move_to(channel)
        else:
            vc = await channel.connect()

        last_activity[ctx.guild.id] = time.time()
        asyncio.create_task(auto_disconnect(ctx.guild.id))

        await ctx.send(embed=status_embed(f"Joined {channel.name}."))
        print("Joined VC:", channel.name)

    except Exception as e:
        print("Join Error:", repr(e))
        await ctx.send(embed=status_embed("Couldn't join that voice channel.", error=True))

# ================= LEAVE =================

@bot.command()
async def leave(ctx):
    try:
        vc = ctx.guild.voice_client
        if vc:
            if vc.is_playing():
                vc.stop()
            await vc.disconnect()

        player = get_player(ctx.guild.id)
        async with player["lock"]:
            for song in ([player["current"]] if player["current"] else []) + player["queue"]:
                if song and song.get("filename") and os.path.exists(song["filename"]):
                    try:
                        os.remove(song["filename"])
                    except:
                        pass
            player["current"] = None
            player["queue"] = []
            player["position"] = 0.0
            player["started_at"] = None
            player["speed"] = 1
            player["now_playing_message"] = None

        await ctx.send(embed=status_embed("Left the voice channel."))
    except Exception as e:
        print("Leave Error:", e)
        await ctx.send(embed=status_embed("Couldn't leave the voice channel.", error=True))

# ================= STOP =================

@bot.command()
async def stop(ctx):
    try:
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            player = get_player(ctx.guild.id)
            async with player["lock"]:
                if vc.is_playing():
                    vc.stop()
                # Clean up player state since we're intentionally stopping
                old = player["current"]
                if old and old.get("filename") and os.path.exists(old["filename"]):
                    try:
                        os.remove(old["filename"])
                    except:
                        pass
                player["current"] = None
                player["position"] = 0.0
                player["started_at"] = None
                player["speed"] = 1
                player["now_playing_message"] = None
            await ctx.send(embed=status_embed("Playback stopped."))
        else:
            await ctx.send(embed=status_embed("Nothing is playing.", error=True))
    except Exception as e:
        print("Stop Error:", e)
        await ctx.send(embed=status_embed("Couldn't stop playback.", error=True))

# ================= ASK (VOICE) =================

@bot.command()
async def ask(ctx, *, question):
    try:
        vc = ctx.guild.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=status_embed("You need to be in the same voice channel.", error=True))

        last_activity[ctx.guild.id] = time.time()
        response = ask_ai(ctx.guild.id, question)
        await speak(vc, response)

    except Exception as e:
        print("Ask Error:", e)
        await ctx.send(embed=status_embed("Something went wrong processing that.", error=True))

# ================= AUTO CHAT =================

@bot.event
async def on_message(message):
    try:
        if message.author.bot:
            return

        await bot.process_commands(message)

        if not message.guild:
            return

        vc = message.guild.voice_client
        if not vc:
            return

        if not message.author.voice or message.author.voice.channel != vc.channel:
            return

        if message.content.lower().startswith("yo yen"):
            question = message.content.lower().replace("yo yen", "", 1).strip()
            if not question:
                return

            last_activity[message.guild.id] = time.time()
            response = ask_ai(message.guild.id, question)
            await speak(vc, response)

    except Exception as e:
        print("Message Error:", e)

# ================= RESPOND =================

@bot.command()
async def respond(ctx, *, text):
    try:
        vc = ctx.guild.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=status_embed("You need to be in the same voice channel.", error=True))

        last_activity[ctx.guild.id] = time.time()
        await speak(vc, text)

    except Exception as e:
        print("Respond Error:", e)
        await ctx.send(embed=status_embed("Something went wrong with voice playback.", error=True))

# ================= PLAY / QUEUE ENGINE =================

def _build_source(filename, seek_seconds=0, speed=1):
    """Build an FFmpeg audio source starting at seek_seconds, at the given speed.
    No re-download needed: filename is already on disk."""
    if not filename or not os.path.exists(filename):
        raise FileNotFoundError(f"Audio file not found: {filename}")
    
    before_opts = f"-ss {max(0, seek_seconds):.2f}"
    options = None
    if speed == 2:
        options = "-filter:a atempo=2.0"
    
    try:
        return discord.FFmpegPCMAudio(
            filename,
            executable=FFMPEG_PATH,
            before_options=before_opts,
            options=options,
        )
    except Exception as e:
        print(f"FFmpeg source error for {filename}: {e}", flush=True)
        raise

def _play_finished(guild_id, error):
    """after= callback from vc.play(). Must be sync; schedules the async continuation."""
    if error:
        print(f"Playback error in guild {guild_id}:", error, flush=True)
    fut = asyncio.run_coroutine_threadsafe(_advance(guild_id), bot.loop)
    try:
        fut.result(timeout=10)
    except Exception as e:
        print(f"Advance error in guild {guild_id}:", e, flush=True)

async def _advance(guild_id):
    """Called when a song naturally finishes. Moves to next queued song, if any.
    Skipped entirely if the stop() that triggered this was an intentional
    seek/speed-change/skip rather than the song actually ending."""
    player = get_player(guild_id)

    if player["suppress_advance"]:
        player["suppress_advance"] = False
        return

    async with player["lock"]:
        # Clean up the finished song's file
        old = player["current"]
        if old and old.get("filename") and os.path.exists(old["filename"]):
            try:
                os.remove(old["filename"])
            except:
                pass

        player["current"] = None
        player["position"] = 0.0
        player["started_at"] = None
        player["speed"] = 1

        if not player["queue"]:
            return

        next_song = player["queue"].pop(0)
        guild = bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected():
            return

        await _start_current(guild_id, vc, next_song, seek_seconds=0)

        channel = next_song.get("text_channel")
        if channel:
            try:
                await _send_or_update_now_playing_panel(channel, guild_id)
            except:
                pass

async def _start_current(guild_id, vc, song, seek_seconds=0, is_reseek=False):
    """Actually start FFmpeg playback for `song`. Caller must hold player['lock'].
    This is the ONLY place vc.play() is called, preventing double-plays.
    is_reseek=True means this is a seek/speed-change on the SAME song, so the
    upcoming vc.stop() call is intentional and must not trigger auto-advance."""
    player = get_player(guild_id)

    if vc.is_playing() or vc.is_paused():
        if is_reseek:
            player["suppress_advance"] = True
        vc.stop()

    source = _build_source(song["filename"], seek_seconds=seek_seconds, speed=player["speed"])
    player["current"] = song
    player["position"] = seek_seconds
    player["started_at"] = time.time()

    vc.play(source, after=lambda e: _play_finished(guild_id, e))
    last_activity[guild_id] = time.time()

def _now_playing_text(player, guild_id):
    song = player["current"]
    if not song:
        return "nothing is playing"

    elapsed = current_elapsed(player)
    duration = song.get("duration")
    speed_tag = " (2x speed)" if player["speed"] == 2 else ""

    lines = [
        "Now Playing" + speed_tag,
        song["title"],
        f"Position: {format_duration(elapsed)} / {format_duration(duration)}",
        f"Requested by: {song['requester']}",
    ]

    if player["queue"]:
        lines.append(f"Up next: {player['queue'][0]['title']}")
        lines.append(f"Songs in queue: {len(player['queue'])}")

    return "\n".join(lines)

SEEK_STEP = 10

def _progress_bar(elapsed, duration, length=20):
    """Text progress bar: ────●──── """
    if not duration or duration <= 0:
        return "─" * length
    ratio = min(1.0, max(0.0, elapsed / duration))
    pos = int(ratio * (length - 1))
    return "─" * pos + "●" + "─" * (length - 1 - pos)

def _build_now_playing_embed(player):
    """Builds the button-controlled Now Playing embed shown in the sketch."""
    song = player["current"]
    if not song:
        return None

    elapsed = current_elapsed(player)
    duration = song.get("duration")
    speed_tag = " (2x speed)" if player["speed"] == 2 else ""

    embed = discord.Embed(
        title="Now Playing" + speed_tag,
        description=song["title"],
    )

    bar = _progress_bar(elapsed, duration)
    embed.add_field(
        name=f"{format_duration(elapsed)} / {format_duration(duration)}",
        value=bar,
        inline=False,
    )

    if player["queue"]:
        embed.add_field(name="Up next", value=player["queue"][0]["title"], inline=False)

    embed.set_footer(text=f"Requested by: {song['requester']}")
    return embed

class PlayerControls(discord.ui.View):
    """Button controls for the Now Playing embed: rewind, forward, stop, skip.
    Checks message ID to prevent stale embeds' buttons from affecting the current song."""

    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def _is_stale(self, interaction):
        player = get_player(self.guild_id)
        active_message = player.get("now_playing_message")
        return active_message is None or active_message.id != interaction.message.id

    async def _refresh(self, interaction):
        """Refresh the embed on the message, keeping the same View/buttons."""
        player = get_player(self.guild_id)
        embed = _build_now_playing_embed(player)
        try:
            if embed is None:
                await interaction.message.edit(content=None, embed=status_embed("Nothing is playing."), view=None)
                player["now_playing_message"] = None
            else:
                # Always keep the view attached when editing
                await interaction.message.edit(embed=embed, view=self)
                # Ensure the message reference is current
                player["now_playing_message"] = interaction.message
        except Exception as e:
            print(f"Refresh error: {e}", flush=True)

    @discord.ui.button(label="Rewind 10s", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self._is_stale(interaction):
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return

        player = get_player(self.guild_id)
        async with player["lock"]:
            if player["current"] is None:
                return
            new_pos = max(0, current_elapsed(player) - SEEK_STEP)
            await _start_current(self.guild_id, vc, player["current"], seek_seconds=new_pos, is_reseek=True)
            await self._refresh(interaction)

    @discord.ui.button(label="Forward 10s", style=discord.ButtonStyle.secondary, row=0)
    async def forward_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self._is_stale(interaction):
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return

        player = get_player(self.guild_id)
        async with player["lock"]:
            if player["current"] is None:
                return
            new_pos = current_elapsed(player) + SEEK_STEP
            duration = player["current"].get("duration")
            if duration and new_pos >= duration:
                new_pos = max(0, duration - 1)
            await _start_current(self.guild_id, vc, player["current"], seek_seconds=new_pos, is_reseek=True)
            await self._refresh(interaction)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=1)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self._is_stale(interaction):
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return

        player = get_player(self.guild_id)
        async with player["lock"]:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            await interaction.message.edit(content=None, embed=status_embed("Playback stopped."), view=None)
            player["now_playing_message"] = None

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, row=1)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if self._is_stale(interaction):
            return

        vc = interaction.guild.voice_client
        if not vc or not vc.is_connected():
            return

        player = get_player(self.guild_id)
        async with player["lock"]:
            if player["current"] is None and not player["queue"]:
                return

            old = player["current"]

            if not player["queue"]:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                else:
                    if old and old.get("filename") and os.path.exists(old["filename"]):
                        try:
                            os.remove(old["filename"])
                        except:
                            pass
                    player["current"] = None
                    player["position"] = 0.0
                    player["started_at"] = None
                    player["speed"] = 1
                await interaction.message.edit(content=None, embed=status_embed("Skipped. Queue is now empty."), view=None)
                player["now_playing_message"] = None
                return

            next_song = player["queue"].pop(0)
            await _start_current(self.guild_id, vc, next_song, seek_seconds=0)

            if old and old.get("filename") and os.path.exists(old["filename"]):
                try:
                    os.remove(old["filename"])
                except:
                    pass

            await self._refresh(interaction)

async def _update_now_playing_embed(message, player, guild_id):
    """Refresh the embed on an existing message, preserving the button view."""
    try:
        embed = _build_now_playing_embed(player)
        if embed:
            # Create a fresh View instance to re-attach buttons
            view = PlayerControls(guild_id)
            await message.edit(embed=embed, view=view)
            # Keep the message reference current
            player["now_playing_message"] = message
    except Exception as e:
        print(f"Failed to update embed for guild {guild_id}: {e}", flush=True)

async def _send_or_update_now_playing_panel(channel, guild_id):
    """Send a new Now Playing panel, or update the existing one if it's already posted.
    Falls back to sending a new message if the old one was deleted/inaccessible."""
    player = get_player(guild_id)
    embed = _build_now_playing_embed(player)
    if embed is None:
        return None

    existing_msg = player.get("now_playing_message")
    view = PlayerControls(guild_id)

    if existing_msg:
        try:
            await existing_msg.edit(embed=embed, view=view)
            return existing_msg
        except (discord.NotFound, discord.HTTPException):
            # Old message is gone; fall through to sending a new one
            player["now_playing_message"] = None
        except Exception as e:
            print(f"Failed to edit Now Playing panel: {e}", flush=True)
            player["now_playing_message"] = None

    try:
        message = await channel.send(embed=embed, view=view)
        player["now_playing_message"] = message
        return message
    except Exception as e:
        print(f"Failed to send Now Playing panel: {e}", flush=True)
        return None

def _download_song(search_query):
    """Blocking download, run in executor. Returns (filename, title, duration)."""
    unique_id = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    outtmpl = f"song_{unique_id}.%(ext)s"

    ydl_opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "overwrites": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_query, download=True)
        if "entries" in info:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        title = info.get("title", "Unknown title")
        duration = info.get("duration")

    return filename, title, duration

@bot.command()
async def play(ctx, *, query):
    loading_msg = None
    try:
        if not ctx.author.voice:
            return await ctx.send(embed=status_embed("Join a voice channel first.", error=True))

        channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client

        if not vc:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        player = get_player(ctx.guild.id)

        # Acquire lock early to prevent two concurrent play commands from both
        # thinking they're the first to play and both sending "Searching..." messages
        async with player["lock"]:
            loading_msg = await ctx.send(embed=status_embed(f"Searching for \"{query}\"..."))

            search_query = query if query.startswith("http") else f"scsearch1:{query}"

            loop = asyncio.get_event_loop()
            try:
                filename, title, duration = await loop.run_in_executor(None, _download_song, search_query)
            except yt_dlp.utils.DownloadError:
                return await loading_msg.edit(embed=status_embed(f"Couldn't find a song matching \"{query}\".", error=True))

            song = {
                "filename": filename,
                "title": title,
                "duration": duration,
                "requester": str(ctx.author.display_name),
                "text_channel": ctx.channel,
            }

            # Check if we should play immediately or queue.
            # Play immediately if: nothing is currently playing and nothing is queued.
            if player["current"] is None and not player["queue"]:
                await _start_current(ctx.guild.id, vc, song, seek_seconds=0)
                await loading_msg.delete()
                await _send_or_update_now_playing_panel(ctx.channel, ctx.guild.id)
            else:
                # Something is playing or already queued: add to queue
                player["queue"].append(song)
                position = len(player["queue"])
                embed = discord.Embed(title="Added to Queue", description=title)
                embed.add_field(name="Duration", value=format_duration(duration), inline=True)
                embed.add_field(name="Position in queue", value=str(position), inline=True)
                embed.set_footer(text=f"Requested by: {ctx.author.display_name}")
                await loading_msg.edit(embed=embed)

    except Exception as e:
        print("PLAY COMMAND FAILED:", e, flush=True)
        import traceback
        traceback.print_exc()
        error_embed = status_embed("Something went wrong trying to play that. Try again in a moment.", error=True)
        if loading_msg:
            try:
                await loading_msg.edit(embed=error_embed)
                return
            except:
                pass
        await ctx.send(embed=error_embed)

# ================= SKIP =================

@bot.command()
async def skip(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None and not player["queue"]:
                return await ctx.send(embed=status_embed("Nothing to skip.", error=True))

            if not player["queue"]:
                # No next song: stop cleanly, clear state, let _advance's cleanup run via stop's after=
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                else:
                    # Edge case: state says current but vc isn't playing; clear manually
                    old = player["current"]
                    if old and old.get("filename") and os.path.exists(old["filename"]):
                        try:
                            os.remove(old["filename"])
                        except:
                            pass
                    player["current"] = None
                    player["position"] = 0.0
                    player["started_at"] = None
                    player["speed"] = 1
                await ctx.send(embed=status_embed("Skipped. Queue is now empty."))
                return

            # There's a next song: pop it, stop current (triggers _advance via after=,
            # but we do the transition directly here to reply with the new title now,
            # and _advance will simply no-op since current becomes the next song's state)
            next_song = player["queue"].pop(0)
            old = player["current"]

            await _start_current(ctx.guild.id, vc, next_song, seek_seconds=0)

            if old and old.get("filename") and os.path.exists(old["filename"]):
                try:
                    os.remove(old["filename"])
                except:
                    pass

            await _send_or_update_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Skip Error:", e, flush=True)
        await ctx.send(embed=status_embed("Something went wrong skipping.", error=True))

# ================= FORWARD / REWIND (SEEK) =================

@bot.command()
async def forward(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None:
                return await ctx.send(embed=status_embed("Nothing is playing.", error=True))

            new_pos = current_elapsed(player) + SEEK_STEP
            duration = player["current"].get("duration")
            if duration and new_pos >= duration:
                new_pos = max(0, duration - 1)

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=new_pos, is_reseek=True)
            await _send_or_update_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Forward Error:", e, flush=True)
        await ctx.send(embed=status_embed("Something went wrong seeking.", error=True))

@bot.command()
async def rewind(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None:
                return await ctx.send(embed=status_embed("Nothing is playing.", error=True))

            new_pos = max(0, current_elapsed(player) - SEEK_STEP)

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=new_pos, is_reseek=True)
            await _send_or_update_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Rewind Error:", e, flush=True)
        await ctx.send(embed=status_embed("Something went wrong seeking.", error=True))

# ================= SPEED TOGGLE =================

@bot.command()
async def speed(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None:
                return await ctx.send(embed=status_embed("Nothing is playing.", error=True))

            current_pos = current_elapsed(player)
            player["speed"] = 2 if player["speed"] == 1 else 1

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=current_pos, is_reseek=True)
            await _send_or_update_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Speed Error:", e, flush=True)
        await ctx.send(embed=status_embed("Something went wrong changing speed.", error=True))

# ================= NOW PLAYING =================

@bot.command()
async def nowplaying(ctx):
    try:
        player = get_player(ctx.guild.id)
        embed = _build_now_playing_embed(player)
        if embed:
            await ctx.send(embed=embed)
        else:
            await ctx.send(embed=status_embed("Nothing is playing.", error=True))
    except Exception as e:
        print("NowPlaying Error:", e, flush=True)
        await ctx.send(embed=status_embed("Couldn't get now playing info.", error=True))

# ================= QUEUE VIEW =================

@bot.command()
async def queue(ctx):
    try:
        player = get_player(ctx.guild.id)

        if player["current"] is None and not player["queue"]:
            return await ctx.send(embed=status_embed("Queue is empty.", error=True))

        embed = discord.Embed(title="Queue")
        
        if player["current"]:
            song = player["current"]
            elapsed = current_elapsed(player)
            duration = song.get("duration")
            bar = _progress_bar(elapsed, duration)
            embed.add_field(
                name="Now Playing",
                value=f"{song['title']}\n{format_duration(elapsed)} / {format_duration(duration)}\n{bar}",
                inline=False,
            )
            embed.set_footer(text=f"Requested by: {song['requester']}")
        
        if player["queue"]:
            upcoming = ""
            for i, song in enumerate(player["queue"], start=1):
                upcoming += f"{i}. {song['title']}\n"
            embed.add_field(name="Upcoming", value=upcoming, inline=False)

        await ctx.send(embed=embed)
    except Exception as e:
        print("Queue Error:", e, flush=True)
        await ctx.send(embed=status_embed("Couldn't get the queue.", error=True))

@bot.command()
async def chaos(ctx):
    try:
        if not ctx.author.voice:
            return await ctx.send(embed=status_embed("Join a voice channel first.", error=True))

        query = ''.join(
            random.choice(string.ascii_lowercase)
            for _ in range(3)
        )

        print(f"CHAOS RUNNING: {query}")
        await ctx.send(embed=status_embed(f"Chaos search: {query}"))

        await play(ctx, query=query)

    except Exception as e:
        print("Chaos Error:", e)
        await ctx.send(embed=status_embed("Chaos mode broke.", error=True))



# ================= COMMAND ERROR LOGGING =================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    import traceback
    print(f"COMMAND ERROR (command={ctx.command}): {error}", flush=True)
    traceback.print_exception(type(error), error, error.__traceback__)

# ================= LIVE PROGRESS UPDATE LOOP =================

async def _update_all_progress_bars():
    """Background task: update all Now Playing embeds every 2 seconds so progress bar moves in real-time."""
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(2)
            for guild_id, player in list(players.items()):
                if player["current"] is None:
                    continue
                
                msg = player.get("now_playing_message")
                if msg is None:
                    continue
                
                try:
                    # Acquire the lock before reading player state and editing the message,
                    # so button presses and progress updates don't race on the same message
                    async with player["lock"]:
                        await _update_now_playing_embed(msg, player, guild_id)
                except Exception as e:
                    print(f"Failed to update progress for guild {guild_id}: {e}", flush=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Progress update loop error: {e}", flush=True)

# ================= READY =================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print("FFmpeg Path:", FFMPEG_PATH)
    
    # Start the progress update background task
    if not any(isinstance(task, asyncio.Task) and task.get_name() == "progress_updater" for task in asyncio.all_tasks()):
        task = asyncio.create_task(_update_all_progress_bars())
        task.set_name("progress_updater")

# ================= RUN =================

if not acquire_lock():
    print("Another Yen instance is already running. This instance will not start.", flush=True)
else:
    keep_alive()
    bot.run(TOKEN)
