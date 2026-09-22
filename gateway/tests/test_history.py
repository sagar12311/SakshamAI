"""Real PostgreSQL integration checks in a disposable schema, never user tables."""
import asyncio
import os
from uuid import UUID, uuid4
from types import SimpleNamespace

import asyncpg
import httpx
from fastapi import FastAPI, Header, HTTPException

from app import history


async def check_history(url):
    from app.main import ChatRequest, ChatMessage
    schema = 'history_test_' + uuid4().hex
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA {schema}')
    pool = None
    try:
        pool = await asyncpg.create_pool(url, min_size=1, max_size=5, server_settings={'search_path': schema})
        await history.initialize(pool)
        app = FastAPI()
        app.state.pool = pool
        calls = []
        started, release = asyncio.Event(), asyncio.Event()

        async def user(x_test_user: str = Header('alice')):
            return SimpleNamespace(id=x_test_user)

        async def infer(request, user, key):
            calls.append(request)
            if request.messages[-1].content == 'fail':
                raise HTTPException(503,'Model offline')
            if request.messages[-1].content == 'slow':
                started.set()
                await release.wait()
            return {'choices':[{'message':{'content':'Saved answer'}}]}

        history.install(app,user,infer,ChatRequest,ChatMessage,lambda *_: None)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            cid = str(uuid4())
            project = (await c.post('/v1/projects',json={'name':'Work'})).json()['id']
            body = {'request_id':str(uuid4()),'content':'Hello','project_id':project}
            path = f'/v1/conversations/{cid}'
            assert (await c.post(path+'/messages',json=body)).status_code == 200
            assert (await c.post(path+'/messages',json=body)).status_code == 200
            assert len(calls)==1, 'Duplicate request performed inference twice'
            body['content']='different'
            assert (await c.post(path+'/messages',json=body)).status_code==409
            bob = {'x-test-user':'bob'}
            assert (await c.get('/v1/conversations',headers=bob)).json()['items']==[]
            assert (await c.get('/v1/projects',headers=bob)).json()==[]
            for method, suffix, data in [('GET','',None),('PATCH','',{'title':'Stolen'}),('DELETE','',None),('POST','/messages',{'request_id':str(uuid4()),'content':'hello'})]:
                assert (await c.request(method,path+suffix,headers=bob,json=data)).status_code==404
            assert (await c.patch('/v1/projects/'+project,headers=bob,json={'name':'Stolen'})).status_code==404
            assert (await c.delete('/v1/projects/'+project,headers=bob)).status_code==404
            bob_project = (await c.post('/v1/projects',headers=bob,json={'name':'Private'})).json()['id']
            assert (await c.patch(path,json={'project_id':bob_project})).status_code==404
            assert (await c.post('/v1/conversations/'+str(uuid4())+'/messages',json={'request_id':str(uuid4()),'content':'hello','project_id':bob_project})).status_code==404
            updated = (await c.patch(path,json={'pinned':True,'title':'Planning'})).json()
            assert updated['pinned'] and updated['title']=='Planning'
            assert (await c.delete('/v1/projects/'+project)).status_code==200
            assert (await c.get(path)).json()['conversation']['project_id'] is None
            for n in range(17):
                r = await c.post(path+'/messages',json={'request_id':str(uuid4()),'content':f'Turn {n}'})
                assert r.status_code==200
            assert len(calls[-1].messages)==29
            assert calls[-1].messages[0].content=='Turn 2'
            page = (await c.get(path+'?limit=3')).json()
            assert page['context_limited'] and len(page['turns'])==3
            older = (await c.get(path+f"?limit=3&before={page['next_before']}")).json()
            assert older['turns'][-1]['id']<page['turns'][0]['id']
            failed = {'request_id':str(uuid4()),'content':'fail'}
            assert (await c.post(path+'/messages',json=failed)).status_code==503
            assert (await c.get(path)).json()['turns'][-1]['status']=='failed'
            assert (await c.post(path+'/messages',json={**failed,'retry':True})).status_code==503
            assert sum(t['request_id']==failed['request_id'] for t in (await c.get(path)).json()['turns'])==1
            slow = {'request_id':str(uuid4()),'content':'slow'}
            task = asyncio.create_task(c.post(path+'/messages',json=slow))
            await asyncio.wait_for(started.wait(),5)
            assert (await c.post(path+'/messages',json=slow)).status_code==409
            release.set()
            assert (await task).status_code==200
            assert (await c.post(path+'/messages',json={**failed,'retry':True})).status_code==409
            await c.post('/v1/conversations/'+str(uuid4())+'/messages',json={'request_id':str(uuid4()),'content':'another'})
            assert (await c.get('/v1/conversations?limit=1')).json()['next_offset']==1
            await c.delete(path)
            assert (await c.get(path)).status_code==404
            assert await pool.fetchval('SELECT count(*) FROM chat_turns WHERE conversation_id=$1',UUID(cid))==0
        print('History integration passed: ownership, persistence, retry, concurrency, pagination, context and deletion.')
    finally:
        if pool:
            await pool.close()
        await admin.execute(f'DROP SCHEMA {schema} CASCADE')
        await admin.close()


def test_history_database():
    import pytest
    url = os.environ.get('HISTORY_TEST_DATABASE_URL')
    if not url:
        pytest.skip('Set HISTORY_TEST_DATABASE_URL to run PostgreSQL integration tests')
    asyncio.run(check_history(url))


if __name__ == '__main__':
    asyncio.run(check_history(os.environ['DATABASE_URL']))
