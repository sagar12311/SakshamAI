import logging
import time
import sys
from faster_whisper import WhisperModel

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("debug_whisper")

print("--- Starting Whisper Debug ---")
start_time = time.time()

try:
    print("Initializing WhisperModel('base')...")
    # Toggling compute_type to float32 might help if int8 is broken on this specific setup
    model = WhisperModel("base", device="cpu", compute_type="int8") 
    print(f"Model initialized in {time.time() - start_time:.2f}s")
    
    print("Attempting dummy transcription...")
    # Generate dummy audio (silence)
    import numpy as np
    dummy_audio = np.zeros(16000, dtype=np.float32)
    
    segments, info = model.transcribe(dummy_audio, beam_size=5)
    print("Transcription output:")
    for segment in segments:
        print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text}")
        
    print("--- Success! ---")

except Exception as e:
    print(f"!!! CRASH !!!: {e}")
    import traceback
    traceback.print_exc()
