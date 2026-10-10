"""Voice: Gemini-backed STT, TTS, and a Gemini Live API websocket proxy.

All routes use the existing GEMINI_API_KEY - Google AI Studio's free tier
covers TTS, transcription and the Live API (ai.google.dev/gemini-api/docs/pricing).
No new accounts, no paid dependency.
"""
import asyncio
import base64
import json
import os
import struct

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

router = APIRouter()

REST = "https://generativelanguage.googleapis.com/v1beta"
WSLIVE = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
TTS_MODELS = (os.environ.get("TTS_MODELS") or "gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts").split(",")
STT_MODEL = os.environ.get("STT_MODEL", "gemini-3.5-flash")
LIVE_MODEL = os.environ.get("LIVE_MODEL", "gemini-3.1-flash-live-preview")
TTS_VOICE = os.environ.get("TTS_VOICE", "Kore")
MAX_TTS = 4000
MAX_AUDIO = 8 * 1024 * 1024


def _key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


@router.post("/api/tts")
async def tts(req: Request):
    if int(req.headers.get("content-length") or 0) > 12000:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        text = str((await req.json()).get("text", "")).strip()[:MAX_TTS]
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    if not _key():
        return JSONResponse({"error": "Voice is not configured."}, status_code=503)
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": TTS_VOICE}}},
        },
    }
    async with httpx.AsyncClient(timeout=40) as c:
        for m in TTS_MODELS:
            m = m.strip()
            if not m:
                continue
            try:
                r = await c.post(f"{REST}/models/{m}:generateContent", params={"key": _key()}, json=body)
                if r.status_code != 200:
                    continue
                j = r.json()
                parts = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                for p in parts:
                    data = (p.get("inlineData") or p.get("inline_data") or {}).get("data")
                    if data:
                        pcm = base64.b64decode(data)
                        return Response(_wav(pcm), media_type="audio/wav")
            except Exception:
                continue
    return JSONResponse({"error": "Voice synthesis unavailable right now."}, status_code=503)


@router.post("/api/stt")
async def stt(req: Request):
    audio = await req.body()
    if not audio or len(audio) > MAX_AUDIO:
        return JSONResponse({"error": "Send an audio clip under 8 MB."}, status_code=400)
    if not _key():
        return JSONResponse({"error": "Voice is not configured."}, status_code=503)
    mime = (req.headers.get("content-type") or "audio/webm").split(";")[0].strip()
    if not mime.startswith("audio/"):
        mime = "audio/webm"
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}},
            {"text": "Transcribe this audio exactly as spoken. Reply with only the transcript, no quotes or notes."},
        ]}]
    }
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{REST}/models/{STT_MODEL}:generateContent", params={"key": _key()}, json=body)
        if r.status_code != 200:
            return JSONResponse({"error": "Transcription unavailable right now."}, status_code=503)
        j = r.json()
        parts = (j.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return {"text": text}
    except Exception:
        return JSONResponse({"error": "Transcription unavailable right now."}, status_code=503)


@router.websocket("/ws/live")
async def live(ws: WebSocket):
    await ws.accept()
    if not _key():
        await ws.send_text(json.dumps({"error": "Voice is not configured."}))
        await ws.close()
        return
    try:
        import websockets
    except Exception:
        await ws.send_text(json.dumps({"error": "Live voice is not installed on the server."}))
        await ws.close()
        return
    setup = {
        "setup": {
            "model": f"models/{LIVE_MODEL}",
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "systemInstruction": {"parts": [{"text": "You are Crayon, a friendly, concise voice assistant. Keep spoken replies short and natural. Answer in the user's language."}]},
        }
    }
    try:
        async with websockets.connect(WSLIVE, additional_headers={"x-goog-api-key": _key()}, max_size=8 * 1024 * 1024) as g:
            await g.send(json.dumps(setup))
            first = json.loads(await asyncio.wait_for(g.recv(), timeout=15))
            if "setupComplete" not in first:
                await ws.send_text(json.dumps({"error": "Live voice is unavailable right now."}))
                await ws.close()
                return
            await ws.send_text(json.dumps({"ready": True}))

            async def to_gemini():
                async for raw in ws.iter_text():
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if "audio" in msg:
                        out = {"realtimeInput": {"mediaChunks": [{"mimeType": "audio/pcm;rate=16000", "data": msg["audio"]}]}}
                        await g.send(json.dumps(out))
                    elif msg.get("end"):
                        await g.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

            async def to_client():
                async for raw in g:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    sc = msg.get("serverContent") or {}
                    parts = ((sc.get("modelTurn") or {}).get("parts")) or []
                    out = {}
                    for p in parts:
                        idata = p.get("inlineData") or p.get("inline_data") or {}
                        if idata.get("data"):
                            out.setdefault("audio", []).append(idata["data"])
                        if p.get("text"):
                            out.setdefault("text", []).append(p["text"])
                    if sc.get("interrupted"):
                        out["interrupted"] = True
                    if sc.get("turnComplete"):
                        out["turnComplete"] = True
                    if out:
                        await ws.send_text(json.dumps(out))

            await asyncio.gather(to_gemini(), to_client())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("live proxy error:", type(e).__name__, str(e)[:200])
        try:
            await ws.send_text(json.dumps({"error": "Live voice connection dropped."}))
            await ws.close()
        except Exception:
            pass
