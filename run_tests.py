import asyncio
import aiohttp
import os
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

async def run_single_test(http, name, test_text, tts_lang, tts_speaker):
    print(f"\n{'='*50}\nRUNNING TEST: {name}\n{'='*50}")
    
    # 1. Generate fake microphone audio
    wav_bytes = await get_test_wav(http, test_text, tts_lang, tts_speaker)
    print(f"[{name}] Generated synthetic test audio: {len(wav_bytes)} bytes")
    
    # 2. Test STT
    transcript, detected_lang = await kural.call_stt(http, wav_bytes)
    kural.safe_print(f"[{name}] STT Transcript: {transcript}")
    print(f"[{name}] STT Detected Lang: {detected_lang}")
    
    # 3. Test Language Classification
    response_lang = kural.determine_response_language(detected_lang, transcript)
    print(f"[{name}] Classified Response Lang: {response_lang}")
    
    # 4. Test LLM
    llm_reply = await kural.call_llm(http, transcript, response_lang)
    kural.safe_print(f"[{name}] LLM Reply: {llm_reply}")
    
    # 5. Test TTS
    pcm, framerate = await kural.call_tts(http, llm_reply, response_lang)
    print(f"[{name}] Final TTS PCM size: {len(pcm)}, Framerate: {framerate}")
    
    return len(pcm) > 0 and framerate > 0

async def run_all_tests():
    async with aiohttp.ClientSession() as http:
        tests = [
            ("Test A (English)", "Hello Kural, tell me a joke.", "en-IN", "priya"),
            ("Test B (Tamil)", "குரல், எனக்கு ஒரு கதை சொல்லு.", "ta-IN", "ishita"),
            ("Test C (Hindi)", "मुझे एक कहानी सुनाओ।", "hi-IN", "priya"),
            ("Test D (Telugu)", "నాకు ఒక కథ చెప్పు.", "te-IN", "priya"),
            ("Test E (Mixed)", "Kural, எனக்கு ஒரு story சொல்லு.", "ta-IN", "ishita"),
        ]
        
        results = []
        for name, text, lang, speaker in tests:
            success = await run_single_test(http, name, text, lang, speaker)
            results.append((name, success))
            
        print("\n\nFINAL RESULTS:")
        for name, success in results:
            print(f"{name}: {'PASS' if success else 'FAIL'}")

if __name__ == "__main__":
    asyncio.run(run_all_tests())
