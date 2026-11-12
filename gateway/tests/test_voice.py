"""Real PostgreSQL + simulated private voice worker; never uses the GPU."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app import history, voice


@pytest.fixture
def rig(monkeypatch):
    url = os.environ.get('VOICE_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set VOICE_TEST_DATABASE_URL to a disposable PostgreSQL instance')
    config = SimpleNamespace(voice_enabled=True, cors_origins=['https://test.local'], voice_worker_key='x'*32,
                             voice_worker_url='ws://worker.invalid/stream', voice_max_session_seconds=10)
    schema = 'voice_test_'+uuid4().hex
    calls = []

    @asynccontextmanager
    async def lifespan(app):
        admin = await asyncpg.connect(url)
        await admin.execute(f'CREATE SCHEMA {schema}')
        pool = await asyncpg.create_pool(url, min_size=1, max_size=5, server_settings={'search_path':schema})
        try:
            await history.initialize(pool)
            await voice.initialize(pool)
            await voice.initialize(pool)
            app.state.pool = pool
            app.state.hosted_gpu_slots = asyncio.BoundedSemaphore(1)
            app.state.voice_slots = asyncio.BoundedSemaphore(1)
            yield
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA {schema} CASCADE')
            await admin.close()

    async def user(authorization: str = Header('')):
        if authorization not in ('Bearer alice','Bearer bob'):
            raise HTTPException(401,'Authentication required')
        return SimpleNamespace(id=authorization.split()[1])

    class Worker:
        def __init__(self):
            self.pause = asyncio.Event()
            self.messages = []
        async def __aenter__(self):
            calls.append(self)
            return self
        async def __aexit__(self,*args):
            pass
        async def send(self,message):
            self.messages.append(message)
            if message == '{"type":"pause"}':
                self.pause.set()
        async def __aiter__(self):
            yield json.dumps({'type':'ready'})
            yield b'\x00\x00'*1920
            await self.pause.wait()
            for role,text in [('user','My favorite snack is roasted makhana.'),('assistant','Your snack is strawberry mash.')]:
                yield json.dumps({'type':'transcript','event_id':str(uuid4()),'role':role,'text':text})
            yield json.dumps({'type':'paused'})

    monkeypatch.setattr(voice,'connect',lambda *args,**kwargs: Worker())
    app = FastAPI(lifespan=lifespan)
    from app.main import ChatMessage, ChatRequest
    async def infer(request, caller, key):
        assert caller.id == 'alice' and request.mode == 'hosted'
        return {'choices':[{'message':{'content':'Confirmed preference: roasted makhana.'}}]}
    voice.install(app,user,lambda:config,infer,ChatRequest,ChatMessage)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer alice'
        cid = client.post('/v1/voice/conversations').json()['id']
        yield client,cid,calls,config


def talk(client,cid):
    with client.websocket_connect(f'/v1/voice/conversations/{cid}/live',headers={'Origin':'https://test.local'}) as ws:
        ws.send_json({'type':'auth','token':'alice','consent':True})
        assert ws.receive_json()['type']=='ready'
        assert len(ws.receive_bytes())==3840
        ws.send_bytes(b'\x00\x00'*1920)
        ws.send_text('{"type":"pause"}')
        user = ws.receive_json()['event']
        assistant = ws.receive_json()['event']
        assert ws.receive_json()['type']=='paused'
        return user,assistant


def test_stream_save_memory_resume(rig):
    client,cid,calls,_ = rig
    user,assistant = talk(client,cid)
    path = f'/v1/voice/conversations/{cid}/events'
    events = client.get(path).json()['items']
    assert len(events)==2
    assert client.patch(path+f"/{assistant['id']}",json={'remembered':True}).status_code==404
    assert client.patch(path+f"/{user['id']}",json={'remembered':True}).status_code==200
    talk(client,cid)
    prompt = json.loads(calls[-1].messages[0])['prompt']
    assert 'roasted makhana' in prompt and '"confirmed": true' in prompt
    assert b'\x00\x00'*1920 in calls[0].messages
    assert client.patch(path+f"/{user['id']}",json={'remembered':False}).status_code==200


def test_access_and_disabled(rig):
    client,cid,calls,config = rig
    path = f'/v1/voice/conversations/{cid}'
    assert client.get(path+'/events',headers={'Authorization':'Bearer bob'}).status_code==404
    assert client.get('/v1/voice/conversations',headers={'Authorization':'Bearer bob'}).json()==[]
    assert client.get(path+'/events',headers={'Authorization':''}).status_code==401
    for token,consent in [('bob',True),('invalid',True),('alice',False)]:
        with client.websocket_connect(path+'/live',headers={'Origin':'https://test.local'}) as ws:
            ws.send_json({'type':'auth','token':token,'consent':consent})
            assert ws.receive_json()['type']=='error'
    assert not calls
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path+'/live',headers={'Origin':'https://evil.invalid'}):
            pass
    config.voice_enabled=False
    assert client.get('/v1/voice/conversations').status_code==503


def test_busy_and_disconnect_release_slot(rig):
    client,cid,calls,_ = rig
    path = f'/v1/voice/conversations/{cid}/live'
    with client.websocket_connect(path,headers={'Origin':'https://test.local'}) as first:
        first.send_json({'type':'auth','token':'alice','consent':True})
        assert first.receive_json()['type']=='ready'
        with client.websocket_connect(path,headers={'Origin':'https://test.local'}) as second:
            second.send_json({'type':'auth','token':'alice','consent':True})
            assert 'busy' in second.receive_json()['detail']
        assert len(calls)==1
    talk(client,cid)
    assert len(calls)==2


def test_summary_review_is_owned_and_in_context_only_after_approval(rig):
    client,cid,calls,_ = rig
    user,_ = talk(client,cid)
    base = f'/v1/voice/conversations/{cid}'
    assert client.post(base+'/summary/draft',headers={'Authorization':'Bearer bob'}).status_code==404
    draft = client.post(base+'/summary/draft').json()
    assert draft['draft'] and draft['approved'] is None
    assert client.post(base+'/summary/approve',headers={'Authorization':'Bearer bob'},json={'draft':draft['draft']}).status_code==404
    assert client.post(base+'/summary/approve',json={'draft':'outdated'}).status_code==409
    assert client.post(base+'/summary/approve',json={'draft':draft['draft']}).json()['approved']==draft['draft']
    talk(client,cid)
    assert 'Confirmed preference: roasted makhana.' in json.loads(calls[-1].messages[0])['prompt']
    assert client.patch(base+f"/events/{user['id']}",json={'remembered':True}).status_code==200
    assert client.get(base+'/summary').json()=={'draft':None,'approved':None}
    assert client.post(base+'/summary/approve',json={'draft':draft['draft']}).status_code==409
    assert client.delete(base+'/summary',headers={'Authorization':'Bearer bob'}).status_code==404
