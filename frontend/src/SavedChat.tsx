import { FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from 'react';
import type { Session } from '@supabase/supabase-js';
import { Menu, Plus, Pin, Folder, MessageSquare, X } from 'lucide-react';
import './SavedChat.css';

type Conversation = { id: string; title: string; project_id: string | null; pinned: boolean; mode: 'hosted' | 'byok'; provider_url: string | null; model: string | null };
type Project = { id: string; name: string };
type Turn = { id: number; request_id: string; user_content: string; assistant_content: string | null; status: 'processing' | 'completed' | 'failed' };
type Detail = { conversation: Conversation; turns: Turn[]; next_before: number | null; context_limited: boolean };
type Pending = { conversation: string; request: string; text: string };

export default function SavedChat({ session, gateway, workspace = 'chat', onWorkspaceChange, children }: { session: Session; gateway: string; workspace?: 'chat' | 'meetings'; onWorkspaceChange?: (workspace: 'chat' | 'meetings') => void; children?: ReactNode }) {
    const [conversations, setConversations] = useState<Conversation[]>([]);
    const [projects, setProjects] = useState<Project[]>([]);
    const [active, setActive] = useState<string | null>(null);
    const [detail, setDetail] = useState<Detail | null>(null);
    const [project, setProject] = useState('');
    const [input, setInput] = useState('');
    const [mode, setMode] = useState<'hosted' | 'byok'>('hosted');
    const [provider, setProvider] = useState('https://api.openai.com/v1');
    const [model, setModel] = useState('');
    const [key, setKey] = useState('');
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [loading, setLoading] = useState(false);
    const [sidebar, setSidebar] = useState(false);
    const [collapsed, setCollapsed] = useState(false);
    const [pending, setPending] = useState<Pending | null>(null);
    const [dialog, setDialog] = useState<{ kind: 'project-create' | 'project-rename' | 'chat-rename' | 'project-delete' | 'chat-delete'; id?: string; value: string } | null>(null);
    const dialogRef = useRef<HTMLDialogElement>(null);
    const current = useRef<string | null>(null);
    const alive = useRef(true);
    const end = useRef<HTMLDivElement>(null);
    const token = useRef(session.access_token);
    token.current = session.access_token;
    useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

    const api = useCallback(async (path: string, method = 'GET', body?: unknown, providerKey?: string) => {
        let response: Response;
        try { response = await fetch(`${gateway}${path}`, { method, headers: { Authorization: `Bearer ${token.current}`, 'Content-Type': 'application/json', ...(providerKey ? { 'X-Saksham-Provider-Key': providerKey } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) }); }
        catch { throw new Error('Connection lost. Reload history or retry the same message when connected.'); }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not save or load this conversation. Please try again.');
        return data;
    }, [gateway]);

    const refresh = useCallback(async () => {
        const all: Conversation[] = [];
        let offset: number | null = 0;
        do {
            const page = await api(`/v1/conversations?offset=${offset}`);
            all.push(...page.items); offset = page.next_offset;
        } while (offset !== null && alive.current);
        const list = await api('/v1/projects');
        if (alive.current) { setConversations(all); setProjects(list); }
    }, [api]);

    const load = useCallback(async (id: string, older?: number) => {
        const data: Detail = await api(`/v1/conversations/${id}${older ? `?before=${older}` : ''}`);
        if (!alive.current || current.current !== id) return;
        setDetail(previous => older && previous ? { ...data, turns: [...data.turns, ...previous.turns] } : data);
        if (!older) { setMode(data.conversation.mode); setProvider(data.conversation.provider_url || 'https://api.openai.com/v1'); setModel(data.conversation.model || ''); setProject(data.conversation.project_id || ''); }
    }, [api]);

    const open = useCallback(async (id: string) => {
        onWorkspaceChange?.('chat');
        current.current = id; setActive(id); setDetail(null); setKey(''); setInput(''); setError(''); setLoading(true); setSidebar(false);
        sessionStorage.setItem(`saksham.active.${session.user.id}`, id);
        try { await load(id); } catch (e) { if (current.current === id) setError((e as Error).message); }
        finally { if (alive.current && current.current === id) setLoading(false); }
    }, [load, session.user.id, onWorkspaceChange]);

    useEffect(() => {
        void refresh().catch(e => alive.current && setError(e.message));
        const saved = sessionStorage.getItem(`saksham.active.${session.user.id}`);
        if (saved) void open(saved);
    }, [refresh, open, session.user.id]);
    useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }); }, [detail?.turns.length, busy]);
    useEffect(() => { if (dialog && !dialogRef.current?.open) dialogRef.current?.showModal(); }, [dialog]);
    useEffect(() => {
        if (!active || !detail?.turns.some(t => t.status === 'processing') || busy) return;
        const timer = window.setInterval(() => { void load(active).catch(e => setError(e.message)); }, 3000);
        return () => window.clearInterval(timer);
    }, [active, detail, busy, load]);

    const newChat = (projectId = '') => {
        onWorkspaceChange?.('chat');
        current.current = null; setActive(null); setDetail(null); setInput(''); setKey(''); setMode('hosted'); setModel(''); setProvider('https://api.openai.com/v1'); setProject(projectId); setError(''); setSidebar(false);
        sessionStorage.removeItem(`saksham.active.${session.user.id}`);
    };

    const send = async (retry?: Pending) => {
        const text = retry?.text || input.trim();
        if (!text || busy) return;
        if (mode === 'byok' && !key) { setError('Enter your provider API key for this session.'); return; }
        const job = retry || { conversation: active || crypto.randomUUID(), request: crypto.randomUUID(), text };
        current.current = job.conversation; setActive(job.conversation); setPending(job); setBusy(true); setError('');
        sessionStorage.setItem(`saksham.active.${session.user.id}`, job.conversation);
        try {
            await api(`/v1/conversations/${job.conversation}/messages`, 'POST', { request_id: job.request, content: job.text, mode, provider_url: mode === 'byok' ? provider : null, model: model || null, project_id: project || null, retry: Boolean(retry) }, key);
            if (!alive.current) return;
            setPending(null); setInput('');
            await refresh();
            if (current.current === job.conversation) await load(job.conversation);
        } catch (e) {
            if (!alive.current) return;
            setError((e as Error).message);
            await refresh().catch(() => undefined);
            if (current.current === job.conversation) await load(job.conversation).catch(() => undefined);
        } finally { if (alive.current) setBusy(false); }
    };

    const patchChat = async (body: object) => {
        if (!active) return;
        try { await api(`/v1/conversations/${active}`, 'PATCH', body); await refresh(); await load(active); }
        catch (e) { setError((e as Error).message); }
    };

    const submitDialog = async (event: FormEvent) => {
        event.preventDefault(); if (!dialog) return;
        try {
            if (dialog.kind === 'project-create') await api('/v1/projects', 'POST', { name: dialog.value.trim() });
            if (dialog.kind === 'project-rename') await api(`/v1/projects/${dialog.id}`, 'PATCH', { name: dialog.value.trim() });
            if (dialog.kind === 'chat-rename') await api(`/v1/conversations/${dialog.id}`, 'PATCH', { title: dialog.value.trim() });
            if (dialog.kind === 'project-delete') { await api(`/v1/projects/${dialog.id}`, 'DELETE'); if (project === dialog.id) setProject(''); }
            if (dialog.kind === 'chat-delete') { await api(`/v1/conversations/${dialog.id}`, 'DELETE'); if (active === dialog.id) newChat(); if (pending?.conversation === dialog.id) setPending(null); }
            setDialog(null); await refresh(); if (current.current) await load(current.current);
        } catch (e) { setDialog(null); setError((e as Error).message); }
    };
    const row = (chat: Conversation) => <button key={chat.id} className={`saved-chat-row ${active === chat.id ? 'selected' : ''}`} onClick={() => void open(chat.id)} disabled={busy || Boolean(pending)} title={chat.title}><MessageSquare size={14} /><span>{chat.title}</span></button>;
    const last = detail?.turns[detail.turns.length - 1];
    const generating = busy || last?.status === 'processing';

    return <section className={`saved-workspace ${collapsed ? 'sidebar-collapsed' : ''}`}>
        <button className="sidebar-toggle" aria-label="Toggle chat sidebar" aria-expanded={sidebar || !collapsed} onClick={() => { setSidebar(!sidebar); setCollapsed(!collapsed); }}><Menu size={18} /></button>
        {sidebar && <button className="sidebar-scrim" aria-label="Close chat sidebar" onClick={() => setSidebar(false)} />}
        <aside className={`chat-sidebar ${sidebar ? 'mobile-open' : ''}`} aria-label="Chat navigation">
            <div className="sidebar-top"><strong>Workspace</strong><button className="mobile-close" aria-label="Close chat sidebar" onClick={() => setSidebar(false)}><X size={16} /></button></div>
            <nav className="workspace-navigation" aria-label="Public beta workspaces"><button aria-current={workspace === 'chat' ? 'page' : undefined} onClick={() => { onWorkspaceChange?.('chat'); setSidebar(false); }}>Chat</button><button aria-current={workspace === 'meetings' ? 'page' : undefined} onClick={() => { onWorkspaceChange?.('meetings'); setSidebar(false); }}>Meeting Intelligence</button></nav>
            <button className="new-chat" disabled={busy || Boolean(pending)} onClick={() => newChat()}><Plus size={16} />New chat</button>
            <h3><Pin size={13} />Pinned</h3>{conversations.filter(c => c.pinned).map(row)}{!conversations.some(c => c.pinned) && <p className="sidebar-empty">Pin a conversation to keep it here.</p>}
            <div className="sidebar-section-heading"><h3><Folder size={13} />Projects</h3><button aria-label="Create project" onClick={() => setDialog({ kind: 'project-create', value: '' })}><Plus size={14} /></button></div>
            {projects.map(p => <details key={p.id} className="chat-project"><summary>{p.name}</summary><div className="project-actions"><button disabled={busy || Boolean(pending)} onClick={() => newChat(p.id)}>New chat</button><button onClick={() => setDialog({ kind: 'project-rename', id: p.id, value: p.name })}>Rename</button><button onClick={() => setDialog({ kind: 'project-delete', id: p.id, value: p.name })}>Delete</button></div>{conversations.filter(c => c.project_id === p.id).map(row)}</details>)}
            {!projects.length && <p className="sidebar-empty">Group related conversations.</p>}
            <h3>Recents</h3>{conversations.map(row)}{!conversations.length && <p className="sidebar-empty">Your saved chats will appear here.</p>}
            <button className="history-refresh" onClick={() => { void refresh().then(() => active ? load(active) : undefined).catch(e => setError(e.message)); }}>Reload history</button>
        </aside>
        <section className="saved-chat-main" aria-label="Conversation" hidden={workspace !== 'chat'}>
            <header className="saved-chat-heading"><h2>{detail?.conversation.title || 'New conversation'}</h2>{detail && <div className="conversation-actions"><button disabled={generating} onClick={() => void patchChat({ pinned: !detail.conversation.pinned })}>{detail.conversation.pinned ? 'Unpin' : 'Pin'}</button><button disabled={generating} onClick={() => setDialog({ kind: 'chat-rename', id: active!, value: detail.conversation.title })}>Rename</button><button disabled={generating} onClick={() => setDialog({ kind: 'chat-delete', id: active!, value: detail.conversation.title })}>Delete</button></div>}</header>
            <div className="saved-provider-bar"><label>Provider<select aria-label="Chat provider" disabled={generating || loading} value={mode} onChange={e => setMode(e.target.value as 'hosted' | 'byok')}><option value="hosted">Saksham Hosted</option><option value="byok">Use my API key</option></select></label></div>
            {mode === 'byok' && <div className="byok-fields"><label>Provider URL<input value={provider} onChange={e => setProvider(e.target.value)} disabled={generating} /></label><label>API key · session only<input type="password" autoComplete="off" value={key} onChange={e => setKey(e.target.value)} disabled={generating} /></label><label>Model<input value={model} onChange={e => setModel(e.target.value)} disabled={generating} /></label></div>}
            <p className="saved-privacy">Chats are saved to your account until you delete them. Provider API keys are never saved.</p>
            {detail?.context_limited && <p className="context-note">Only the latest 14 completed exchanges are included in new replies. Your full history remains saved.</p>}
            <div className="saved-messages" aria-live="polite">
                {loading ? <p className="muted">Loading conversation…</p> : <>
                    {detail?.next_before && <button className="older-messages" onClick={() => void load(active!, detail.next_before!).catch(e => setError(e.message))}>Load earlier messages</button>}
                    {!detail?.turns.length && !busy && <div className="empty-chat"><img className="brand-mark" src="/saksham-mark.svg" alt="" /><div><h3>What are you working on?</h3><p className="muted">Start a conversation, then pick it up here anytime.</p></div></div>}
                    {detail?.turns.map(t => <div className="saved-turn" key={t.id}><article className="saved-user"><strong>You</strong><p>{t.user_content}</p></article>{t.assistant_content && <article><strong>Saksham</strong><p>{t.assistant_content}</p></article>}{t.status === 'failed' && <p className="failed-turn">Reply failed. Your message is saved.{last?.id === t.id && !pending && <button disabled={busy} onClick={() => void send({ conversation: active!, request: t.request_id, text: t.user_content })}>Retry reply</button>}</p>}{t.status === 'processing' && <p className="muted">Generating reply…</p>}</div>)}
                    {busy && <p className="muted" role="status">Saving your message and generating a reply…</p>}<div ref={end} />
                </>}
            </div>
            {error && <div className="notice" role="alert">{error}</div>}
            {pending && !busy && <div className="pending-message"><p>Delivery needs checking: “{pending.text.slice(0,100)}”</p><button onClick={() => void send(pending)}>Retry same message</button><button onClick={() => { setPending(null); setError(''); }}>Keep draft</button></div>}
            <form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}><input aria-label="Message" maxLength={12000} value={input} onChange={e => setInput(e.target.value)} placeholder="Message Saksham…" disabled={generating || loading || Boolean(pending)} /><button disabled={generating || loading || Boolean(pending) || !input.trim()}>Send</button></form>
        </section>
        {workspace === 'meetings' && <div className="saved-meeting-main">{children}</div>}
        {dialog && <dialog ref={dialogRef} className="chat-dialog" onCancel={() => setDialog(null)}><form onSubmit={submitDialog}><h2>{dialog.kind.endsWith('delete') ? 'Confirm deletion' : dialog.kind === 'project-create' ? 'Create project' : 'Rename'}</h2>{dialog.kind.endsWith('delete') ? <p>{dialog.kind === 'project-delete' ? `Delete “${dialog.value}”? Its chats will be kept outside the project.` : `Permanently delete “${dialog.value}” and its messages?`}</p> : <label>Name<input autoFocus required maxLength={dialog.kind === 'chat-rename' ? 160 : 100} value={dialog.value} onChange={e => setDialog({ ...dialog, value: e.target.value })} /></label>}<div><button type="button" onClick={() => setDialog(null)}>Cancel</button><button disabled={!dialog.value.trim()}>{dialog.kind.endsWith('delete') ? 'Delete' : 'Save'}</button></div></form></dialog>}
    </section>;
}
