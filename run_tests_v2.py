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

# Mock HTTP context, the `kural.pipeline_worker` does the real orchestration, but here we can simulate it.
async def simulate_pipeline(http, text, lang, speaker):
    kural.safe_print(f"\n[{text}]")
    transcript = text
    detected_lang = lang
    
    response_language = kural.determine_response_language(detected_lang, transcript)
    intent, intent_num = kural.detect_intent(transcript)
    
    if intent == "MORAL_STORY":
        response_language = "ta-IN"

    kural.safe_print(f"  Detected   : {detected_lang}")
    kural.safe_print(f"  Intent     : {intent}")
    if intent == "THIRUKKURAL_NUMBER":
        kural.safe_print(f"  Number     : {intent_num}")
    
    global last_thirukkural_number
    reply = ""
    
    if intent == "THIRUKKURAL_NUMBER" or intent == "THIRUKKURAL_RANDOM":
        response_language = "ta-IN"
        if not kural.thirukkural_db:
            reply = "என்னிடம் திருக்குறள் புத்தகம் இப்போது இல்லை."
        else:
            if intent == "THIRUKKURAL_NUMBER":
                idx = intent_num - 1
                if 0 <= idx < 1330:
                    k = kural.thirukkural_db[idx]
                    kural.last_thirukkural_number = intent_num
                else:
                    reply = "திருக்குறள் எண் 1 முதல் 1330 வரை தான் இருக்கிறது."
                    k = None
            else: # RANDOM
                available_indices = [i for i in range(1330) if i+1 not in kural.recent_kural_numbers]
                if not available_indices:
                    available_indices = list(range(1330))
                import random
                idx = random.choice(available_indices)
                k = kural.thirukkural_db[idx]
                kural.last_thirukkural_number = idx + 1
                kural.recent_kural_numbers.append(kural.last_thirukkural_number)
                
            if not reply and k:
                num = k.get("Number", kural.last_thirukkural_number)
                line1 = k.get("Line1", "")
                line2 = k.get("Line2", "")
                meaning = k.get("mv", k.get("explanation", ""))
                reply = f"திருக்குறள் எண் {num}.\n\n{line1}\n{line2}\n\nஇதன் பொருள்:\n{meaning}"
        
    elif intent == "THIRUKKURAL_MEANING":
        response_language = "ta-IN"
        if not kural.thirukkural_db or not kural.last_thirukkural_number:
            reply = "எந்த திருக்குறள் என்று எனக்குத் தெரியவில்லை."
        else:
            k = kural.thirukkural_db[kural.last_thirukkural_number - 1]
            meaning = k.get("mv", k.get("explanation", ""))
            reply = f"அந்த திருக்குறளின் பொருள் இதுதான்:\n{meaning}"
        
    else:
        reply = await kural.call_llm(http, transcript, response_language, intent)

    kural.safe_print(f"  Reply      : {reply[:100]}...")
    
    pcm, framerate = await kural.call_tts(http, reply, response_language)
    kural.safe_print(f"  TTS        : {len(pcm)} bytes at {framerate} Hz")
    
    return len(pcm) > 0

async def run_all_tests():
    kural.load_thirukkural()
    
    async with aiohttp.ClientSession() as http:
        tests = [
            ("ஒரு திருக்குறள் சொல்லு", "ta-IN", "ishita"),
            ("இன்னொரு திருக்குறள் சொல்லு", "ta-IN", "ishita"),
            ("இதன் பொருள் என்ன?", "ta-IN", "ishita"),
            ("திருக்குறள் 1 சொல்லு", "ta-IN", "ishita"),
            ("திருக்குறள் 1330 சொல்லு", "ta-IN", "ishita"),
            ("திருக்குறள் 2000 சொல்லு", "ta-IN", "ishita"),
            ("ஒரு moral story சொல்லு", "ta-IN", "ishita"),
            ("Hello Kural, tell me a joke.", "en-IN", "priya"),
            ("मुझे एक कहानी सुनाओ।", "hi-IN", "priya"),
        ]
        
        for text, lang, speaker in tests:
            await simulate_pipeline(http, text, lang, speaker)

if __name__ == "__main__":
    asyncio.run(run_all_tests())
