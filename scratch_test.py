import asyncio
import aiohttp
import os
import base64
import wave
import io
from dotenv import load_dotenv

load_dotenv()
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")

async def test_sarvam():
    async with aiohttp.ClientSession() as http:
        payload = {
            "text": "Hello, this is a test.",
            "model": "bulbul:v3",
            "target_language_code": "en-IN",
            "speaker": "priya",
            "pace": 0.95,
            "output_audio_codec": "wav"
        }
        headers = {
            "api-subscription-key": SARVAM_API_KEY,
            "Content-Type": "application/json"
        }
        async with http.post("https://api.sarvam.ai/text-to-speech", json=payload, headers=headers) as resp:
            data = await resp.json()
            audio_b64 = "".join(data.get("audios", []))
            wav_bytes = base64.b64decode(audio_b64)
            
            with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                print(f"Channels: {wf.getnchannels()}")
                print(f"Sample width: {wf.getsampwidth()}")
                print(f"Framerate: {wf.getframerate()}")
                print(f"Number of frames: {wf.getnframes()}")

asyncio.run(test_sarvam())
