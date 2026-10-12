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
forced_language = {}  # guild_id -> "hindi" | "english" | not set (auto-detect)

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
            "volume": 1.0,
            "source": None,
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
    if get_player(vc.guild.id)["current"] is not None:
        print("TTS skipped: music is playing", flush=True)
        return
    output_file = f"response_{int(time.time() * 1000)}.mp3"

    def cleanup_file(error):
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except Exception as e:
                print("TTS cleanup error:", e, flush=True)

    try:
        guild_id = vc.guild.id if vc.guild else None
        override = forced_language.get(guild_id)

        if override == "hindi":
            voice = "hi-IN-MadhurNeural"
        elif override == "english":
            voice = "en-US-GuyNeural"
        else:
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

# ================= LEAVE / STOP / VOLUME =================

@bot.command()
async def leave(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc:
            return await ctx.send(embed=status_embed("I'm not in a voice channel.", error=True))
        player = get_player(ctx.guild.id)
        async with player["lock"]:
            await _halt_playback(ctx.guild.id, vc)
            await _close_panel(player, "Disconnected.")
        await vc.disconnect()
        await ctx.send(embed=status_embed("Left the voice channel."))
    except Exception as e:
        print("Leave Error:", e, flush=True)
        await ctx.send(embed=status_embed("Couldn't leave the voice channel.", error=True))

@bot.command()
async def stop(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("I'm not in a voice channel.", error=True))
        player = get_player(ctx.guild.id)
        if player["current"] is None and not player["queue"]:
            return await ctx.send(embed=status_embed("Nothing is playing.", error=True))
        async with player["lock"]:
            await _halt_playback(ctx.guild.id, vc)
            await _close_panel(player, "Stopped.")
        await ctx.send(embed=status_embed("Playback stopped and queue cleared."))
    except Exception as e:
        print("Stop Error:", e, flush=True)
        await ctx.send(embed=status_embed("Couldn't stop playback.", error=True))

@bot.command()
async def volume(ctx, level: str = None):
    try:
        player = get_player(ctx.guild.id)
        if level is None:
            return await ctx.send(embed=status_embed(f"Volume is {round(player['volume'] * 100)}%."))
        try:
            percent = int(level.rstrip("%"))
        except ValueError:
            percent = -1
        if not 0 <= percent <= MAX_VOLUME:
            return await ctx.send(embed=status_embed(f"Volume must be a number from 0 to {MAX_VOLUME}.", error=True))
        async with player["lock"]:
            player["volume"] = percent / 100
            if player["source"] is not None:
                player["source"].volume = percent / 100
        await ctx.send(embed=status_embed(f"Volume set to {percent}%."))
    except Exception as e:
        print("Volume Error:", e, flush=True)
        await ctx.send(embed=status_embed("Couldn't change the volume.", error=True))

# ================= ASK (VOICE) =================

@bot.command()
async def ask(ctx, *, question):
    try:
        vc = ctx.guild.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send(embed=status_embed("Not connected to a voice channel.", error=True))

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            return await ctx.send(embed=status_embed("You need to be in the same voice channel.", error=True))

        if get_player(ctx.guild.id)["current"] is not None:
            return await ctx.send(embed=status_embed("Music is playing. Stop it before using voice.", error=True))
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

        content = message.content.strip()
        if not content:
            return

        # Don't speak bot commands (e.g. "yen play tek") since they're not chat messages
        if content.lower().startswith(bot.command_prefix):
            return

        if content.lower().startswith("yo yen"):
            question = content.lower().replace("yo yen", "", 1).strip()
            if not question:
                return

            last_activity[message.guild.id] = time.time()
            response = ask_ai(message.guild.id, question)
            await speak(vc, response)
            return

        # Speak everything else typed by someone in the bot's voice channel,
        # but skip if music is currently playing (don't interrupt the song)
        player = get_player(message.guild.id)
        if player["current"] is not None:
            return

        last_activity[message.guild.id] = time.time()
        await speak(vc, content)

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

        if get_player(ctx.guild.id)["current"] is not None:
            return await ctx.send(embed=status_embed("Music is playing. Stop it before using voice.", error=True))
        last_activity[ctx.guild.id] = time.time()
        await speak(vc, text)

    except Exception as e:
        print("Respond Error:", e)
        await ctx.send(embed=status_embed("Something went wrong with voice playback.", error=True))

# ================= LANGUAGE TOGGLE =================

@bot.command()
async def hindi(ctx):
    try:
        forced_language[ctx.guild.id] = "hindi"
        await ctx.send(embed=status_embed("Yen will now speak everything in Hindi."))
    except Exception as e:
        print("Hindi Command Error:", e)
        await ctx.send(embed=status_embed("Couldn't switch language.", error=True))

@bot.command()
async def english(ctx):
    try:
        forced_language[ctx.guild.id] = "english"
        await ctx.send(embed=status_embed("Yen will now speak everything in English."))
    except Exception as e:
        print("English Command Error:", e)
        await ctx.send(embed=status_embed("Couldn't switch language.", error=True))

# ================= PLAY / QUEUE ENGINE =================

SEEK_STEP = 10
MAX_VOLUME = 200

def _build_source(filename, seek_seconds=0, speed=1):
    """FFmpeg source starting at seek_seconds, optionally at 2x speed.
    The file is already on disk, so seeking never re-downloads anything."""
    if not filename or not os.path.exists(filename):
        raise FileNotFoundError(f"Audio file not found: {filename}")
    # Filter order matters: speed change first, then loudness is measured on the final audio.
    filters = ["aresample=48000"]
    if speed == 2:
        filters.append("atempo=2.0")
    filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    options = "-filter:a " + ",".join(filters)
    return discord.FFmpegPCMAudio(
        filename,
        executable=FFMPEG_PATH,
        before_options=f"-ss {max(0, seek_seconds):.2f}",
        options=options,
    )

def _delete_file(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            print("File cleanup error:", e, flush=True)

def _log_advance_failure(guild_id, future):
    if future.cancelled():
        return
    exc = future.exception()
    if exc:
        print(f"Advance error in guild {guild_id}:", exc, flush=True)

def _play_finished(guild_id, error):
    """after= callback, run on discord.py's audio thread. Schedules _advance on
    the event loop and returns immediately instead of blocking that thread."""
    if error:
        print(f"Playback error in guild {guild_id}:", error, flush=True)
    future = asyncio.run_coroutine_threadsafe(_advance(guild_id), bot.loop)
    future.add_done_callback(lambda f: _log_advance_failure(guild_id, f))

async def _advance(guild_id):
    """Runs when a song ends on its own. Ignored when the stop came from our own
    code (skip, stop, seek, speed), because that code already chose what plays next."""
    player = get_player(guild_id)
    async with player["lock"]:
        if player["suppress_advance"]:
            player["suppress_advance"] = False
            return

        finished = player["current"]
        player["current"] = None
        player["started_at"] = None
        player["position"] = 0.0
        player["speed"] = 1
        player["source"] = None
        _delete_file(finished["filename"] if finished else None)

        if not player["queue"]:
            await _close_panel(player, "Queue finished.")
            return

        guild = bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected():
            return

        next_song = player["queue"].pop(0)
        await _start_current(guild_id, vc, next_song)
        channel = next_song.get("text_channel")
        if channel:
            await _send_or_update_now_playing_panel(channel, guild_id)

async def _start_current(guild_id, vc, song, seek_seconds=0, suppress_auto_advance=False):
    """The only place vc.play() is called for music. Caller must hold player['lock'].
    suppress_auto_advance=True tells _advance to ignore the stop this call triggers,
    because the caller has already decided what plays next."""
    player = get_player(guild_id)
    if vc.is_playing() or vc.is_paused():
        if suppress_auto_advance and player["current"] is not None:
            player["suppress_advance"] = True
        vc.stop()

    raw = _build_source(song["filename"], seek_seconds=seek_seconds, speed=player["speed"])
    source = discord.PCMVolumeTransformer(raw, volume=player["volume"])
    player["source"] = source
    player["current"] = song
    player["position"] = seek_seconds
    player["started_at"] = time.time()
    vc.play(source, after=lambda e: _play_finished(guild_id, e))
    last_activity[guild_id] = time.time()

async def _halt_playback(guild_id, vc):
    """Stop audio and wipe the queue. Caller must hold player['lock']."""
    player = get_player(guild_id)
    if player["current"] is not None and (vc.is_playing() or vc.is_paused()):
        player["suppress_advance"] = True
        vc.stop()
    if player["current"]:
        _delete_file(player["current"]["filename"])
    for song in player["queue"]:
        _delete_file(song.get("filename"))
    player["current"] = None
    player["queue"] = []
    player["position"] = 0.0
    player["started_at"] = None
    player["speed"] = 1
    player["source"] = None

async def _close_panel(player, text):
    """Replace the Now Playing embed with a plain status embed and forget it."""
    message = player.get("now_playing_message")
    player["now_playing_message"] = None
    if message:
        try:
            await message.edit(embed=status_embed(text))
        except discord.HTTPException:
            pass

def _progress_bar(elapsed, duration, length=20):
    """Block-character progress bar with partial fills for finer resolution."""
    if not duration or duration <= 0:
        return "▬" * length
    ratio = min(1.0, max(0.0, elapsed / duration))
    filled = ratio * length
    full_blocks = int(filled)
    remainder = filled - full_blocks

    bar = "█" * full_blocks
    partials = " ▏▎▍▌▋▊▉"
    if full_blocks < length:
        partial_index = int(remainder * (len(partials) - 1))
        bar += partials[partial_index] if partial_index > 0 else "▬"
        bar += "▬" * (length - full_blocks - 1)
    return bar

def _format_count(n):
    if n is None:
        return None
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

def _build_now_playing_embed(player):
    """Now Playing embed: artwork top-right, clickable title, progress bar,
    stats, up-next preview, and a volume readout in the footer."""
    song = player["current"]
    if not song:
        return None

    elapsed = current_elapsed(player)
    duration = song.get("duration")
    ratio = (elapsed / duration) if duration else 0
    percent = int(min(100, max(0, ratio * 100)))

    if player["speed"] == 2:
        color = discord.Color.orange()
    elif ratio > 0.85:
        color = discord.Color.purple()
    else:
        color = discord.Color.from_rgb(30, 215, 96)

    speed_tag = " (2x)" if player["speed"] == 2 else ""
    title_line = f"[{song['title']}]({song['webpage_url']})" if song.get("webpage_url") else song["title"]

    embed = discord.Embed(
        title=f"Now Playing{speed_tag}",
        description=f"**{title_line}**",
        color=color,
    )

    bar = _progress_bar(elapsed, duration)
    embed.add_field(
        name="\u200b",
        value=f"`{format_duration(elapsed)}` {bar} `{format_duration(duration)}`  •  **{percent}%**",
        inline=False,
    )

    stats = []
    if song.get("uploader"):
        stats.append(f"**Uploader**\n{song['uploader']}")
    views = _format_count(song.get("view_count"))
    if views:
        stats.append(f"**Views**\n{views}")
    likes = _format_count(song.get("like_count"))
    if likes:
        stats.append(f"**Likes**\n{likes}")
    for stat in stats:
        embed.add_field(name="\u200b", value=stat, inline=True)

    if player["queue"]:
        upcoming = player["queue"][0]
        total_queue_duration = sum(s.get("duration") or 0 for s in player["queue"])
        queue_line = (
            f"**{upcoming['title']}**\n"
            f"{len(player['queue'])} song(s) queued • {format_duration(total_queue_duration)} total"
        )
        embed.add_field(name="Up Next", value=queue_line, inline=False)

    if song.get("thumbnail"):
        embed.set_thumbnail(url=song["thumbnail"])

    volume_pct = round(player["volume"] * 100)
    embed.set_footer(
        text=f"Requested by {song['requester']}  •  Volume {volume_pct}%",
        icon_url=song.get("requester_avatar"),
    )
    return embed

async def _update_now_playing_embed(message, player):
    embed = _build_now_playing_embed(player)
    if embed:
        await message.edit(embed=embed)

async def _send_or_update_now_playing_panel(channel, guild_id):
    """Edit the active Now Playing message if there is one, otherwise post a new one."""
    player = get_player(guild_id)
    embed = _build_now_playing_embed(player)
    if embed is None:
        return None

    existing = player.get("now_playing_message")
    if existing:
        try:
            await existing.edit(embed=embed)
            return existing
        except discord.HTTPException:
            player["now_playing_message"] = None

    try:
        message = await channel.send(embed=embed)
        player["now_playing_message"] = message
        return message
    except discord.HTTPException as e:
        print("Failed to send Now Playing panel:", e, flush=True)
        return None

def _download_song(search_query):
    """Blocking download, run in executor. Returns a metadata dict plus filename."""
    unique_id = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    outtmpl = f"song_{unique_id}.%(ext)s"

    ydl_opts = {
        "format": "bestaudio/best",
        "format_sort": ["abr", "asr"],
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

    return {
        "filename": filename,
        "title": info.get("title", "Unknown title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader") or info.get("channel"),
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "webpage_url": info.get("webpage_url"),
    }

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
                meta = await loop.run_in_executor(None, _download_song, search_query)
            except yt_dlp.utils.DownloadError:
                return await loading_msg.edit(embed=status_embed(f"Couldn't find a song matching \"{query}\".", error=True))

            song = {
                "filename": meta["filename"],
                "title": meta["title"],
                "duration": meta["duration"],
                "thumbnail": meta["thumbnail"],
                "uploader": meta["uploader"],
                "view_count": meta["view_count"],
                "like_count": meta["like_count"],
                "webpage_url": meta["webpage_url"],
                "requester": str(ctx.author.display_name),
                "requester_avatar": ctx.author.display_avatar.url if ctx.author.display_avatar else None,
                "text_channel": ctx.channel,
                "added_at": time.time(),
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

                # Estimate time until this song plays: remaining time on current song
                # plus full duration of every song ahead of it in queue
                eta = 0
                if player["current"]:
                    remaining = (player["current"].get("duration") or 0) - current_elapsed(player)
                    eta += max(0, remaining)
                for queued_song in player["queue"][:position - 1]:
                    eta += queued_song.get("duration") or 0

                embed = discord.Embed(
                    title="Added to Queue",
                    description=f"[{song['title']}]({song['webpage_url']})" if song.get("webpage_url") else song["title"],
                    color=discord.Color.blurple(),
                )
                if song.get("thumbnail"):
                    embed.set_thumbnail(url=song["thumbnail"])
                embed.add_field(name="Duration", value=format_duration(song["duration"]), inline=True)
                embed.add_field(name="Position", value=f"#{position}", inline=True)
                embed.add_field(name="Est. time until played", value=format_duration(eta), inline=True)
                if song.get("uploader"):
                    embed.add_field(name="Uploader", value=song["uploader"], inline=True)
                embed.set_footer(
                    text=f"Requested by {ctx.author.display_name}",
                    icon_url=song.get("requester_avatar"),
                )
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
            return await ctx.send(embed=status_embed("I'm not in a voice channel.", error=True))
        player = get_player(ctx.guild.id)
        async with player["lock"]:
            if player["current"] is None and not player["queue"]:
                return await ctx.send(embed=status_embed("Nothing to skip.", error=True))
            if not player["queue"]:
                await _halt_playback(ctx.guild.id, vc)
                await _close_panel(player, "Skipped.")
                return await ctx.send(embed=status_embed("Skipped. Queue is now empty."))

            finished = player["current"]
            next_song = player["queue"].pop(0)
            await _start_current(ctx.guild.id, vc, next_song, suppress_auto_advance=True)
            _delete_file(finished["filename"] if finished else None)
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

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=new_pos, suppress_auto_advance=True)
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

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=new_pos, suppress_auto_advance=True)
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

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=current_pos, suppress_auto_advance=True)
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

        embed = discord.Embed(title="Queue", color=discord.Color.blurple())

        if player["current"]:
            song = player["current"]
            elapsed = current_elapsed(player)
            duration = song.get("duration")
            bar = _progress_bar(elapsed, duration, length=15)
            embed.add_field(
                name="Now Playing",
                value=f"**{song['title']}**\n`{format_duration(elapsed)}` {bar} `{format_duration(duration)}`",
                inline=False,
            )
            if song.get("thumbnail"):
                embed.set_thumbnail(url=song["thumbnail"])
            embed.set_footer(
                text=f"Requested by {song['requester']}",
                icon_url=song.get("requester_avatar"),
            )

        if player["queue"]:
            upcoming_lines = []
            total_duration = 0
            for i, song in enumerate(player["queue"], start=1):
                dur = song.get("duration") or 0
                total_duration += dur
                upcoming_lines.append(f"`{i}.` **{song['title']}** — `{format_duration(dur)}` — *{song['requester']}*")

            embed.add_field(
                name=f"Up Next ({len(player['queue'])})",
                value="\n".join(upcoming_lines),
                inline=False,
            )
            embed.add_field(name="Total queue runtime", value=format_duration(total_duration), inline=True)

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
    """Refresh every active Now Playing embed on a fixed interval so the progress
    bar moves. Four seconds is smooth enough and stays clear of Discord rate limits."""
    await bot.wait_until_ready()
    while True:
        try:
            await asyncio.sleep(4)
            for guild_id, player in list(players.items()):
                message = player.get("now_playing_message")
                if player["current"] is None or message is None:
                    continue
                try:
                    async with player["lock"]:
                        await _update_now_playing_embed(message, player)
                except discord.NotFound:
                    player["now_playing_message"] = None
                except Exception as e:
                    print(f"Progress update error for guild {guild_id}: {e}", flush=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print("Progress update loop error:", e, flush=True)

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
