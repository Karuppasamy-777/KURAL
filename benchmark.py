import asyncio
import aiohttp
import os
import time
import importlib.util
import sys
from dotenv import load_dotenv

spec = importlib.util.spec_from_file_location("kural", "c:/Users/acer/OneDrive/Documents/KURAL-TEST/kural_gemini_voice_test.py.py")
kural = importlib.util.module_from_spec(spec)
sys.modules["kural"] = kural
spec.loader.exec_module(kural)

load_dotenv()
kural.SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

async def get_test_wav(http, text, language, speaker):
    payload = {
        "text": text,
        "model": "bulbul:v3",
        "target_language_code": language,
        "speaker": speaker,
        "pace": 0.95,
        "output_audio_codec": "wav"
    }
    headers = {
        "api-subscription-key": kural.SARVAM_API_KEY,
        "Content-Type": "application/json"
    }
    async with http.post("https://api.sarvam.ai/text-to-speech", json=payload, headers=headers) as resp:
        data = await resp.json()
        import base64
        audio_b64 = "".join(data.get("audios", []))
        return base64.b64decode(audio_b64)

async def run_benchmark():
    # optimized connector as per kural_gemini_voice_test
    connector = aiohttp.TCPConnector(limit=10, limit_per_host=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as http:
        wav_bytes = await get_test_wav(http, "Hello Kural, tell me a joke.", "en-IN", "priya")
        
        # WARMUP RUN to cache DNS and TCP connections
        print("Warming up connections...")
        await kural.call_stt(http, wav_bytes)
        await kural.call_llm(http, "Hello Kural", "en-IN")
        await kural.call_tts(http, "Hi there", "en-IN")
        print("Warmup complete. Measuring...")
        
        t0 = time.perf_counter()
        
        # 1. STT
        t_stt_start = time.perf_counter()
        transcript, detected_lang = await kural.call_stt(http, wav_bytes)
        t_stt_end = time.perf_counter()
        
        # 2. Language Detection
        response_lang = kural.determine_response_language(detected_lang, transcript)
        
        # 3. LLM
        t_llm_start = time.perf_counter()
        llm_reply = await kural.call_llm(http, transcript, response_lang)
        t_llm_end = time.perf_counter()
        
        # 4. TTS
        t_tts_start = time.perf_counter()
        pcm, framerate = await kural.call_tts(http, llm_reply, response_lang)
        t_tts_end = time.perf_counter()
        
        t1 = time.perf_counter()
        
        stt_ms = (t_stt_end - t_stt_start) * 1000
        llm_ms = (t_llm_end - t_llm_start) * 1000
        tts_ms = (t_tts_end - t_tts_start) * 1000
        total_ms = (t1 - t0) * 1000
        
        print("\n[BENCHMARK]")
        print(f"STT: {stt_ms:.1f} ms")
        print(f"LLM: {llm_ms:.1f} ms")
        print(f"TTS: {tts_ms:.1f} ms")
        print(f"Total: {total_ms:.1f} ms")

if __name__ == "__main__":
    asyncio.run(run_benchmark())
