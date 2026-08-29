import discord
from discord.ext import commands
import requests
import asyncio
import edge_tts
import os
import imageio_ffmpeg
import re
import time
import yt_dlp
import random
import string

from flask import Flask, request, jsonify
from threading import Thread

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
        }
    return players[guild_id]

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

async def speak(vc, text):
    output_file = f"response_{int(time.time() * 1000)}.mp3"

    def cleanup_file(error):
        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except Exception as e:
                print("TTS cleanup error:", e, flush=True)

    try:
        communicate = edge_tts.Communicate(
            text=text,
            voice="en-US-GuyNeural"
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
                return await ctx.send("invalid vc link")

            guild = bot.get_guild(int(match.group(1)))
            channel = bot.get_channel(int(match.group(2)))

            if not guild or not channel:
                return await ctx.send("not found")

            if not isinstance(channel, discord.VoiceChannel):
                return await ctx.send("not a vc")

        else:
            if not ctx.author.voice:
                return await ctx.send("join vc first")

            channel = ctx.author.voice.channel

        vc = ctx.guild.voice_client

        if vc and vc.is_connected():
            if vc.channel == channel:
                return await ctx.send("already there")
            await vc.move_to(channel)
        else:
            vc = await channel.connect()

        last_activity[ctx.guild.id] = time.time()
        asyncio.create_task(auto_disconnect(ctx.guild.id))

        await ctx.send(f"joined {channel.name}")
        print("Joined VC:", channel.name)

    except Exception as e:
        print("Join Error:", repr(e))
        await ctx.send("couldn't join vc")

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

        await ctx.send("bye")
    except Exception as e:
        print("Leave Error:", e)

# ================= STOP =================

@bot.command()
async def stop(ctx):
    try:
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await ctx.send("stopped")
    except Exception as e:
        print("Stop Error:", e)

# ================= ASK (VOICE) =================

@bot.command()
async def ask(ctx, *, question):
    try:
        vc = ctx.guild.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send("im not in vc")

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            return await ctx.send("you gotta be in my vc")

        last_activity[ctx.guild.id] = time.time()
        response = ask_ai(ctx.guild.id, question)
        await speak(vc, response)

    except Exception as e:
        print("Ask Error:", e)
        await ctx.send("voice broke")

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
            return await ctx.send("im not in vc")

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            return await ctx.send("you gotta be in my vc")

        last_activity[ctx.guild.id] = time.time()
        await speak(vc, text)

    except Exception as e:
        print("Respond Error:", e)
        await ctx.send("voice broke")

# ================= PLAY / QUEUE ENGINE =================

def _build_source(filename, seek_seconds=0, speed=1):
    """Build an FFmpeg audio source starting at seek_seconds, at the given speed.
    No re-download needed: filename is already on disk."""
    before_opts = f"-ss {max(0, seek_seconds):.2f}"
    options = None
    if speed == 2:
        # atempo doubles playback speed without changing pitch drastically for x2
        options = "-filter:a atempo=2.0"
    return discord.FFmpegPCMAudio(
        filename,
        executable=FFMPEG_PATH,
        before_options=before_opts,
        options=options,
    )

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
    """Called when a song naturally finishes. Moves to next queued song, if any."""
    player = get_player(guild_id)
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
                await _send_now_playing_panel(channel, guild_id)
            except:
                pass

async def _start_current(guild_id, vc, song, seek_seconds=0):
    """Actually start FFmpeg playback for `song`. Caller must hold player['lock'].
    This is the ONLY place vc.play() is called, preventing double-plays."""
    player = get_player(guild_id)

    if vc.is_playing() or vc.is_paused():
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
    Buttons check they belong to the guild's current song message before acting,
    so an old/stale embed's buttons can't affect a different, already-changed song."""

    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    def _is_stale(self, interaction):
        player = get_player(self.guild_id)
        active_message = player.get("now_playing_message")
        return active_message is None or active_message.id != interaction.message.id

    async def _refresh(self, interaction, player):
        embed = _build_now_playing_embed(player)
        if embed is None:
            await interaction.message.edit(content="nothing is playing", embed=None, view=None)
            player["now_playing_message"] = None
        else:
            await interaction.message.edit(embed=embed, view=self)

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
            await _start_current(self.guild_id, vc, player["current"], seek_seconds=new_pos)
            await self._refresh(interaction, player)

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
            await _start_current(self.guild_id, vc, player["current"], seek_seconds=new_pos)
            await self._refresh(interaction, player)

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
            await interaction.message.edit(content="stopped", embed=None, view=None)
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
                await interaction.message.edit(content="skipped. queue is empty", embed=None, view=None)
                player["now_playing_message"] = None
                return

            next_song = player["queue"].pop(0)
            await _start_current(self.guild_id, vc, next_song, seek_seconds=0)

            if old and old.get("filename") and os.path.exists(old["filename"]):
                try:
                    os.remove(old["filename"])
                except:
                    pass

            await self._refresh(interaction, player)

async def _send_now_playing_panel(channel, guild_id):
    """Sends a fresh Now Playing embed with button controls, and tracks it
    as the guild's active panel so old panels' buttons become inert."""
    player = get_player(guild_id)
    embed = _build_now_playing_embed(player)
    if embed is None:
        return None

    view = PlayerControls(guild_id)
    message = await channel.send(embed=embed, view=view)
    player["now_playing_message"] = message
    return message

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
    try:
        if not ctx.author.voice:
            return await ctx.send("join vc first")

        channel = ctx.author.voice.channel
        vc = ctx.guild.voice_client

        if not vc:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        player = get_player(ctx.guild.id)

        await ctx.send("loading song...")

        search_query = query if query.startswith("http") else f"scsearch1:{query}"

        loop = asyncio.get_event_loop()
        filename, title, duration = await loop.run_in_executor(None, _download_song, search_query)

        song = {
            "filename": filename,
            "title": title,
            "duration": duration,
            "requester": str(ctx.author.display_name),
            "text_channel": ctx.channel,
        }

        async with player["lock"]:
            if player["current"] is None and not vc.is_playing():
                await _start_current(ctx.guild.id, vc, song, seek_seconds=0)
                await _send_now_playing_panel(ctx.channel, ctx.guild.id)
            else:
                player["queue"].append(song)
                position = len(player["queue"])
                lines = [
                    "Added to queue",
                    title,
                    f"Duration: {format_duration(duration)}",
                    f"Position in queue: {position}",
                    f"Requested by: {ctx.author.display_name}",
                ]
                await ctx.send("\n".join(lines))

    except Exception:
        import traceback
        print("PLAY COMMAND FAILED", flush=True)
        traceback.print_exc()
        await ctx.send("song broke")

# ================= SKIP =================

@bot.command()
async def skip(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("im not in vc")

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None and not player["queue"]:
                return await ctx.send("nothing to skip")

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
                await ctx.send("skipped. queue is empty")
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

            await _send_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Skip Error:", e, flush=True)
        await ctx.send("skip broke")

# ================= FORWARD / REWIND (SEEK) =================

@bot.command()
async def forward(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("im not in vc")

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None:
                return await ctx.send("nothing is playing")

            new_pos = current_elapsed(player) + SEEK_STEP
            duration = player["current"].get("duration")
            if duration and new_pos >= duration:
                new_pos = max(0, duration - 1)

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=new_pos)
            await _send_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Forward Error:", e, flush=True)
        await ctx.send("seek broke")

@bot.command()
async def rewind(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("im not in vc")

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None:
                return await ctx.send("nothing is playing")

            new_pos = max(0, current_elapsed(player) - SEEK_STEP)

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=new_pos)
            await _send_now_playing_panel(ctx.channel, ctx.guild.id)

    except Exception as e:
        print("Rewind Error:", e, flush=True)
        await ctx.send("seek broke")

# ================= SPEED TOGGLE =================

@bot.command()
async def speed(ctx):
    try:
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            return await ctx.send("im not in vc")

        player = get_player(ctx.guild.id)

        async with player["lock"]:
            if player["current"] is None:
                return await ctx.send("nothing is playing")

            current_pos = current_elapsed(player)
            player["speed"] = 2 if player["speed"] == 1 else 1

            await _start_current(ctx.guild.id, vc, player["current"], seek_seconds=current_pos)
            await ctx.send(f"speed set to {player['speed']}x")

    except Exception as e:
        print("Speed Error:", e, flush=True)
        await ctx.send("speed toggle broke")

# ================= NOW PLAYING =================

@bot.command()
async def nowplaying(ctx):
    try:
        player = get_player(ctx.guild.id)
        await ctx.send(_now_playing_text(player, ctx.guild.id))
    except Exception as e:
        print("NowPlaying Error:", e, flush=True)
        await ctx.send("couldn't get now playing info")

# ================= QUEUE VIEW =================

@bot.command()
async def queue(ctx):
    try:
        player = get_player(ctx.guild.id)

        if player["current"] is None and not player["queue"]:
            return await ctx.send("queue is empty")

        lines = []
        if player["current"]:
            lines.append(_now_playing_text(player, ctx.guild.id))
        if player["queue"]:
            lines.append("")
            lines.append("Upcoming:")
            for i, song in enumerate(player["queue"], start=1):
                lines.append(f"{i}. {song['title']} (requested by {song['requester']})")

        await ctx.send("\n".join(lines))
    except Exception as e:
        print("Queue Error:", e, flush=True)
        await ctx.send("couldn't get queue")

@bot.command()
async def chaos(ctx):
    try:
        if not ctx.author.voice:
            return await ctx.send("join vc first")

        query = ''.join(
            random.choice(string.ascii_lowercase)
            for _ in range(3)
        )

        print(f"CHAOS RUNNING: {query}")
        await ctx.send(f"chaos search: {query}")

        await play(ctx, query=query)

    except Exception as e:
        print("Chaos Error:", e)
        await ctx.send("chaos broke")


# ================= COMMAND ERROR LOGGING =================

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    import traceback
    print(f"COMMAND ERROR (command={ctx.command}): {error}", flush=True)
    traceback.print_exception(type(error), error, error.__traceback__)

# ================= READY =================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print("FFmpeg Path:", FFMPEG_PATH)

# ================= RUN =================

keep_alive()
bot.run(TOKEN)
