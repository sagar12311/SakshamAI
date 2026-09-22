"""Account-owned conversation storage; provider keys never enter the database."""
import asyncio
from pathlib import Path
from uuid import UUID, uuid4
from typing import Literal

from fastapi import Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"\S")


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160, pattern=r"\S")
    pinned: bool | None = None
    project_id: UUID | None = None


class SavedMessage(BaseModel):
    request_id: UUID
    content: str = Field(min_length=1, max_length=12_000, pattern=r"\S")
    mode: Literal['hosted', 'byok'] = 'hosted'
    provider_url: HttpUrl | None = None
    model: str | None = Field(default=None, max_length=160)
    project_id: UUID | None = None
    retry: bool = False


async def initialize(pool):
    async with pool.acquire() as db, db.transaction():
        await db.execute(Path(__file__).with_name('history.sql').read_text())
        # This deployment runs one gateway process. Recover interrupted inference.
        await db.execute("UPDATE chat_turns SET status='failed' WHERE status='processing'")


def install(app, get_user, infer, ChatRequest, ChatMessage, select_provider):
    async def owned(db, conversation_id, user_id):
        row = await db.fetchrow('SELECT * FROM chat_conversations WHERE id=$1 AND user_id=$2', conversation_id, user_id)
        if not row:
            raise HTTPException(404, 'Conversation not found')
        return row

    async def project_owned(db, project_id, user_id):
        if project_id and not await db.fetchval('SELECT 1 FROM chat_projects WHERE id=$1 AND user_id=$2', project_id, user_id):
            raise HTTPException(404, 'Project not found')

    @app.get('/v1/projects')
    async def projects(user=Depends(get_user)):
        return [dict(r) for r in await app.state.pool.fetch('SELECT * FROM chat_projects WHERE user_id=$1 ORDER BY created_at,id', user.id)]

    @app.post('/v1/projects')
    async def create_project(body: ProjectInput, user=Depends(get_user)):
        return dict(await app.state.pool.fetchrow('INSERT INTO chat_projects(id,user_id,name) VALUES($1,$2,$3) RETURNING *', uuid4(), user.id, body.name.strip()))

    @app.patch('/v1/projects/{project_id}')
    async def rename_project(project_id: UUID, body: ProjectInput, user=Depends(get_user)):
        row = await app.state.pool.fetchrow('UPDATE chat_projects SET name=$3 WHERE id=$1 AND user_id=$2 RETURNING *', project_id,user.id,body.name.strip())
        if not row:
            raise HTTPException(404, 'Project not found')
        return dict(row)

    @app.delete('/v1/projects/{project_id}')
    async def delete_project(project_id: UUID, user=Depends(get_user)):
        row = await app.state.pool.fetchval('DELETE FROM chat_projects WHERE id=$1 AND user_id=$2 RETURNING id', project_id,user.id)
        if not row:
            raise HTTPException(404, 'Project not found')
        return {'deleted': True}

    @app.get('/v1/conversations')
    async def conversations(offset: int = Query(0,ge=0), limit: int = Query(50,ge=1,le=100), user=Depends(get_user)):
        rows = await app.state.pool.fetch('SELECT * FROM chat_conversations WHERE user_id=$1 ORDER BY updated_at DESC,id LIMIT $2 OFFSET $3', user.id,limit+1,offset)
        return {'items': [dict(r) for r in rows[:limit]], 'next_offset': offset+limit if len(rows)>limit else None}

    @app.get('/v1/conversations/{conversation_id}')
    async def conversation(conversation_id: UUID, before: int | None = Query(None,ge=1), limit: int = Query(50,ge=1,le=100), user=Depends(get_user)):
        async with app.state.pool.acquire() as db:
            row = await owned(db,conversation_id,user.id)
            turns = await db.fetch('SELECT * FROM chat_turns WHERE conversation_id=$1 AND ($2::bigint IS NULL OR id<$2) ORDER BY id DESC LIMIT $3',conversation_id,before,limit+1)
            count = await db.fetchval("SELECT count(*) FROM chat_turns WHERE conversation_id=$1 AND status='completed'",conversation_id)
        return {'conversation': dict(row), 'turns': [dict(t) for t in reversed(turns[:limit])], 'next_before': turns[limit-1]['id'] if len(turns)>limit else None, 'context_limited': count>14}

    @app.patch('/v1/conversations/{conversation_id}')
    async def update(conversation_id: UUID, body: ConversationPatch, user=Depends(get_user)):
        async with app.state.pool.acquire() as db, db.transaction():
            current = await owned(db,conversation_id,user.id)
            project_id = body.project_id if 'project_id' in body.model_fields_set else current['project_id']
            await project_owned(db,project_id,user.id)
            row = await db.fetchrow('UPDATE chat_conversations SET title=$3,pinned=$4,project_id=$5 WHERE id=$1 AND user_id=$2 RETURNING *',conversation_id,user.id,body.title.strip() if body.title else current['title'],body.pinned if body.pinned is not None else current['pinned'],project_id)
        return dict(row)

    @app.delete('/v1/conversations/{conversation_id}')
    async def delete(conversation_id: UUID, user=Depends(get_user)):
        row = await app.state.pool.fetchval('DELETE FROM chat_conversations WHERE id=$1 AND user_id=$2 RETURNING id',conversation_id,user.id)
        if not row:
            raise HTTPException(404,'Conversation not found')
        return {'deleted': True}

    @app.post('/v1/conversations/{conversation_id}/messages')
    async def send(conversation_id: UUID, body: SavedMessage, user=Depends(get_user), byok_key: str | None = Header(default=None,alias='X-Saksham-Provider-Key')):
        request = ChatRequest(messages=[ChatMessage(role='user',content=body.content)], mode=body.mode,provider_url=body.provider_url,model=body.model)
        select_provider(request,byok_key)
        async with app.state.pool.acquire() as db:
            # Session lock spans persistence and inference; duplicate sends do not race.
            lock_id = conversation_id.int % (2**63-1)
            if not await db.fetchval('SELECT pg_try_advisory_lock($1)',lock_id):
                raise HTTPException(409,'This conversation is generating a reply. Reload shortly.')
            try:
                async with db.transaction():
                    existing = await db.fetchrow('SELECT * FROM chat_conversations WHERE id=$1',conversation_id)
                    if existing:
                        await owned(db,conversation_id,user.id)
                    else:
                        await project_owned(db,body.project_id,user.id)
                        await db.execute('INSERT INTO chat_conversations(id,user_id,title,project_id) VALUES($1,$2,$3,$4)',conversation_id,user.id,' '.join(body.content.split())[:80],body.project_id)
                    previous = await db.fetchrow('SELECT * FROM chat_turns WHERE conversation_id=$1 AND request_id=$2',conversation_id,body.request_id)
                    if previous:
                        if previous['user_content'] != body.content:
                            raise HTTPException(409,'Request identifier already belongs to another message')
                        if previous['status']=='completed' or not body.retry:
                            return {'turn':dict(previous)}
                        latest = await db.fetchval('SELECT max(id) FROM chat_turns WHERE conversation_id=$1',conversation_id)
                        if latest != previous['id']:
                            raise HTTPException(409,'Only the latest failed message can be retried')
                        turn = await db.fetchrow("UPDATE chat_turns SET status='processing' WHERE id=$1 RETURNING *",previous['id'])
                    else:
                        turn = await db.fetchrow("INSERT INTO chat_turns(conversation_id,request_id,user_content,status) VALUES($1,$2,$3,'processing') RETURNING *",conversation_id,body.request_id,body.content)
                    await db.execute('UPDATE chat_conversations SET mode=$2,provider_url=$3,model=$4,updated_at=now() WHERE id=$1',conversation_id,body.mode,str(body.provider_url) if body.mode=='byok' and body.provider_url else None,body.model)
                try:
                    completed = await db.fetch("SELECT user_content,assistant_content FROM chat_turns WHERE conversation_id=$1 AND status='completed' AND id<$2 ORDER BY id DESC LIMIT 14",conversation_id,turn['id'])
                    request.messages = [ChatMessage(role=role,content=t[field]) for t in reversed(completed) for role,field in [('user','user_content'),('assistant','assistant_content')]] + request.messages
                    result = await infer(request,user,byok_key)
                    content = result.get('choices',[{}])[0].get('message',{}).get('content')
                    if not isinstance(content,str) or not content.strip():
                        raise HTTPException(502,'The provider returned no response')
                    async with db.transaction():
                        saved = await db.fetchrow("UPDATE chat_turns SET assistant_content=$2,status='completed' WHERE id=$1 RETURNING *",turn['id'],content)
                        await db.execute('UPDATE chat_conversations SET updated_at=now() WHERE id=$1',conversation_id)
                    if not saved:
                        raise HTTPException(404,'Conversation was deleted')
                    return {'turn':dict(saved)}
                except (Exception, asyncio.CancelledError):
                    await db.execute("UPDATE chat_turns SET status='failed' WHERE id=$1",turn['id'])
                    raise
            finally:
                await db.execute('SELECT pg_advisory_unlock($1)',lock_id)
