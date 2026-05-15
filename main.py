import discord
from discord.ext import commands
import requests
import asyncio
import edge_tts
import os
import imageio_ffmpeg
import re
import time

from flask import Flask
from threading import Thread

# ================= KEEP ALIVE =================

app = Flask('')

@app.route('/')
def home():
    return "Yen Voice Bot Online"

def run_web():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

# ================= CONFIG =================

TOKEN = "TOKEN"
GROQ_KEY = "GROQ_KEY"

# automatic ffmpeg path for replit/mobile hosting
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# auto leave after inactivity
IDLE_TIMEOUT = 120

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="yen ",
    intents=intents
)

# ================= ACTIVITY TRACKING =================

last_activity = {}

# ================= AI =================

def ask_ai(prompt):

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are Yen. "
                            "Sarcastic, casual, funny, blunt. "
                            "Keep responses short and natural."
                        )
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                "max_tokens": 80
            },
            timeout=15
        )

        if r.status_code != 200:
            print("Groq Error:", r.text)
            return "api exploded"

        data = r.json()

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI Error:", e)
        return "my brain exploded"

# ================= PLAY AUDIO =================

async def speak(vc, text):

    output_file = "response.mp3"

    communicate = edge_tts.Communicate(
        text=text,
        voice="en-US-GuyNeural"
    )

    await communicate.save(output_file)

    while vc.is_playing():
        await asyncio.sleep(0.5)

    vc.play(
        discord.FFmpegPCMAudio(
            output_file,
            executable=FFMPEG_PATH
        )
    )

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
            print(f"Disconnected from {guild.name} due to inactivity")
        except Exception as e:
            print("Disconnect Error:", e)

# ================= JOIN VC =================

@bot.command()
async def join(ctx, *, vc_link=None):

    try:

        # ================= JOIN THROUGH LINK =================
        if vc_link:

            match = re.search(r'/channels/(\d+)/(\d+)', vc_link)

            if not match:
                return await ctx.send("invalid vc link")

            guild_id = int(match.group(1))
            channel_id = int(match.group(2))

            guild = bot.get_guild(guild_id)

            if not guild:
                return await ctx.send("guild not found")

            channel = guild.get_channel(channel_id)

            if not channel:
                return await ctx.send("vc not found")

            if not isinstance(channel, discord.VoiceChannel):
                return await ctx.send("not a vc")

        # ================= NORMAL JOIN =================
        else:

            if not ctx.author.voice:
                return await ctx.send("join vc first")

            channel = ctx.author.voice.channel

        # ================= CONNECT =================

        if ctx.voice_client:

            if ctx.voice_client.channel == channel:
                return await ctx.send("already there")

            await ctx.voice_client.move_to(channel)

        else:
            await channel.connect()

        last_activity[ctx.guild.id] = time.time()

        asyncio.create_task(
            auto_disconnect(ctx.guild.id)
        )

        await ctx.send(f"joined {channel.name}")

    except Exception as e:
        print("Join Error:", e)
        await ctx.send("couldn't join vc")

# ================= LEAVE VC =================

@bot.command()
async def leave(ctx):

    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("bye")

# ================= STOP AUDIO =================

@bot.command()
async def stop(ctx):

    vc = ctx.voice_client

    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("stopped")

# ================= AI SPEAK =================

@bot.command()
async def ask(ctx, *, question):

    vc = ctx.voice_client

    if not vc:
        return await ctx.send("im not in vc")

    # only same vc users
    if (
        not ctx.author.voice
        or ctx.author.voice.channel != vc.channel
    ):
        return await ctx.send("you gotta be in my vc")

    last_activity[ctx.guild.id] = time.time()

    asyncio.create_task(
        auto_disconnect(ctx.guild.id)
    )

    if vc.is_playing():
        vc.stop()

    response = ask_ai(question)

    await ctx.send(response)

    await speak(vc, response)

# ================= AUTO CHAT TRIGGER =================

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    await bot.process_commands(message)

    # ignore commands
    if message.content.startswith("yen "):
        return

    if not message.guild:
        return

    # only respond if bot is in vc
    vc = message.guild.voice_client

    if not vc:
        return

    # user must be in same vc
    if (
        not message.author.voice
        or message.author.voice.channel != vc.channel
    ):
        return

    # trigger
    if message.content.lower().startswith("hey yen"):

        question = message.content[8:].strip()

        if not question:
            return

        last_activity[message.guild.id] = time.time()

        asyncio.create_task(
            auto_disconnect(message.guild.id)
        )

        if vc.is_playing():
            vc.stop()

        response = ask_ai(question)

        await message.reply(response)

        await speak(vc, response)

# ================= READY =================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# ================= RUN =================

keep_alive()
bot.run(TOKEN)