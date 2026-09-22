CREATE TABLE IF NOT EXISTS chat_projects (
 id UUID PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_conversations (
 id UUID PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
 project_id UUID REFERENCES chat_projects(id) ON DELETE SET NULL,
 pinned BOOLEAN NOT NULL DEFAULT false, mode TEXT NOT NULL DEFAULT 'hosted',
 provider_url TEXT, model TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_conversations_owner ON chat_conversations(user_id,updated_at DESC,id);
CREATE TABLE IF NOT EXISTS chat_turns (
 id BIGSERIAL PRIMARY KEY, conversation_id UUID NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
 request_id UUID NOT NULL, user_content TEXT NOT NULL, assistant_content TEXT,
 status TEXT NOT NULL CHECK(status IN ('processing','completed','failed')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(conversation_id,request_id)
);
CREATE INDEX IF NOT EXISTS chat_turns_conversation ON chat_turns(conversation_id,id);
