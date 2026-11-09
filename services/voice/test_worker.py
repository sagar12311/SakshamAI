import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pytest
import sphn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from faster_whisper.audio import decode_audio

import worker


@pytest.fixture
def client(monkeypatch):
    class ASR:
        def transcribe(self, audio, **kwargs):
            decoded = decode_audio(audio)
            assert len(decoded)>0
            assert kwargs['beam_size']==5 and kwargs['language']=='en'
            return iter([SimpleNamespace(text='My favorite snack is roasted makhana.')]), None

    class Model:
        def __init__(self):
            self.received = asyncio.Queue()
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            pass
        async def recv(self):
            return b'\x00'
        async def send(self,payload):
            assert payload[0]==1
            await self.received.put(payload[1:])
        async def __aiter__(self):
            yield b'\x02Hello there.'
            decoder = sphn.OpusStreamReader(24000)
            encoder = sphn.OpusStreamWriter(24000)
            while True:
                pcm = decoder.append_bytes(await self.received.get())
                if len(pcm):
                    encoded = encoder.append_pcm(pcm)
                    if encoded:
                        yield b'\x01'+encoded

    monkeypatch.setenv('VOICE_WORKER_KEY','x'*32)
    monkeypatch.setattr(worker,'WhisperModel',lambda *a,**k:ASR())
    monkeypatch.setattr(worker,'connect',lambda *a,**k:Model())
    with TestClient(worker.app) as c:
        yield c


def test_pcm_opus_roundtrip_and_flush(client):
    with client.websocket_connect('/stream',headers={'Authorization':'Bearer '+'x'*32}) as ws:
        ws.send_json({'type':'start','prompt':'You are Saksham.'})
        assert ws.receive_json()['type']=='ready'
        pcm = (np.sin(np.arange(1920)*0.1)*8000).astype('<i2').tobytes()
        for _ in range(20):
            ws.send_bytes(pcm)
        packet = ws.receive()
        assert packet.get('bytes') and len(packet['bytes'])%2==0
        ws.send_text('{"type":"pause"}')
        transcripts = []
        while True:
            packet = ws.receive()
            if 'text' not in packet:
                continue
            data = json.loads(packet['text'])
            assert data['type']!='error'
            if data['type']=='paused':
                break
            if data['type']=='transcript':
                transcripts.append(data)
        assert any(e['role']=='user' and 'makhana' in e['text'] for e in transcripts)
        assert any(e['role']=='assistant' and 'Hello' in e['text'] for e in transcripts)


def test_private_worker_requires_key(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/stream'):
            pass


def test_invalid_audio_releases_worker(client):
    with client.websocket_connect('/stream',headers={'Authorization':'Bearer '+'x'*32}) as ws:
        ws.send_json({'type':'start','prompt':'Hello'})
        assert ws.receive_json()['type']=='ready'
        ws.send_bytes(b'odd')
        assert ws.receive_json()['type']=='error'
    with client.websocket_connect('/stream',headers={'Authorization':'Bearer '+'x'*32}) as ws:
        ws.send_json({'type':'start','prompt':'Hello'})
        assert ws.receive_json()['type']=='ready'
        ws.send_text('{"type":"pause"}')
        while ws.receive_json()['type']!='paused':
            pass
