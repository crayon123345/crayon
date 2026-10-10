"""Discord front-end for the same Crayon brain. Dormant until DISCORD_BOT_TOKEN is set
(secure intake stores it encrypted, like the other keys). Each Discord user gets the same
kind of per-user memory as web visitors, keyed dc_<user id> - isolated per user."""
import asyncio, os, re, threading, time
from collections import defaultdict, deque

import llm
import memory

_token = None
_per_min = defaultdict(deque)


def token():
    global _token
    if _token is None:
        _token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    return _token


def _limited(uid: str) -> bool:
    now = time.time()
    q = _per_min[uid]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 8:
        return True
    q.append(now)
    return False


def register():
    if not token():
        return
    try:
        import discord
    except Exception as e:
        print("discord lib missing:", type(e).__name__)
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print("discord bot online as", client.user)

    @client.event
    async def on_message(msg):
        if msg.author.bot:
            return
        dm = msg.guild is None
        if not dm and client.user not in msg.mentions:
            return
        uid = "dc_" + str(msg.author.id)
        if _limited(uid):
            await msg.reply("Easy there - a few seconds between messages.", mention_author=False)
            return
        text = re.sub(r"<@!?\d+>", "", msg.content).strip()[:4000]
        if text.lower() in ("forget me", "/forget", "delete my data"):
            await asyncio.to_thread(memory.forget_vid, "dc_" + str(msg.author.id))
            await msg.reply("Done - everything I remembered about you is wiped.", mention_author=False)
            return
        if not text:
            await msg.reply("Ask me anything!", mention_author=False)
            return
        vid = uid
        hist = await asyncio.to_thread(memory.load_context, vid)
        async with msg.channel.typing():
            out = []
            try:
                steps = llm.plan(text) if llm.provider() == "gemini" else [("gemini", None, "default")]
                last = None
                for kind, nm, lab in steps:
                    try:
                        mdl = llm.build_or(nm) if kind == "or" else llm.build_model(name=nm) if nm else llm.build_model()
                        async for ch in mdl.astream(llm.to_messages(hist[-32:], text)):
                            tt = llm.chunk_text(ch)
                            if tt:
                                out.append(tt)
                        last = None
                        break
                    except Exception as e2:
                        last = e2
                        print("discord model fail:", type(e2).__name__)
                        if out:
                            break
                if last is not None and not out:
                    raise last
            except Exception:
                out = ["Crayon hit a snag. Try again in a moment."]
        full = "".join(out).strip() or "I got an empty answer - try rephrasing?"
        stripped = re.sub(r"```(python-run|web-search|web-fetch|gmail|image-gen)\n[\s\S]*?(```|$)", "", full).strip()
        full = stripped or "That one needs tools from the web app - try me at crayon-web.onrender.com"
        for i in range(0, len(full), 1900):
            await msg.reply(full[i:i + 1900], mention_author=False)
        await asyncio.to_thread(memory.save_turn, vid, text, full)
        await asyncio.to_thread(memory.event, vid, "discord")

    def run():
        try:
            client.run(token(), log_handler=None)
        except Exception as e:
            print("discord run:", type(e).__name__, str(e)[:120])

    threading.Thread(target=run, daemon=True).start()
