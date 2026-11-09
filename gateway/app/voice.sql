CREATE TABLE IF NOT EXISTS voice_events (
 id BIGSERIAL PRIMARY KEY,
 conversation_id UUID NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
 event_id UUID NOT NULL,
 role TEXT NOT NULL CHECK (role IN ('user','assistant')),
 content TEXT NOT NULL,
 remembered BOOLEAN NOT NULL DEFAULT false,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 UNIQUE(conversation_id,event_id)
);
CREATE INDEX IF NOT EXISTS voice_event_history ON voice_events(conversation_id,id);
ALTER TABLE voice_events ADD COLUMN IF NOT EXISTS remembered BOOLEAN NOT NULL DEFAULT false;
