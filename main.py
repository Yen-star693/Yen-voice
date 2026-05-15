import discord
from discord.ext import commands
import requests
import asyncio
import edge_tts
import os

# ================= CONFIG =================

TOKEN = "TOKEN"
GROQ_KEY = "GROQ_KEY"

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="yen ",
    intents=intents
)

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

        data = r.json()

        return data["choices"][0]["message"]["content"]

    except Exception as e:
        print("AI Error:", e)
        return "my brain exploded"

# ================= JOIN VC =================

@bot.command()
async def join(ctx):

    if not ctx.author.voice:
        return await ctx.send("join vc first")

    channel = ctx.author.voice.channel

    if ctx.voice_client:

        if ctx.voice_client.channel == channel:
            return await ctx.send("already there")

        await ctx.voice_client.move_to(channel)

    else:
        await channel.connect()

    await ctx.send("joined")

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

    # prevent overlap
    if vc.is_playing():
        vc.stop()

    # get ai response
    response = ask_ai(question)

    # send text reply
    await ctx.send(response)

    # generate speech
    communicate = edge_tts.Communicate(
        text=response,
        voice="en-US-GuyNeural"
    )

    output_file = "response.mp3"

    await communicate.save(output_file)

    # play audio
    vc.play(
        discord.FFmpegPCMAudio(output_file)
    )

# ================= AUTO CHAT TRIGGER =================

@bot.event
async def on_message(message):

    if message.author.bot:
        return

    await bot.process_commands(message)

    # ignore commands
    if message.content.startswith("yen "):
        return

    # only respond if bot is in vc
    vc = message.guild.voice_client

    if not vc:
        return

    # trigger if starts with "hey yen"
    if message.content.lower().startswith("hey yen"):

        question = message.content[8:].strip()

        if not question:
            return

        if vc.is_playing():
            vc.stop()

        response = ask_ai(question)

        await message.reply(response)

        communicate = edge_tts.Communicate(
            text=response,
            voice="en-US-GuyNeural"
        )

        output_file = "response.mp3"

        await communicate.save(output_file)

        vc.play(
            discord.FFmpegPCMAudio(output_file)
        )

# ================= READY =================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# ================= RUN =================

bot.run(TOKEN)