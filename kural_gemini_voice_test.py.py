import asyncio
import queue
import threading
import time
import base64
import json
import struct
import io

import numpy as np
import sounddevice as sd
import aiohttp
import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================
# KURAL - SARVAM AI LIVE VOICE
# ============================================================

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# ---- STT ----
STT_URL      = "https://api.sarvam.ai/speech-to-text"
STT_MODEL    = "saaras:v3"          # REST model

# ---- LLM ----
LLM_URL   = "https://api.sarvam.ai/v1/chat/completions"
LLM_MODEL = "sarvam-105b-conversations"

# ---- TTS ----
TTS_URL      = "https://api.sarvam.ai/text-to-speech"
TTS_LANGUAGE = "en-IN"
TTS_SPEAKER  = "shubh"
TTS_MODEL    = "bulbul:v3"

# ---- Audio ----
INPUT_RATE  = 16000
OUTPUT_RATE = 22050
BLOCK_SIZE  = 160               # 10 ms chunks for responsive VAD

# ---- VAD settings ----
VAD_SPEECH_THRESHOLD  = 1200   # RMS above this = speaking (raise if too sensitive)
VAD_SILENCE_THRESHOLD = 600    # RMS below this = silent
VAD_MIN_SPEECH_MS     = 600    # ignore bursts shorter than this (ms)
VAD_SILENCE_END_MS    = 1200   # silence this long = end of utterance (ms)
VAD_MAX_CHUNK_SEC     = 25      # hard cap before forcing a send

# ---- System prompt ----
SYSTEM_PROMPT = (
    "You are KURAL, a friendly AI companion for children. "
    "The child may speak in any language — Tamil, Hindi, Telugu, "
    "Malayalam, Kannada, English, or a mix. "
    "Always understand what the child says and always respond "
    "ONLY in clear, simple English or tamil. "
    "Use short, playful, warm, and age-appropriate sentences. "
    "Never respond in Tamil, Hindi, or any other language "
    "unless the child explicitly asks you to."
)


# ============================================================
# SHARED STATE
# ============================================================

mic_queue        = queue.Queue()
speech_queue     = asyncio.Queue()   # completed WAV bytes -> STT
speaker_queue    = queue.Queue(maxsize=200)
conversation_history = []

kural_speaking = threading.Event()  # True while KURAL plays audio
mic_level      = 0.0                # live RMS (0.0–1.0) for meter


# ============================================================
# WAV HELPERS
# ============================================================

def make_wav(pcm_frames: list, rate: int = INPUT_RATE) -> bytes:
    """Pack a list of int16 numpy arrays into a valid WAV bytes object."""
    raw = b"".join(f.astype(np.int16).tobytes() for f in pcm_frames)
    num_samples   = len(raw) // 2
    num_channels  = 1
    bits_per_sample = 16
    byte_rate     = rate * num_channels * bits_per_sample // 8
    block_align   = num_channels * bits_per_sample // 8
    data_size     = len(raw)
    chunk_size    = 36 + data_size

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", chunk_size, b"WAVE",
        b"fmt ", 16, 1, num_channels, rate, byte_rate,
        block_align, bits_per_sample,
        b"data", data_size
    )
    return header + raw


# ============================================================
# MICROPHONE CALLBACK
# ============================================================

def mic_callback(indata, frames, time_info, status):
    global mic_level

    if status:
        print("MIC:", status)

    pcm = indata[:, 0].copy()

    rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
    mic_level = min(rms / 4000.0, 1.0)

    if kural_speaking.is_set():
        return

    mic_queue.put_nowait(pcm)


# ============================================================
# SPEAKER WORKER  (background thread)
# ============================================================

def speaker_worker():
    try:
        with sd.RawOutputStream(
            samplerate=OUTPUT_RATE,
            channels=1,
            dtype="int16",
            blocksize=0,
            latency="low"
        ) as stream:
            while True:
                data = speaker_queue.get()
                if data is None:
                    break
                try:
                    stream.write(data)
                except Exception as e:
                    print("Speaker error:", e)
    except Exception as e:
        print("Speaker init error:", e)


# ============================================================
# VAD WORKER  (thread: mic_queue -> speech_queue)
# Collects mic frames, detects start/end of speech by RMS,
# and puts completed WAV bytes into speech_queue.
# ============================================================

def vad_worker(loop: asyncio.AbstractEventLoop):
    """TEST MODE: Records for exactly 5 seconds then sends to Sarvam STT."""

    RECORD_SECONDS = 5
    frames_needed  = int(RECORD_SECONDS * INPUT_RATE / BLOCK_SIZE)

    while True:
        print(f"\n[REC] Recording for {RECORD_SECONDS} seconds...", flush=True)
        buf = []

        while len(buf) < frames_needed:
            try:
                frame = mic_queue.get(timeout=1.0)
                buf.append(frame)
            except queue.Empty:
                continue

        print("[REC] Done. Sending to STT...", flush=True)
        wav = make_wav(buf)
        asyncio.run_coroutine_threadsafe(
            speech_queue.put(wav), loop
        )


# ============================================================
# STT  (WAV bytes -> transcript text)
# ============================================================

async def call_stt(http: aiohttp.ClientSession, wav_bytes: bytes) -> str:

    headers = {"api-subscription-key": SARVAM_API_KEY}

    form = aiohttp.FormData()
    form.add_field(
        "file",
        io.BytesIO(wav_bytes),
        filename="audio.wav",
        content_type="audio/wav"
    )
    form.add_field("model", STT_MODEL)

    async with http.post(STT_URL, headers=headers, data=form) as resp:

        if resp.status != 200:
            err = await resp.text()
            print(f"[STT] Error {resp.status}: {err}")
            return ""

        data = await resp.json()
        return data.get("transcript", "").strip()


# ============================================================
# LLM  (transcript -> reply text)
# ============================================================

async def call_llm(http: aiohttp.ClientSession, user_text: str) -> str:

    conversation_history.append({"role": "user", "content": user_text})

    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + conversation_history
    )

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 200,
    }

    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json"
    }

    async with http.post(LLM_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            err = await resp.text()
            print(f"[LLM] Error {resp.status}: {err}")
            return ""
        data = await resp.json()
        reply = data["choices"][0]["message"]["content"].strip()

    conversation_history.append({"role": "assistant", "content": reply})
    return reply


# ============================================================
# TTS  (reply text -> raw PCM bytes)
# ============================================================

async def call_tts(http: aiohttp.ClientSession, text: str) -> bytes:

    payload = {
        "text": text,
        "model": TTS_MODEL,
        "target_language_code": TTS_LANGUAGE,
        "speaker": TTS_SPEAKER,
        "pace": 0.95,
        "output_audio_codec": "wav"
    }

    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json"
    }

    async with http.post(TTS_URL, json=payload, headers=headers) as resp:
        if resp.status != 200:
            err = await resp.text()
            print(f"[TTS] Error {resp.status}: {err}")
            return b""
        data = await resp.json()

    audio_b64 = "".join(data.get("audios", []))
    wav_bytes  = base64.b64decode(audio_b64)
    return wav_bytes[44:]   # strip 44-byte WAV header -> raw PCM


# ============================================================
# MAIN PIPELINE WORKER
# Pulls completed speech WAVs, runs STT -> LLM -> TTS -> Speaker
# ============================================================

async def pipeline_worker():

    async with aiohttp.ClientSession() as http:

        while True:

            wav_bytes = await speech_queue.get()

            if wav_bytes is None:
                break

            print("\n⏸  Processing speech...", flush=True)

            # Step 1: STT
            transcript = await call_stt(http, wav_bytes)

            if not transcript:
                print("(could not transcribe — try again)", flush=True)
                print("🎤 Listening...", flush=True)
                continue

            print(f"YOU: {transcript}")

            # Step 2: LLM
            print("[KURAL thinking...]")
            reply = await call_llm(http, transcript)

            if not reply:
                print("🎤 Listening...", flush=True)
                continue

            print(f"KURAL: {reply}")

            # Step 3: TTS
            print("🔊 [KURAL SPEAKING]", flush=True)
            pcm_bytes = await call_tts(http, reply)

            if not pcm_bytes:
                print("🎤 Listening...", flush=True)
                continue

            # Step 4: Mute mic, flush stale audio, play
            kural_speaking.set()

            while not mic_queue.empty():
                try:
                    mic_queue.get_nowait()
                except queue.Empty:
                    break

            chunk_size = OUTPUT_RATE * 2   # 1 second per chunk
            for i in range(0, len(pcm_bytes), chunk_size):
                speaker_queue.put(pcm_bytes[i:i + chunk_size])

            # Wait for playback to finish
            while not speaker_queue.empty():
                await asyncio.sleep(0.1)

            await asyncio.sleep(0.4)   # brief gap after playback

            # Unmute mic
            kural_speaking.clear()
            print("🎤 Listening...", flush=True)


# ============================================================
# LIVE MIC LEVEL DISPLAY  (updates 10x per second)
# ============================================================

async def mic_level_display():

    BAR_WIDTH = 20

    while True:
        await asyncio.sleep(0.1)

        if kural_speaking.is_set():
            bar = chr(0x2591) * BAR_WIDTH
            print(f"\r{chr(0x1F507)} [MUTED]  [{bar}]   ", end="", flush=True)
        else:
            level  = mic_level
            filled = int(level * BAR_WIDTH)
            bar    = chr(0x2588) * filled + chr(0x2591) * (BAR_WIDTH - filled)

            if level > 0.5:
                label = f" {chr(0x1F5E3)}  SPEAKING  "
            elif level > 0.1:
                label = f" {chr(0x1F509)} voice...   "
            else:
                label = f" {chr(0x1F3A4)} Listening..."

            print(f"\r[{bar}]{label}", end="", flush=True)


# ============================================================
# MAIN SESSION
# ============================================================

async def run_session():

    loop = asyncio.get_running_loop()

    # Speaker thread
    speaker_thread = threading.Thread(target=speaker_worker, daemon=True)
    speaker_thread.start()

    # VAD thread (mic_queue -> speech_queue)
    vad_thread = threading.Thread(
        target=vad_worker, args=(loop,), daemon=True
    )
    vad_thread.start()

    print("Starting microphone...")
    print()

    with sd.InputStream(
        samplerate=INPUT_RATE,
        channels=1,
        dtype="int16",
        blocksize=BLOCK_SIZE,
        latency="low",
        callback=mic_callback
    ):
        print("=" * 32)
        print("   VOICE CHAT READY")
        print("=" * 32)
        print()
        print("Speak in any language.")
        print("KURAL always responds in English.")
        print()
        print("Press CTRL+C to stop.")
        print()
        print("🎤 Listening...", flush=True)

        await asyncio.gather(
            pipeline_worker(),
            mic_level_display()
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    print()
    print("=" * 32)
    print("  KURAL - SARVAM AI VOICE")
    print("=" * 32)
    print()

    if (
        not SARVAM_API_KEY
        or SARVAM_API_KEY == "PASTE_YOUR_SARVAM_API_KEY_HERE"
    ):
        print("ERROR: Please set your SARVAM_API_KEY in the script.")
        return

    MAX_RECONNECTS  = 5
    RECONNECT_DELAY = 3
    attempt         = 0

    while attempt <= MAX_RECONNECTS:

        if attempt > 0:
            print()
            print(f"Restarting... (attempt {attempt} of {MAX_RECONNECTS})")
            print()
            await asyncio.sleep(RECONNECT_DELAY)

        try:
            await run_session()
            break

        except KeyboardInterrupt:
            raise

        except Exception as e:
            print()
            print("=" * 32)
            print("ERROR")
            print("=" * 32)
            print(type(e).__name__, "—", str(e))
            print()
            attempt += 1

            if attempt > MAX_RECONNECTS:
                print("Too many errors. Stopping.")
                break

    try:
        speaker_queue.put_nowait(None)
    except queue.Full:
        pass


# ============================================================
# START
# ============================================================

try:
    asyncio.run(main())

except KeyboardInterrupt:
    print()
    print("=" * 32)
    print("  KURAL STOPPED")
    print("=" * 32)




