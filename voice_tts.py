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

# Current Microsoft Edge voices (checked against edge_tts.list_voices()).
# ``gender`` is used so the fallback never turns a male voice into a female one.
VOICE_META: dict[str, dict[str, str]] = {
    # --- US English
    "aria":        {"neural": "en-US-AriaNeural",        "gender": "female", "accent": "US"},
    "jenny":       {"neural": "en-US-JennyNeural",       "gender": "female", "accent": "US"},
    "mommy":       {"neural": "en-US-AvaNeural",         "gender": "female", "accent": "US", "preset": "soft"},
    "ava":         {"neural": "en-US-AvaNeural",         "gender": "female", "accent": "US"},
    "emma":        {"neural": "en-US-EmmaNeural",        "gender": "female", "accent": "US"},
    "michelle":    {"neural": "en-US-MichelleNeural",    "gender": "female", "accent": "US"},
    "guy":         {"neural": "en-US-GuyNeural",         "gender": "male",   "accent": "US"},
    "andrew":      {"neural": "en-US-AndrewNeural",      "gender": "male",   "accent": "US"},
    "brian":       {"neural": "en-US-BrianNeural",       "gender": "male",   "accent": "US"},
    "christopher": {"neural": "en-US-ChristopherNeural", "gender": "male",   "accent": "US"},
    "eric":        {"neural": "en-US-EricNeural",        "gender": "male",   "accent": "US"},
    "roger":       {"neural": "en-US-RogerNeural",       "gender": "male",   "accent": "US"},
    "steffan":     {"neural": "en-US-SteffanNeural",     "gender": "male",   "accent": "US"},
    # --- UK / Ireland
    "libby":       {"neural": "en-GB-LibbyNeural",       "gender": "female", "accent": "UK"},
    "sonia":       {"neural": "en-GB-SoniaNeural",       "gender": "female", "accent": "UK"},
    "maisie":      {"neural": "en-GB-MaisieNeural",      "gender": "female", "accent": "UK"},
    "ryan":        {"neural": "en-GB-RyanNeural",        "gender": "male",   "accent": "UK"},
    "thomas":      {"neural": "en-GB-ThomasNeural",      "gender": "male",   "accent": "UK"},
    "emily":       {"neural": "en-IE-EmilyNeural",       "gender": "female", "accent": "IE"},
    "connor":      {"neural": "en-IE-ConnorNeural",      "gender": "male",   "accent": "IE"},
    # --- Australia / Canada / India / NZ / South Africa
    "natasha":     {"neural": "en-AU-NatashaNeural",     "gender": "female", "accent": "AU"},
    "william":     {"neural": "en-AU-WilliamMultilingualNeural", "gender": "male", "accent": "AU"},
    "clara":       {"neural": "en-CA-ClaraNeural",       "gender": "female", "accent": "CA"},
    "liam":        {"neural": "en-CA-LiamNeural",        "gender": "male",   "accent": "CA"},
    "neerja":      {"neural": "en-IN-NeerjaNeural",      "gender": "female", "accent": "IN"},
    "prabhat":     {"neural": "en-IN-PrabhatNeural",     "gender": "male",   "accent": "IN"},
    "molly":       {"neural": "en-NZ-MollyNeural",       "gender": "female", "accent": "NZ"},
    "mitchell":    {"neural": "en-NZ-MitchellNeural",    "gender": "male",   "accent": "NZ"},
    "leah":        {"neural": "en-ZA-LeahNeural",        "gender": "female", "accent": "ZA"},
    "luke":        {"neural": "en-ZA-LukeNeural",        "gender": "male",   "accent": "ZA"},
    # --- Legacy names (Microsoft retired these voices). They now map to the
    # closest living voice so old configs keep working instead of silently
    # falling back to Google's female voice.
    "davis":       {"neural": "en-US-AndrewNeural", "gender": "male",   "accent": "US", "alias": "andrew"},
    "tony":        {"neural": "en-US-BrianNeural",  "gender": "male",   "accent": "US", "alias": "brian"},
    "jane":        {"neural": "en-US-EmmaNeural",   "gender": "female", "accent": "US", "alias": "emma"},
    "sara":        {"neural": "en-US-AvaNeural",    "gender": "female", "accent": "US", "alias": "ava"},
    "nancy":       {"neural": "en-US-MichelleNeural", "gender": "female", "accent": "US", "alias": "michelle"},
}
VOICES = {name: meta["neural"] for name, meta in VOICE_META.items()}
LEGACY = {name for name, meta in VOICE_META.items() if "alias" in meta}
# Voice-specific delivery. ``mommy`` is Ava with a slower, lower, softer tone.
VOICE_PRESETS: dict[str, dict[str, int]] = {
    "mommy": {"rate": -18, "pitch": -4},
}
# Gentle close-mic treatment for the ASMR-style voices. Applied to the decoded
# PCM, so it cannot break playback if the filter is unsupported.
VOICE_FILTERS: dict[str, str] = {
    "mommy": "highpass=f=90,lowpass=f=5000,volume=0.80",
}

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

_edge_blocked_until = 0.0
_last_result = "idle"
_last_provider = ""
_last_voice = ""
_last_pcm_seconds = 0.0
_locks: dict[int, asyncio.Lock] = {}
_generation: dict[int, int] = {}


def meta(voice: str) -> dict[str, str]:
    """Metadata for a voice key, falling back to aria."""
    return VOICE_META.get(voice, VOICE_META["aria"])


def display_name(voice: str) -> str:
    """Human name, e.g. ``andrew`` or ``davis (legacy → andrew)``."""
    m = meta(voice)
    alias = m.get("alias")
    return f"{voice} (legacy → {alias})" if alias else voice


def effective_tone(voice: str, rate: int = 0, pitch: int = 0) -> tuple[int, int]:
    """Apply a voice preset on top of the user's speed/pitch settings."""
    preset = VOICE_PRESETS.get(voice)
    if not preset:
        return rate, pitch
    return rate + preset["rate"], pitch + preset["pitch"]


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
async def _edge_mp3(text: str, voice_key: str,
                    rate: int = 0, pitch: int = 0) -> bytes:
    import edge_tts
    neural = VOICES.get(voice_key, VOICES["aria"])
    last_error: Exception | None = None
    for attempt in range(2):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        name = tmp.name
        tmp.close()
        try:
            await asyncio.wait_for(
                edge_tts.Communicate(
                    text,
                    voice=neural,
                    rate=f"{int(rate):+d}%",
                    pitch=f"{int(pitch):+d}Hz",
                ).save(name),
                timeout=25,
            )
            if os.path.getsize(name) < 500:
                raise RuntimeError("edge returned empty audio")
            with open(name, "rb") as fh:
                return fh.read()
        except Exception as exc:
            last_error = exc
            # Edge occasionally returns an empty stream when throttled. One
            # short retry fixes it without hiding a genuinely retired voice.
            if type(exc).__name__ == "NoAudioReceived" and attempt == 0:
                await asyncio.sleep(0.5)
                continue
            raise
        finally:
            try:
                os.remove(name)
            except OSError:
                pass
    if last_error is not None:
        raise last_error
    raise RuntimeError("edge returned no audio")


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


async def synthesize(text: str, voice: str = "aria",
                     rate: int = 0, pitch: int = 0) -> tuple[bytes, str]:
    """Return (mp3_bytes, provider_label).

    Edge is the real voice. Google is only a fallback for female voices, because
    its single English voice is female. Substituting it for a male voice is what
    made "davis" sound like a girl, so male voices fail loudly instead.
    """
    global _edge_blocked_until
    spoken = clean_text(text) or "hello"
    gender = meta(voice).get("gender", "female")
    rate, pitch = effective_tone(voice, rate, pitch)
    errors: list[str] = []
    edge_error: Exception | None = None

    if time.monotonic() >= _edge_blocked_until:
        try:
            return (await _edge_mp3(spoken, voice, rate, pitch),
                    f"edge/{voice}")
        except Exception as exc:
            edge_error = exc
            # Some voices reject one specific pitch/rate combination. Retry at
            # neutral settings before declaring the voice dead.
            if type(exc).__name__ == "NoAudioReceived" and (rate or pitch):
                try:
                    data = await _edge_mp3(spoken, voice)
                    print(f"[voice] {voice}: rate/pitch rejected, used neutral "
                          f"settings", flush=True)
                    return data, f"edge/{voice} (neutral tone)"
                except Exception as exc2:
                    edge_error = exc2
            errors.append(f"edge {type(edge_error).__name__}")
            # A missing voice is voice-specific; don't poison edge for 5 min.
            if type(edge_error).__name__ != "NoAudioReceived":
                _edge_blocked_until = time.monotonic() + 300
                print(f"[voice] edge unavailable for 5 min: "
                      f"{type(edge_error).__name__}: {str(edge_error)[:140]}",
                      flush=True)
            else:
                print(f"[voice] edge has no audio for voice {voice!r} "
                      f"({VOICES.get(voice)})", flush=True)

    if gender == "female":
        try:
            return await _google_mp3(spoken), "google (female fallback)"
        except Exception as exc:
            errors.append(f"google {type(exc).__name__}")

    if edge_error is not None:
        raise RuntimeError(
            f"{' · '.join(errors)} — edge failed for {display_name(voice)} "
            f"({VOICES.get(voice)})"
        ) from edge_error
    raise RuntimeError(
        f"edge is cooling down and Google cannot imitate a male voice "
        f"({display_name(voice)})"
    )


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


async def to_pcm(mp3: bytes, audio_filter: str | None = None,
                 gain: int = 100) -> bytes:
    """Decode MP3 bytes into raw 48 kHz stereo s16le PCM."""
    exe = ffmpeg_path()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.write(mp3)
    tmp.close()
    proc = None
    filters = [audio_filter] if audio_filter else []
    if gain != 100:
        filters.append(f"volume={max(0, min(100, gain)) / 100:.2f}")
    args = [exe, "-v", "error", "-i", tmp.name]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-f", "s16le", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
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
                stored_id: int | None, label: str,
                generation: int | None = None) -> str:
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

    if generation is not None and _generation.get(guild.id, 0) != generation:
        _last_result = "stopped"
        return "cancelled"

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
                stored_id: int | None = None,
                rate: int = 0,
                pitch: int = 0,
                gain: int = 100) -> str:
    """Speak ``text`` in the guild's voice channel."""
    global _last_result, _last_voice
    generation = _generation.get(guild.id, 0)
    lock = _locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        if _generation.get(guild.id, 0) != generation:
            _last_result = "stopped"
            return "cancelled"
        try:
            mp3, provider = await synthesize(text, voice, rate, pitch)
        except Exception as exc:
            _last_result = f"TTS failed: {exc}"[:300]
            return f"TTS failed ({exc})"
        if _generation.get(guild.id, 0) != generation:
            _last_result = "stopped"
            return "cancelled"
        try:
            pcm = await to_pcm(mp3, VOICE_FILTERS.get(voice), gain)
        except Exception as exc:
            _last_result = f"decode failed: {exc}"[:300]
            return f"audio decode failed ({exc})"
        if len(pcm) < BYTES_PER_SECOND // 8:
            _last_result = "decoded audio was empty"
            return "audio came back empty — try again"
        result = await _play(guild, pcm, preferred, stored_id, provider, generation)
        if result == "speaking":
            _last_voice = voice
        return result


async def play_tone(guild: discord.Guild, *,
                    preferred: discord.VoiceChannel | None = None,
                    stored_id: int | None = None) -> str:
    """Play a short beep, bypassing TTS entirely."""
    return await _play(guild, tone_pcm(), preferred, stored_id, "tone")


def stop(guild: discord.Guild) -> str:
    """Stop the current clip and cancel anything waiting to speak."""
    global _last_result
    _generation[guild.id] = _generation.get(guild.id, 0) + 1
    vc = guild.voice_client
    was_playing = bool(vc and vc.is_playing())
    if was_playing:
        try:
            vc.stop()
        except Exception:
            pass
    _last_result = "stopped" if was_playing else "stop requested"
    return "stopped" if was_playing else "idle"


def status(guild: discord.Guild) -> dict:
    """Snapshot for `.voicediag`."""
    vc = guild.voice_client
    member = getattr(guild, "me", None)
    state = member.voice if member else None
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
        "last_voice": _last_voice,
        "last_seconds": _last_pcm_seconds,
        "edge_blocked": time.monotonic() < _edge_blocked_until,
    }


async def probe(voice: str = "aria") -> list[str]:
    """Test both TTS providers and the decode step. Returns display lines."""
    results: list[str] = []
    rate, pitch = effective_tone(voice, 0, 0)
    for name, fn in (("edge", lambda: _edge_mp3("voice test one two", voice, rate, pitch)),
                     ("google", lambda: _google_mp3("voice test one two"))):
        started = time.perf_counter()
        try:
            data = await fn()
            pcm = await to_pcm(data, VOICE_FILTERS.get(voice))
            results.append(f"✅ {name} — {len(data)} MP3 bytes, "
                           f"{len(pcm) / BYTES_PER_SECOND:.1f}s audio "
                           f"({time.perf_counter() - started:.1f}s)")
        except Exception as exc:
            results.append(f"❌ {name} — `{type(exc).__name__}: {str(exc)[:110]}`")
    return results


def reset_edge_backoff() -> None:
    global _edge_blocked_until
    _edge_blocked_until = 0.0
