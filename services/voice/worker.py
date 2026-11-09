"""Private WSL adapter: PCM16 browser audio <-> PersonaPlex Opus + CPU ASR.

Run with --host 127.0.0.1. Gateway authenticates with VOICE_WORKER_KEY.
No audio files or transcript logs are written. Only one session is admitted.
"""
import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode
from uuid import uuid4
import wave
import anyio

import numpy as np
import sphn
from fastapi import FastAPI, Header, HTTPException, WebSocket
from faster_whisper import WhisperModel
from websockets.asyncio.client import connect


@asynccontextmanager
async def lifespan(app):
    key = os.environ.get('VOICE_WORKER_KEY', '')
    if len(key) < 32:
        raise RuntimeError('Set VOICE_WORKER_KEY to a random secret of at least 32 characters')
    app.state.key = key
    app.state.lock = asyncio.Lock()
    app.state.model = await asyncio.to_thread(WhisperModel, 'small.en', device='cpu', compute_type='int8', cpu_threads=2, num_workers=1)
    yield


app = FastAPI(lifespan=lifespan)


@app.get('/health')
async def health(authorization: str = Header('')):
    if not secrets.compare_digest(authorization, 'Bearer '+app.state.key):
        raise HTTPException(401,'Authentication required')
    return {'asr_ready': True, 'busy': app.state.lock.locked()}


def transcribe(model, pcm):
    wav = io.BytesIO()
    with wave.open(wav, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(pcm)
    wav.seek(0)
    segments, _ = model.transcribe(wav, language='en', beam_size=5,
                                  vad_filter=True, condition_on_previous_text=False)
    return ' '.join(s.text.strip() for s in segments)[:4000]


@app.websocket('/stream')
async def stream(ws: WebSocket):
    supplied = ws.headers.get('authorization', '')
    if not secrets.compare_digest(supplied, 'Bearer '+app.state.key):
        await ws.close(code=1008)
        return
    await ws.accept()
    if app.state.lock.locked():
        await ws.send_json({'type':'error','detail':'Voice server is busy'})
        await ws.close()
        return
    async with app.state.lock:
        tasks = []
        send_lock = asyncio.Lock()
        queue = asyncio.Queue(maxsize=3)
        pending_audio = bytearray()
        encode_pending = bytearray()
        assistant_text = ''
        last_text = time.monotonic()
        paused = asyncio.Event()

        async def send(event):
            async with send_lock:
                await ws.send_json(event)

        async def transcript(role, text):
            if text.strip():
                await send({'type':'transcript','event_id':str(uuid4()),'role':role,'text':text.strip()})

        async def asr():
            while True:
                pcm = await queue.get()
                try:
                    if pcm is None:
                        return
                    job = asyncio.create_task(asyncio.to_thread(transcribe, app.state.model, pcm))
                    try:
                        text = await asyncio.shield(job)
                    except asyncio.CancelledError:
                        # Don't admit a new session while previous CPU work is still running.
                        await job
                        raise
                    await transcript('user', text)
                finally:
                    queue.task_done()

        try:
            initial = await asyncio.wait_for(ws.receive_text(), 10)
            if len(initial)>18000:
                raise ValueError('Context too long')
            start = json.loads(initial)
            prompt = start.get('prompt', '')
            if start.get('type')!='start' or not isinstance(prompt,str) or len(prompt)>14000:
                raise ValueError('Invalid context')
            # Fixed local model destination; neither browser nor gateway can supply a URL.
            query = urlencode({'voice_prompt':'NATF2.pt', 'text_prompt':prompt})
            async with connect('ws://127.0.0.1:8998/api/chat?'+query, open_timeout=15, max_size=65536, max_queue=8, proxy=None) as model:
                handshake = asyncio.create_task(asyncio.wait_for(model.recv(), 180))
                disconnected = asyncio.create_task(ws.receive())
                try:
                    await asyncio.wait([handshake,disconnected], return_when=asyncio.FIRST_COMPLETED)
                    if disconnected.done():
                        raise ConnectionError('Client left or sent audio before readiness')
                    hello = handshake.result()
                finally:
                    handshake.cancel()
                    disconnected.cancel()
                    await asyncio.gather(handshake,disconnected,return_exceptions=True)
                if hello != b'\x00':
                    raise ValueError('Unexpected PersonaPlex handshake')
                encoder = sphn.OpusStreamWriter(24000)
                decoder = sphn.OpusStreamReader(24000)
                await send({'type':'ready'})

                async def input_audio():
                    started = time.monotonic()
                    total = 0
                    silence = 0
                    while True:
                        message = await ws.receive()
                        if message['type']=='websocket.disconnect':
                            raise ConnectionError('Browser disconnected')
                        pcm = message.get('bytes')
                        if pcm is not None:
                            if not pcm or len(pcm)>9600 or len(pcm)%2:
                                raise ValueError('Invalid PCM')
                            total += len(pcm)
                            if total > (time.monotonic()-started+3)*48000:
                                raise ValueError('Audio rate exceeded')
                            samples = np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768
                            encode_pending.extend(pcm)
                            while len(encode_pending)>=3840:
                                frame = np.frombuffer(bytes(encode_pending[:3840]),dtype='<i2').astype(np.float32)/32768
                                del encode_pending[:3840]
                                encoded = encoder.append_pcm(frame)
                                if encoded:
                                    await model.send(b'\x01'+encoded)
                            pending_audio.extend(pcm)
                            silence = silence+len(pcm) if np.max(np.abs(samples)) < 0.015 else 0
                            # Flush at a quiet boundary, or cap chunks at 12 seconds.
                            if len(pending_audio)>=576000 or (len(pending_audio)>=48000 and silence>=24000):
                                queue.put_nowait(bytes(pending_audio))
                                pending_audio.clear()
                                silence = 0
                        elif message.get('text') == '{"type":"pause"}':
                            if encode_pending:
                                padded = bytes(encode_pending).ljust(3840,b'\x00')
                                encoded = encoder.append_pcm(np.frombuffer(padded,dtype='<i2').astype(np.float32)/32768)
                                if encoded:
                                    await model.send(b'\x01'+encoded)
                                encode_pending.clear()
                            if pending_audio:
                                queue.put_nowait(bytes(pending_audio))
                                pending_audio.clear()
                            paused.set()
                            return
                        else:
                            raise ValueError('Unsupported control')

                async def output_audio():
                    nonlocal assistant_text, last_text
                    async for message in model:
                        if not isinstance(message,bytes) or not message:
                            raise ValueError('Invalid model frame')
                        if message[0]==1:
                            pcm = decoder.append_bytes(message[1:])
                            if len(pcm):
                                payload = (np.clip(pcm,-1,1)*32767).astype('<i2').tobytes()
                                # Bounded frames for the gateway/browser protocol.
                                async with send_lock:
                                    for offset in range(0,len(payload),9600):
                                        await ws.send_bytes(payload[offset:offset+9600])
                        elif message[0]==2:
                            assistant_text += message[1:].decode('utf-8')
                            last_text = time.monotonic()
                            if len(assistant_text)>=500:
                                text, assistant_text = assistant_text, ''
                                await transcript('assistant',text)
                    if not paused.is_set():
                        raise ConnectionError('PersonaPlex disconnected')

                async def flush_text():
                    nonlocal assistant_text
                    while True:
                        await asyncio.sleep(0.25)
                        if assistant_text and time.monotonic()-last_text>1:
                            text, assistant_text = assistant_text, ''
                            await transcript('assistant',text)

                receiver = asyncio.create_task(input_audio())
                processor = asyncio.create_task(asr())
                output = asyncio.create_task(output_audio())
                flusher = asyncio.create_task(flush_text())
                tasks = [receiver,processor,output,flusher]
                async with asyncio.timeout(660):
                    done, _ = await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if not paused.is_set():
                        raise RuntimeError('Session interrupted')
                    output.cancel()
                    flusher.cancel()
                    await asyncio.gather(output,flusher,return_exceptions=True)
                    await transcript('assistant', assistant_text)
                    await queue.put(None)
                    await asyncio.wait_for(processor, 60)
                    await send({'type':'paused'})
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logging.getLogger(__name__).error('Voice session failed (%s)', type(error).__name__)
            try:
                await send({'type':'error','detail':'Voice worker stopped. Some recent speech may be missing; reload history.'})
            except Exception:
                pass
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks,return_exceptions=True)
                pending_audio.clear()
                try:
                    await ws.close()
                except Exception:
                    pass
