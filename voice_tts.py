"""Voice/TTS engine for Wigglesworth.

Pipeline:
    text -> MP3 (edge-tts, Google fallback) -> raw 48 kHz stereo PCM (ffmpeg)
    -> discord.PCMAudio -> voice client

Design notes:
- ffmpeg only runs while *building* the audio. Playback never spawns a process,
  so a broken /usr/bin/ffmpeg cannot silently swallow the sound.
- Every provider is validated (non-empty audio, decodable PCM) before playing.
- Playback is serialized per guild and retried once if the voice link dropped.
- All failures are reported to the caller and kept in ``status()`` for diagnostics.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import struct
import tempfile
import time

import aiohttp
import discord

VOICES = {
    "aria": "en-US-AriaNeural",
    "jenny": "en-US-JennyNeural",
    "guy": "en-US-GuyNeural",
    "davis": "en-US-DavisNeural",
    "jane": "en-US-JaneNeural",
    "sara": "en-US-SaraNeural",
    "tony": "en-US-TonyNeural",
    "nancy": "en-US-NancyNeural",
}

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

_edge_blocked_until = 0.0
_last_result = "idle"
_last_provider = ""
_last_pcm_seconds = 0.0
_locks: dict[int, asyncio.Lock] = {}


# ---------------------------------------------------------------- text helpers
def clean_text(text: str, limit: int = 600) -> str:
    """Strip mentions, URLs, emoji tokens and markdown before speaking."""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<a?:\w+:\d+>", " ", text)          # custom emoji
    text = re.sub(r"<@[!&]?\d+>|<#\d+>", " ", text)    # mentions
    text = re.sub(r"[*_~>|`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def chunks(text: str, limit: int = 180) -> list[str]:
    """Split text into pieces the Google endpoint accepts (~200 chars)."""
    words = text.split()
    out: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > limit:
            if current:
                out.append(current)
            current = word[:limit]
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out or [text[:limit]]


# ---------------------------------------------------------------- providers
async def _edge_mp3(text: str, voice_key: str) -> bytes:
    import edge_tts
    neural = VOICES.get(voice_key, VOICES["aria"])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    name = tmp.name
    tmp.close()
    try:
        await asyncio.wait_for(
            edge_tts.Communicate(text, voice=neural).save(name), timeout=25)
        if os.path.getsize(name) < 500:
            raise RuntimeError("edge returned empty audio")
        with open(name, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(name)
        except OSError:
            pass


async def _google_mp3(text: str) -> bytes:
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Referer": "https://translate.google.com/",
    }
    timeout = aiohttp.ClientTimeout(total=30)
    parts: list[bytes] = []
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for piece in chunks(text):
            async with session.get(
                "https://translate.google.com/translate_tts",
                params={"ie": "UTF-8", "client": "tw-ob", "tl": "en",
                        "ttsspeed": "1", "q": piece},
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"google HTTP {resp.status}")
                data = await resp.read()
                if len(data) < 500 or data[:1] == b"<":
                    raise RuntimeError("google returned no audio")
                parts.append(data)
    return b"".join(parts)


async def synthesize(text: str, voice: str = "aria") -> tuple[bytes, str]:
    """Return (mp3_bytes, provider_label). Tries edge then Google."""
    global _edge_blocked_until
    spoken = clean_text(text) or "hello"
    errors: list[str] = []
    if time.monotonic() >= _edge_blocked_until:
        try:
            return await _edge_mp3(spoken, voice), f"edge/{voice}"
        except Exception as exc:
            errors.append(f"edge {type(exc).__name__}")
            _edge_blocked_until = time.monotonic() + 300
            print(f"[voice] edge unavailable for 5 min: "
                  f"{type(exc).__name__}: {str(exc)[:140]}", flush=True)
    try:
        return await _google_mp3(spoken), "google"
    except Exception as exc:
        errors.append(f"google {type(exc).__name__}")
        raise RuntimeError(" · ".join(errors)) from exc


# ---------------------------------------------------------------- audio tools
def ffmpeg_path() -> str | None:
    """Bundled imageio ffmpeg first; system ffmpeg only as a last resort."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


async def to_pcm(mp3: bytes) -> bytes:
    """Decode MP3 bytes into raw 48 kHz stereo s16le PCM."""
    exe = ffmpeg_path()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.write(mp3)
    tmp.close()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "-v", "error", "-i", tmp.name,
            "-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
    if proc is None or proc.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", "replace")[:160] if stderr else "no output"
        raise RuntimeError(f"ffmpeg decode failed: {detail}")
    # A valid frame is a multiple of 4 bytes (2 channels × 16-bit).
    return stdout[:len(stdout) - (len(stdout) % 4)]


def tone_pcm(seconds: float = 1.5, freq: int = 440) -> bytes:
    """Generate a clean stereo test tone with short fades (no clicks)."""
    total = int(SAMPLE_RATE * seconds)
    fade = int(SAMPLE_RATE * 0.03)
    frames = bytearray()
    for i in range(total):
        gain = 1.0
        if i < fade:
            gain = i / fade
        elif i > total - fade:
            gain = (total - i) / fade
        sample = int(32767 * 0.28 * gain * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
        frames += struct.pack("<hh", sample, sample)
    return bytes(frames)


def _write_pcm(pcm: bytes) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pcm")
    tmp.write(pcm)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------- voice state
def crypto_note(vc: discord.VoiceClient | None) -> str:
    conn = getattr(vc, "_connection", None) if vc else None
    if conn is None:
        return "no connection"
    session = getattr(conn, "dave_session", None)
    status = getattr(session, "status", "?") if session else "?"
    epoch = getattr(session, "epoch", "?") if session else "?"
    return (f"dave_proto={getattr(conn, 'dave_protocol_version', 0)} "
            f"session={'yes' if session else 'no'} "
            f"status={status} epoch={epoch} "
            f"can_encrypt={bool(getattr(conn, 'can_encrypt', False))} "
            f"mode={getattr(conn, 'mode', '?')}")


async def ensure_voice(guild: discord.Guild,
                       preferred: discord.VoiceChannel | None = None,
                       stored_id: int | None = None) -> discord.VoiceClient | None:
    """Return a connected voice client, moving/reconnecting as needed."""
    vc = guild.voice_client
    if vc and vc.is_connected():
        if preferred is not None and vc.channel is not None and vc.channel.id != preferred.id:
            try:
                await vc.move_to(preferred)
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"[voice] move failed: {type(exc).__name__}", flush=True)
        return vc
    channel = preferred
    if not isinstance(channel, discord.VoiceChannel) and stored_id:
        channel = guild.get_channel(stored_id)
    if not isinstance(channel, discord.VoiceChannel):
        return None
    if vc is not None:
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
    try:
        return await asyncio.wait_for(channel.connect(self_deaf=True), timeout=25)
    except Exception as exc:
        print(f"[voice] connect failed: {type(exc).__name__}: {exc}", flush=True)
        return None


async def _play(guild: discord.Guild, pcm: bytes,
                preferred: discord.VoiceChannel | None,
                stored_id: int | None, label: str) -> str:
    """Play PCM in the guild's VC. Returns a status string."""
    global _last_result, _last_provider, _last_pcm_seconds
    try:
        vc = await ensure_voice(guild, preferred, stored_id)
    except Exception as exc:
        _last_result = f"connect failed: {type(exc).__name__}"
        return f"couldn't reach voice ({type(exc).__name__})"
    if vc is None or not vc.is_connected():
        _last_result = "not connected to voice"
        return "not in voice — join a channel and run `.vcjoin`"

    # Wait for any previous clip instead of throwing "already playing".
    for _ in range(120):
        if not vc.is_playing():
            break
        await asyncio.sleep(0.25)

    path = _write_pcm(pcm)
    handle = open(path, "rb")
    source = discord.PCMAudio(handle)

    def _after(error: Exception | None) -> None:
        if error is not None:
            print(f"[voice] playback error: {type(error).__name__}: {error}", flush=True)
        try:
            handle.close()
        except Exception:
            pass
        try:
            os.remove(path)
        except OSError:
            pass

    try:
        vc.play(source, after=_after)
    except discord.ClientException:
        # Link probably rebuilt underneath us: reconnect once and retry.
        try:
            handle.seek(0)
            await vc.disconnect(force=True)
            vc = await ensure_voice(guild, preferred, stored_id)
            if vc is None:
                _after(None)
                _last_result = "voice reconnect failed"
                return "voice link died — reconnect with `.vcjoin`"
            vc.play(discord.PCMAudio(handle), after=_after)
        except Exception as exc:
            _after(None)
            _last_result = f"play failed: {type(exc).__name__}"
            return f"playback failed ({type(exc).__name__})"

    seconds = len(pcm) / BYTES_PER_SECOND
    _last_provider = label
    _last_pcm_seconds = seconds
    _last_result = f"playing {seconds:.1f}s via {label}"
    print(f"[voice] {label} | {seconds:.1f}s | {crypto_note(vc)}", flush=True)
    return "speaking"


# ---------------------------------------------------------------- public API
async def speak(guild: discord.Guild, text: str, *,
                voice: str = "aria",
                preferred: discord.VoiceChannel | None = None,
                stored_id: int | None = None) -> str:
    """Speak ``text`` in the guild's voice channel."""
    global _last_result
    lock = _locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        try:
            mp3, provider = await synthesize(text, voice)
        except Exception as exc:
            _last_result = f"TTS failed: {exc}"[:300]
            return f"TTS failed ({exc})"
        try:
            pcm = await to_pcm(mp3)
        except Exception as exc:
            _last_result = f"decode failed: {exc}"[:300]
            return f"audio decode failed ({exc})"
        if len(pcm) < BYTES_PER_SECOND // 8:
            _last_result = "decoded audio was empty"
            return "audio came back empty — try again"
        return await _play(guild, pcm, preferred, stored_id, provider)


async def play_tone(guild: discord.Guild, *,
                    preferred: discord.VoiceChannel | None = None,
                    stored_id: int | None = None) -> str:
    """Play a short beep, bypassing TTS entirely."""
    return await _play(guild, tone_pcm(), preferred, stored_id, "tone")


def status(guild: discord.Guild) -> dict:
    """Snapshot for `.voicediag`."""
    vc = guild.voice_client
    state = guild.me.voice if guild.me else None
    conn = getattr(vc, "_connection", None) if vc else None
    try:
        import importlib.metadata as md
        davey_ver = md.version("davey")
    except Exception:
        davey_ver = "MISSING"
    try:
        import edge_tts
        edge_ver = getattr(edge_tts, "__version__", "installed")
    except ImportError:
        edge_ver = "MISSING"
    return {
        "vc": vc,
        "channel": getattr(vc, "channel", None),
        "connected": bool(vc and vc.is_connected()),
        "playing": bool(vc and vc.is_playing()),
        "server_mute": bool(state and state.mute),
        "server_deaf": bool(state and state.deaf),
        "self_mute": bool(state and state.self_mute),
        "self_deaf": bool(state and state.self_deaf),
        "dave_proto": getattr(conn, "dave_protocol_version", 0) if conn else 0,
        "dave_session": bool(getattr(conn, "dave_session", None)) if conn else False,
        "dave_status": (str(getattr(getattr(conn, "dave_session", None), "status", "?"))
                        if conn else "?"),
        "dave_epoch": (getattr(getattr(conn, "dave_session", None), "epoch", "?")
                       if conn else "?"),
        "can_encrypt": bool(getattr(conn, "can_encrypt", False)) if conn else False,
        "davey": davey_ver,
        "edge": edge_ver,
        "ffmpeg": ffmpeg_path(),
        "last_result": _last_result,
        "last_provider": _last_provider,
        "last_seconds": _last_pcm_seconds,
        "edge_blocked": time.monotonic() < _edge_blocked_until,
    }


async def probe(voice: str = "aria") -> list[str]:
    """Test both TTS providers and the decode step. Returns display lines."""
    results: list[str] = []
    for name, fn in (("edge", lambda: _edge_mp3("voice test one two", voice)),
                     ("google", lambda: _google_mp3("voice test one two"))):
        started = time.perf_counter()
        try:
            data = await fn()
            pcm = await to_pcm(data)
            results.append(f"✅ {name} — {len(data)} MP3 bytes, "
                           f"{len(pcm) / BYTES_PER_SECOND:.1f}s audio "
                           f"({time.perf_counter() - started:.1f}s)")
        except Exception as exc:
            results.append(f"❌ {name} — `{type(exc).__name__}: {str(exc)[:110]}`")
    return results


def reset_edge_backoff() -> None:
    global _edge_blocked_until
    _edge_blocked_until = 0.0
