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

from flask import Flask
from threading import Thread

# ================= KEEP ALIVE =================

app = Flask('')

@app.route('/')
def home():
    return "Yen Voice Bot Online"

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

IDLE_TIMEOUT = 120

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="yen ", intents=intents)

last_activity = {}
conversation_history = {}

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

        history.append({
            "role": "user",
            "content": prompt
        })

        history.append({
            "role": "assistant",
            "content": reply
        })

        conversation_history[guild_id] = history[-6:]

        return reply

    except Exception as e:
        print("AI Error:", e, flush=True)
        return "Processing error."

# ================= SPEAK =================

async def speak(vc, text):
    try:
        output_file = "response.mp3"

        if os.path.exists(output_file):
            try:
                os.remove(output_file)
            except:
                pass

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

        vc.play(source)

        print("Speaking:", text)

    except Exception as e:
        print("VOICE ERROR:", e)

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

# ================COMMANDS =================
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

        if vc.is_playing():
            vc.stop()

        await ctx.send("loading song...")

        ydl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "outtmpl": "song.%(ext)s",
}

search_query = query if query.startswith("http") else f"scsearch1:{query}"

with yt_dlp.YoutubeDL(ydl_opts) as ydl:

    info = ydl.extract_info(
        search_query,
        download=True
    )

    if "entries" in info:
        info = info["entries"][0]

    filename = ydl.prepare_filename(info)
    title = info["title"]

        source = discord.FFmpegPCMAudio(
            filename,
            executable=FFMPEG_PATH
        )

        vc.play(source)

        await ctx.send(f"playing: {title}")

    except Exception:
        import traceback

        print("PLAY COMMAND FAILED", flush=True)
        traceback.print_exc()

        await ctx.send("song broke")


# ================= READY =================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print("FFmpeg Path:", FFMPEG_PATH)

# ================= RUN =================

keep_alive()
bot.run(TOKEN)