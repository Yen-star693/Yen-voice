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
            timeout=20
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

        await asyncio.sleep(1)

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

    vc = discord.utils.get(
        bot.voice_clients,
        guild=guild
    )

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

            match = re.search(
                r'/channels/(\d+)/(\d+)',
                vc_link
            )

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

        vc = discord.utils.get(
            bot.voice_clients,
            guild=ctx.guild
        )

        if vc:

            if vc.channel == channel:
                return await ctx.send("already there")

            await vc.move_to(channel)

        else:

            vc = await channel.connect(
                reconnect=True,
                timeout=30
            )

        # IMPORTANT FOR RENDER
        await asyncio.sleep(3)

        # VERIFY CONNECTION
        vc = discord.utils.get(
            bot.voice_clients,
            guild=ctx.guild
        )

        if vc and vc.is_connected():

            last_activity[ctx.guild.id] = time.time()

            asyncio.create_task(
                auto_disconnect(ctx.guild.id)
            )

            await ctx.send(f"joined {channel.name}")

            print(f"Joined VC: {channel.name}")

        else:
            return await ctx.send("voice connection failed")

    except Exception as e:
        print("Join Error:", e)
        await ctx.send("couldn't join vc")

# ================= LEAVE VC =================

@bot.command()
async def leave(ctx):

    try:

        vc = discord.utils.get(
            bot.voice_clients,
            guild=ctx.guild
        )

        if vc:

            if vc.is_playing():
                vc.stop()

            await vc.disconnect()

            await ctx.send("bye")

    except Exception as e:
        print("Leave Error:", e)

# ================= STOP AUDIO =================

@bot.command()
async def stop(ctx):

    try:

        vc = discord.utils.get(
            bot.voice_clients,
            guild=ctx.guild
        )

        if vc and vc.is_playing():

            vc.stop()

            await ctx.send("stopped")

    except Exception as e:
        print("Stop Error:", e)

# ================= AI SPEAK =================

@bot.command()
async def ask(ctx, *, question):

    try:

        vc = discord.utils.get(
            bot.voice_clients,
            guild=ctx.guild
        )

        if not vc or not vc.is_connected():
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

        response = ask_ai(question)

        # VOICE ONLY
        await speak(vc, response)

    except Exception as e:
        print("Ask Error:", e)
        await ctx.send("voice broke")

# ================= AUTO CHAT TRIGGER =================

@bot.event
async def on_message(message):

    try:

        if message.author.bot:
            return

        await bot.process_commands(message)

        # ignore commands
        if message.content.startswith("yen "):
            return

        if not message.guild:
            return

        vc = discord.utils.get(
            bot.voice_clients,
            guild=message.guild
        )

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

            response = ask_ai(question)

            # VOICE ONLY
            await speak(vc, response)

    except Exception as e:
        print("Message Error:", e)

# ================= READY =================

@bot.event
async def on_ready():

    print(f"Logged in as {bot.user}")
    print("FFmpeg Path:", FFMPEG_PATH)

# ================= RUN =================

keep_alive()
bot.run(TOKEN)