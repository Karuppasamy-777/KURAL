import asyncio
import queue
import threading
import time
import base64
import json
import struct
import io
import collections
import os
import wave

import numpy as np
import sounddevice as sd
import aiohttp
import sys
import random
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================
# KURAL - SARVAM AI LIVE VOICE
# ============================================================

# Security: Read API key from environment variable
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

# ---- STT ----
STT_URL      = "https://api.sarvam.ai/speech-to-text"
STT_MODEL    = "saaras:v3"          # REST model

# ============================================================
# API SHARED HEADERS
# ============================================================

SARVAM_HEADERS_JSON = {
    "api-subscription-key": SARVAM_API_KEY,
    "Content-Type": "application/json"
}

SARVAM_HEADERS_FORM = {
    "api-subscription-key": SARVAM_API_KEY
}

# ---- LLM ----
LLM_URL   = "https://api.sarvam.ai/v1/chat/completions"
LLM_MODEL = "sarvam-105b-conversations"

# ---- TTS ----
TTS_URL      = "https://api.sarvam.ai/text-to-speech"
TTS_MODEL    = "bulbul:v3"

# Female Speakers for Tamil and English
TTS_TAMIL_SPEAKER = "ishita"
TTS_ENGLISH_SPEAKER = "priya"

# ---- Audio ----
INPUT_RATE  = 16000
BLOCK_SIZE  = 160               # 10 ms chunks for responsive VAD

# ---- VAD settings ----
VAD_SPEECH_THRESHOLD  = 1200   # RMS above this = speaking (raise if too sensitive)
VAD_SILENCE_THRESHOLD = 600    # RMS below this = silent
VAD_MIN_SPEECH_MS     = 600    # ignore bursts shorter than this (ms)
VAD_SILENCE_END_MS    = 2000   # silence this long = end of utterance (ms)
VAD_MAX_CHUNK_SEC     = 25     # hard cap before forcing a send

# ============================================================
# THIRUKKURAL DATABASE
# ============================================================

thirukkural_db = []
recent_kural_numbers = collections.deque(maxlen=15)
last_thirukkural_number = None

def load_thirukkural():
    global thirukkural_db
    try:
        db_path = os.path.join(os.path.dirname(__file__), "thirukkural.json")
        with open(db_path, "r", encoding="utf-8") as f:
            thirukkural_db = json.load(f)
            
        if len(thirukkural_db) == 1330:
            safe_print("[THIRUKKURAL] Loading database...")
            safe_print("[THIRUKKURAL] 1330 Kurals loaded successfully.")
            safe_print("[THIRUKKURAL] Database status: READY")
        else:
            safe_print(f"[ERROR] Thirukkural database is incomplete. Expected: 1330, Found: {len(thirukkural_db)}")
            safe_print("[THIRUKKURAL] Database status: ERROR")
            thirukkural_db = []
    except Exception as e:
        safe_print("[ERROR] Thirukkural database not found or invalid.")
        safe_print(f"[THIRUKKURAL] Database status: ERROR ({e})")
        thirukkural_db = []

# ============================================================
# SHARED STATE
# ============================================================

mic_queue        = queue.Queue()
speech_queue     = asyncio.Queue()   # completed WAV bytes -> STT
speaker_queue    = queue.Queue()     # (pcm_bytes, framerate) items
conversation_history = []

kural_speaking = threading.Event()   # True while KURAL plays audio
playback_finished = threading.Event()# Triggered when playback genuinely completes
mic_level      = 0.0                 # live RMS (0.0–1.0) for meter

# ============================================================
# SAFE PRINTING
# ============================================================

def safe_print(text: str, end="\n", flush=False):
    """Prints text safely on Windows terminals that lack full Unicode support."""
    try:
        print(text, end=end, flush=flush)
    except UnicodeEncodeError:
        safe_text = text.encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding)
        print(safe_text, end=end, flush=flush)

# ============================================================
# WAV HELPERS
# ============================================================

def make_wav(pcm_frames: list, rate: int = INPUT_RATE) -> bytes:
    """Pack a list of int16 numpy arrays into a valid WAV bytes object."""
    if not pcm_frames:
        return b""
    # Removed unnecessary astype() copy since input is already int16 from sounddevice
    raw = b"".join(f.tobytes() for f in pcm_frames)
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

    mic_queue.put_nowait((pcm, rms))

# ============================================================
# SPEAKER WORKER  (background thread)
# ============================================================

def speaker_worker():
    while True:
        item = speaker_queue.get()
        if item is None:
            break
            
        pcm_bytes, framerate = item
        try:
            with sd.RawOutputStream(
                samplerate=framerate,
                channels=1,
                dtype="int16",
                blocksize=0,
                latency="low"
            ) as stream:
                stream.write(pcm_bytes)
        except Exception as e:
            print("Speaker error:", e)
        finally:
            playback_finished.set()

# ============================================================
# VAD WORKER  (thread: mic_queue -> speech_queue)
# Detects start/end of speech dynamically using RMS
# ============================================================

def vad_worker(loop: asyncio.AbstractEventLoop):
    frames_per_ms = INPUT_RATE / 1000
    chunk_frames = BLOCK_SIZE

    min_speech_chunks = int(VAD_MIN_SPEECH_MS / (BLOCK_SIZE * 1000 / INPUT_RATE))
    silence_end_chunks = int(VAD_SILENCE_END_MS / (BLOCK_SIZE * 1000 / INPUT_RATE))
    max_chunks = int(VAD_MAX_CHUNK_SEC * INPUT_RATE / BLOCK_SIZE)

    while True:
        # Clear out any stale audio from previous turns
        while not mic_queue.empty():
            try:
                mic_queue.get_nowait()
            except queue.Empty:
                break
                
        buf = []
        pre_buffer = collections.deque(maxlen=30) # keep ~300ms pre-speech audio
        speech_chunks = 0
        silence_chunks = 0
        in_speech = False
        
        while True:
            try:
                frame, rms = mic_queue.get(timeout=0.1)
            except queue.Empty:
                continue
                
            if not in_speech:
                pre_buffer.append(frame)
                if rms > VAD_SPEECH_THRESHOLD:
                    speech_chunks += 1
                    # Require a few consecutive loud chunks to confirm speech
                    if speech_chunks >= 5:
                        in_speech = True
                        buf.extend(pre_buffer)
                        print("\n[REC] Speech detected, recording...", flush=True)
                else:
                    speech_chunks = 0
            else:
                buf.append(frame)
                if rms < VAD_SILENCE_THRESHOLD:
                    silence_chunks += 1
                else:
                    silence_chunks = 0
                    
                # End utterance if silence reaches threshold or max duration is hit
                if silence_chunks >= silence_end_chunks or len(buf) >= max_chunks:
                    if len(buf) > min_speech_chunks:
                        print(f"[REC] Silence detected. Sending {len(buf)} chunks to STT...", flush=True)
                        wav = make_wav(buf)
                        asyncio.run_coroutine_threadsafe(speech_queue.put(wav), loop)
                    else:
                        print(f"[REC] Audio too short, ignoring.", flush=True)
                        
                    # Break to start listening for a new utterance
                    break

# ============================================================
# LANGUAGE CLASSIFICATION
# ============================================================

def normalize_language_code(language: str) -> str:
    if not language:
        return ""
    return language.strip().lower().replace("_", "-")

def determine_response_language(detected_language: str, transcript: str) -> str:
    """
    Decides between ONLY two response languages: "ta-IN" (Tamil) or "en-IN" (English).
    Tamil input -> Tamil. Everything else -> English.
    """
    normalized = normalize_language_code(detected_language)
    
    # 1. Trust STT detected language if it exactly indicates Tamil
    if normalized in {"ta", "ta-in", "tamil"}:
        return "ta-IN"
        
    # 2. Threshold-based Tamil Unicode fallback
    tamil_chars = sum(1 for c in transcript if '\u0B80' <= c <= '\u0BFF')
    alphabetic_chars = sum(1 for c in transcript if c.isalpha())
    
    if tamil_chars >= 2 and tamil_chars / max(alphabetic_chars, 1) >= 0.20:
        return "ta-IN"
        
    # Default to English for all other languages (Hindi, Telugu, Malayalam, Kannada, etc.)
    return "en-IN"

# ============================================================
# INTENT CLASSIFICATION
# ============================================================

def detect_intent(transcript: str) -> tuple[str, int]:
    """Returns (intent_type, number)"""
    t = transcript.lower().strip()
    
    # 1. THIRUKKURAL NUMBER
    num_match = re.search(r'(?:திருக்குறள்|குறள்|thirukkural)\s*.*?(\d+)', t)
    if num_match:
        return "THIRUKKURAL_NUMBER", int(num_match.group(1))
        
    # 2. THIRUKKURAL MEANING
    meaning_words = {"பொருள்", "meaning", "அர்த்தம்", "விளக்கம்", "explain", "சொல்லுது"}
    if any(w in t for w in meaning_words) and ("இந்த" in t or "இதன்" in t or "அதன்" in t or "இதை" in t):
        if last_thirukkural_number is not None:
            return "THIRUKKURAL_MEANING", last_thirukkural_number
            
    # 3. THIRUKKURAL RANDOM
    kural_words = ["திருக்குறள்", "thirukkural", "ஒரு குறள் சொல்லு", "குறள் சொல்லு"]
    if any(w in t for w in kural_words) or ("இன்னொன்று" in t and last_thirukkural_number is not None):
        return "THIRUKKURAL_RANDOM", 0
        
    # 4. MORAL STORY
    story_words = {"கதை", "story", "நீதிக்கதை"}
    if any(w in t for w in story_words):
        return "MORAL_STORY", 0
        
    return "NORMAL", 0

# ============================================================
# STT  (WAV bytes -> transcript text + language)
# ============================================================

async def call_stt(http: aiohttp.ClientSession, wav_bytes: bytes) -> tuple[str, str]:
    form = aiohttp.FormData()
    form.add_field(
        "file",
        io.BytesIO(wav_bytes),
        filename="audio.wav",
        content_type="audio/wav"
    )
    form.add_field("model", STT_MODEL)
    form.add_field("language_code", "unknown")

    try:
        async with http.post(STT_URL, headers=SARVAM_HEADERS_FORM, data=form) as resp:
            if resp.status != 200:
                err = await resp.text()
                print(f"[STT] Error {resp.status}: {err}")
                return "", "Unknown"

            data = await resp.json()
            transcript = data.get("transcript", "").strip()
            detected_language = data.get("language_code", "Unknown")
            return transcript, detected_language
    except Exception as e:
        print(f"[STT] Exception: {e}")
        return "", "Unknown"

# ============================================================
# LLM  (transcript -> reply text)
# ============================================================

async def call_llm(http: aiohttp.ClientSession, user_text: str, response_language: str, intent: str = "NORMAL") -> str:
    # Build dynamic prompt per turn based on intent and language
    if intent == "MORAL_STORY":
        system_prompt = (
            "You are KURAL, a friendly AI companion for children.\n"
            "Create an engaging moral story in simple conversational Tamil.\n"
            "The story should take approximately 5 minutes to speak aloud.\n"
            "Use child-friendly language. Include interesting characters, dialogue, a meaningful problem, a satisfying solution, and a clear positive moral.\n"
            "The story must be completely in Tamil, although a small number of commonly used English words may naturally appear in conversational Tamil.\n"
            "Do not use Hindi, Telugu, Malayalam, Kannada, Bengali, or any other language.\n"
            "Do not include unsafe, excessively frightening, violent, hateful, or inappropriate content.\n"
            "End with a simple moral lesson."
        )
        max_toks = 1000
    elif response_language == "ta-IN":
        system_prompt = (
            "You are KURAL, a friendly AI companion for children.\n"
            "The child may speak Tamil, English, Hindi, Telugu, Malayalam, Kannada, or other languages.\n"
            "The current user's spoken language has been detected as Tamil.\n"
            "Therefore, respond ONLY in natural, conversational Tamil.\n"
            "You may understand English words naturally mixed into Tamil speech.\n"
            "Keep the response short, warm, friendly, and child-safe.\n"
            "Do not switch to Hindi, Telugu, Malayalam, Kannada, or other languages."
        )
        max_toks = 200
    else:
        system_prompt = (
            "You are KURAL, a friendly AI companion for children.\n"
            "The child may speak Tamil, English, Hindi, Telugu, Malayalam, Kannada, or other languages.\n"
            "The current user's spoken language has NOT been detected as Tamil.\n"
            "Therefore, respond ONLY in clear simple English.\n"
            "Understand the user's original language and preserve their intended meaning.\n"
            "Do not answer in Hindi, Telugu, Malayalam, Kannada, Bengali, Marathi, Gujarati, Punjabi, or any other language.\n"
            "Keep the response short, warm, friendly, and child-safe."
        )
        max_toks = 200

    conversation_history.append({"role": "user", "content": user_text})

    # Find the last system message if it exists, replace it or prepend
    messages = [{"role": "system", "content": system_prompt}] + conversation_history[-10:]

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_toks,
    }

    try:
        async with http.post(LLM_URL, json=payload, headers=SARVAM_HEADERS_JSON) as resp:
            if resp.status != 200:
                err = await resp.text()
                print(f"[LLM] Error {resp.status}: {err}")
                return ""
            data = await resp.json()
            reply = data["choices"][0]["message"]["content"].strip()

        conversation_history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        print(f"[LLM] Exception: {e}")
        return ""

# ============================================================
# TTS  (reply text -> raw PCM bytes, framerate)
# ============================================================
async def call_tts(http: aiohttp.ClientSession, text: str, response_language: str, speaker: str = None) -> tuple[bytes, int]:
    if not text.strip():
        return b"", 22050

    if speaker is None:
        speaker = TTS_TAMIL_SPEAKER if response_language == "ta-IN" else TTS_ENGLISH_SPEAKER

    # Sarvam TTS max limit is 2500 characters. We chunk it by sentences.
    import textwrap
    import base64
    import wave
    import io
    
    sentences = re.split(r'(?<=[.!?\n])\s+', text)
    chunks = []
    curr = ""
    for s in sentences:
        if len(curr) + len(s) + 1 <= 2000:
            curr += s + " "
        else:
            if curr.strip():
                chunks.append(curr.strip())
            if len(s) > 2000:
                chunks.extend(textwrap.wrap(s, width=2000))
                curr = ""
            else:
                curr = s + " "
    if curr.strip():
        chunks.append(curr.strip())

    all_pcm = []
    framerate = 22050
    
    for chunk in chunks:
        payload = {
            "text": chunk,
            "model": TTS_MODEL,
            "target_language_code": response_language,
            "speaker": speaker,
            "pace": 0.95,
            "output_audio_codec": "wav"
        }

        try:
            async with http.post(TTS_URL, json=payload, headers=SARVAM_HEADERS_JSON) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    print(f"[TTS] Error {resp.status}: {err}")
                    continue
                data = await resp.json()
                audio_b64 = "".join(data.get("audios", []))
                wav_bytes = base64.b64decode(audio_b64)
                
                with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                    framerate = wf.getframerate()
                    pcm_bytes = wf.readframes(wf.getnframes())
                    all_pcm.append(pcm_bytes)
        except Exception as e:
            print(f"[TTS] Exception: {e}")
            
    return b"".join(all_pcm), framerate

# ============================================================
# MAIN PIPELINE WORKER
# ============================================================

async def pipeline_worker():
    connector = aiohttp.TCPConnector(limit=10, limit_per_host=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as http:
        while True:
            wav_bytes = await speech_queue.get()
            if wav_bytes is None:
                break

            safe_print("\n[PAUSED] Processing speech...", flush=True)

            t_start = time.perf_counter()
            # Step 1: STT
            t_stt_start = time.perf_counter()
            transcript, detected_language = await call_stt(http, wav_bytes)
            t_stt_end = time.perf_counter()

            if not transcript:
                print("(could not transcribe - try again)", flush=True)
                print("[MIC] Listening...", flush=True)
                continue

            # Step 2: Decide response language and intent
            response_language = determine_response_language(detected_language, transcript)
            intent, intent_num = detect_intent(transcript)
            
            # Context override: Moral story triggers Tamil mode
            if intent == "MORAL_STORY":
                response_language = "ta-IN"

            speaker_name = TTS_TAMIL_SPEAKER if response_language == "ta-IN" else TTS_ENGLISH_SPEAKER

            print("========================================")
            if intent == "NORMAL":
                print("USER SPEECH")
            elif intent == "MORAL_STORY":
                print("MORAL STORY")
            else:
                print("THIRUKKURAL")
            print("========================================")
            
            if intent == "NORMAL":
                safe_print(f"Transcript : {transcript}")
                print(f"Detected   : {detected_language}")
                print(f"Response   : {'Tamil' if response_language == 'ta-IN' else 'English'}")
                print(f"Intent     : NORMAL")
            elif intent == "MORAL_STORY":
                print(f"Language   : Tamil")
                print(f"Target     : ~5 minutes")
            else:
                print(f"Mode       : {'NUMBER' if intent == 'THIRUKKURAL_NUMBER' else 'RANDOM'}")
                if intent == "THIRUKKURAL_NUMBER":
                    print(f"Number     : {intent_num}")
                print(f"Language   : Tamil")
            print(f"Voice      : {speaker_name}")
            print("========================================")

            # Step 3: LLM or DB Lookup
            safe_print("[KURAL thinking...]")
            t_llm_start = time.perf_counter()
            
            global last_thirukkural_number
            reply = ""
            
            if intent == "THIRUKKURAL_NUMBER" or intent == "THIRUKKURAL_RANDOM":
                response_language = "ta-IN" # Force Tamil
                if not thirukkural_db:
                    reply = "என்னிடம் திருக்குறள் புத்தகம் இப்போது இல்லை."
                else:
                    if intent == "THIRUKKURAL_NUMBER":
                        idx = intent_num - 1
                        if 0 <= idx < 1330:
                            k = thirukkural_db[idx]
                            last_thirukkural_number = intent_num
                        else:
                            reply = "திருக்குறள் எண் 1 முதல் 1330 வரை தான் இருக்கிறது."
                            k = None
                    else: # RANDOM
                        available_indices = [i for i in range(1330) if i+1 not in recent_kural_numbers]
                        if not available_indices:
                            available_indices = list(range(1330))
                        idx = random.choice(available_indices)
                        k = thirukkural_db[idx]
                        last_thirukkural_number = idx + 1
                        recent_kural_numbers.append(last_thirukkural_number)
                        
                    if not reply and k:
                        num = k.get("Number", last_thirukkural_number)
                        line1 = k.get("Line1", "")
                        line2 = k.get("Line2", "")
                        meaning = k.get("mv", k.get("explanation", ""))
                        reply = f"திருக்குறள் எண் {num}.\n\n{line1}\n{line2}\n\nஇதன் பொருள்:\n{meaning}"
                t_llm_end = time.perf_counter()
                
            elif intent == "THIRUKKURAL_MEANING":
                response_language = "ta-IN"
                if not thirukkural_db or not last_thirukkural_number:
                    reply = "எந்த திருக்குறள் என்று எனக்குத் தெரியவில்லை."
                else:
                    k = thirukkural_db[last_thirukkural_number - 1]
                    meaning = k.get("mv", k.get("explanation", ""))
                    reply = f"அந்த திருக்குறளின் பொருள் இதுதான்:\n{meaning}"
                t_llm_end = time.perf_counter()
                
            else:
                reply = await call_llm(http, transcript, response_language, intent)
                t_llm_end = time.perf_counter()

            if not reply:
                print("[MIC] Listening...", flush=True)
                continue

            # Step 3.5: Validation (Only for Normal LLM queries)
            if intent in ("NORMAL", "MORAL_STORY"):
                tamil_chars_in_reply = any('\u0B80' <= c <= '\u0BFF' for c in reply)
                
                if response_language == "en-IN" and tamil_chars_in_reply:
                    print("[WARNING] LLM generated Tamil but expected English. Requesting rewrite...")
                    rewrite_prompt = (
                        "The previous answer violated the required output language. "
                        "Rewrite the answer in English only. Preserve the original meaning. "
                        "Return only the corrected answer."
                    )
                    reply2 = await call_llm(http, rewrite_prompt, response_language, intent)
                    if reply2 and not any('\u0B80' <= c <= '\u0BFF' for c in reply2):
                        reply = reply2
                    else:
                        reply = "I understand, but I can only reply in English right now."
                        
                elif response_language == "ta-IN" and not tamil_chars_in_reply:
                    print("[WARNING] LLM generated English but expected Tamil. Requesting rewrite...")
                    rewrite_prompt = (
                        "The previous answer was completely in English, but you MUST respond primarily in Tamil. "
                        "Rewrite the answer in natural, conversational Tamil. "
                        "Return only the corrected Tamil answer."
                    )
                    reply2 = await call_llm(http, rewrite_prompt, response_language, intent)
                    if reply2 and any('\u0B80' <= c <= '\u0BFF' for c in reply2):
                        reply = reply2

            safe_print(f"KURAL: {reply}")

            # Step 4: TTS
            safe_print("[KURAL SPEAKING]", flush=True)
            t_tts_start = time.perf_counter()
            pcm_bytes, framerate = await call_tts(http, reply, response_language)
            t_tts_end = time.perf_counter()
            
            t_total_end = time.perf_counter()
            
            stt_ms = (t_stt_end - t_stt_start) * 1000
            llm_ms = (t_llm_end - t_llm_start) * 1000
            tts_ms = (t_tts_end - t_tts_start) * 1000
            total_ms = (t_total_end - t_start) * 1000
            processing_ms = total_ms - (stt_ms + llm_ms + tts_ms)
            
            safe_print("\n[PERFORMANCE]")
            safe_print(f"STT:       {stt_ms:.1f} ms")
            safe_print(f"LLM:       {llm_ms:.1f} ms")
            safe_print(f"TTS:       {tts_ms:.1f} ms")
            safe_print(f"Processing:{processing_ms:.1f} ms")
            safe_print(f"Total:     {total_ms:.1f} ms\n")

            if not pcm_bytes or framerate == 0:
                print("[MIC] Listening...", flush=True)
                continue

            # Step 5: Play audio safely
            kural_speaking.set()
            playback_finished.clear()

            speaker_queue.put((pcm_bytes, framerate))

            # Wait for playback to genuinely finish confirmation from the stream
            await asyncio.to_thread(playback_finished.wait)

            await asyncio.sleep(0.4)   # brief safety gap after playback

            # clear queue to prevent hearing own voice
            while not mic_queue.empty():
                try:
                    mic_queue.get_nowait()
                except queue.Empty:
                    break

            # Unmute mic
            kural_speaking.clear()
            print("[MIC] Listening...", flush=True)

# ============================================================
# LIVE MIC LEVEL DISPLAY
# ============================================================

async def mic_level_display():
    BAR_WIDTH = 20
    while True:
        await asyncio.sleep(0.2)  # Reduced frequency to 5 updates/sec to save CPU

        if kural_speaking.is_set():
            bar = "-" * BAR_WIDTH
            print(f"\r[MUTED] [{bar}]   ", end="", flush=True)
        else:
            level  = mic_level
            filled = int(level * BAR_WIDTH)
            bar    = "#" * filled + "-" * (BAR_WIDTH - filled)

            if level > 0.5:
                label = f" [VOL] SPEAKING  "
            elif level > 0.1:
                label = f" [VOL] voice...   "
            else:
                label = f" [MIC] Listening..."

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
    vad_thread = threading.Thread(target=vad_worker, args=(loop,), daemon=True)
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
        print("   KURAL BILINGUAL VOICE READY")
        print("=" * 32)
        print()
        print("Speak in ANY language (Tamil, Hindi, Malayalam, English, etc.)")
        print("KURAL responds in:")
        print("  - Tamil (if you speak Tamil or Tamil+English)")
        print("  - English (for all other languages)")
        print()
        print("Press CTRL+C to stop.")
        print()
        print("[MIC] Listening...", flush=True)

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

    # Load Thirukkural Database
    load_thirukkural()
    
    if not SARVAM_API_KEY or SARVAM_API_KEY == "PASTE_YOUR_SARVAM_API_KEY_HERE":
        print("ERROR: Please set your SARVAM_API_KEY environment variable.")
        print("In Windows PowerShell, run:")
        print('    $env:SARVAM_API_KEY="your_api_key_here"')
        print("Or place it in your .env file as: SARVAM_API_KEY=your_api_key_here")
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
            print(type(e).__name__, "-", str(e))
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("=" * 32)
        print("  KURAL STOPPED")
        print("=" * 32)
