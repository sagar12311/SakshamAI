import { FormEvent, useEffect, useMemo, useState } from 'react';
import { createClient, type Session } from '@supabase/supabase-js';
import './PublicApp.css';

type Message = { role: 'user' | 'assistant'; content: string };
type Mode = 'hosted' | 'byok';
type Workspace = 'chat' | 'meetings';
type TranscriptSegment = {
    start_ms?: number;
    end_ms?: number;
    start?: number;
    end?: number;
    text?: string;
};
type TranscriptionResponse = {
    mode?: Mode;
    text?: string;
    segments?: TranscriptSegment[];
    language?: string | null;
    model?: string | null;
};

const maxAudioBytes = 25 * 1024 * 1024;
const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;
const gatewayUrl = (import.meta.env.VITE_SAKSHAM_GATEWAY_URL as string | undefined)?.replace(/\/$/, '');
const supabase = supabaseUrl && supabaseAnonKey ? createClient(supabaseUrl, supabaseAnonKey) : null;

function formatTime(milliseconds: number) {
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function normalizedSegments(result: TranscriptionResponse | null) {
    return (result?.segments ?? []).map((segment, index) => ({
        id: `${index}-${segment.text ?? ''}`,
        start: typeof segment.start_ms === 'number' ? segment.start_ms : Math.round((segment.start ?? 0) * 1000),
        end: typeof segment.end_ms === 'number' ? segment.end_ms : Math.round((segment.end ?? 0) * 1000),
        text: segment.text?.trim() ?? '',
    })).filter(segment => segment.text);
}

function AuthScreen({ onSession }: { onSession: (session: Session) => void }) {
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [signup, setSignup] = useState(false);
    const [notice, setNotice] = useState('');
    const [busy, setBusy] = useState(false);

    const submit = async (event: FormEvent) => {
        event.preventDefault();
        if (!supabase) return;
        setBusy(true);
        setNotice('');
        const result = signup
            ? await supabase.auth.signUp({ email, password })
            : await supabase.auth.signInWithPassword({ email, password });
        setBusy(false);
        if (result.error) return setNotice(result.error.message);
        if (result.data.session) onSession(result.data.session);
        else setNotice('Check your email to confirm your account, then sign in.');
    };

    return <main className="public-shell auth-shell">
        <section className="auth-card">
            <p className="eyebrow">Saksham Public Beta</p>
            <h1>{signup ? 'Create your account' : 'Welcome back'}</h1>
            <p className="muted">Hosted requests are processed on Saksham-operated infrastructure. You can also use your own supported provider key for one session.</p>
            <form onSubmit={submit}>
                <label>Email<input type="email" value={email} onChange={event => setEmail(event.target.value)} required autoComplete="email" /></label>
                <label>Password<input type="password" value={password} onChange={event => setPassword(event.target.value)} required minLength={8} autoComplete={signup ? 'new-password' : 'current-password'} /></label>
                {notice && <p className="notice">{notice}</p>}
                <button disabled={busy}>{busy ? 'Please wait…' : signup ? 'Create account' : 'Sign in'}</button>
            </form>
            <button className="link-button" onClick={() => { setSignup(!signup); setNotice(''); }}>
                {signup ? 'Already have an account? Sign in' : 'New to Saksham? Create an account'}
            </button>
        </section>
    </main>;
}

function PublicChat({ session }: { session: Session }) {
    const [messages, setMessages] = useState<Message[]>([]);
    const [input, setInput] = useState('');
    const [mode, setMode] = useState<Mode>('hosted');
    const [providerUrl, setProviderUrl] = useState('https://api.openai.com/v1');
    const [providerKey, setProviderKey] = useState('');
    const [model, setModel] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const send = async (event: FormEvent) => {
        event.preventDefault();
        const text = input.trim();
        if (!text || busy || !gatewayUrl) return;
        if (mode === 'byok' && !providerKey) return setError('Enter your provider key for this session.');
        const next = [...messages, { role: 'user' as const, content: text }];
        setMessages(next);
        setInput('');
        setBusy(true);
        setError('');
        try {
            const response = await fetch(`${gatewayUrl}/v1/chat/completions`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${session.access_token}`,
                    ...(mode === 'byok' ? { 'X-Saksham-Provider-Key': providerKey } : {}),
                },
                body: JSON.stringify({ messages: next, mode, provider_url: mode === 'byok' ? providerUrl : undefined, model: model || undefined }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || 'Unable to reach Saksham.');
            const content = payload.choices?.[0]?.message?.content;
            if (!content) throw new Error('The provider returned no response.');
            setMessages(current => [...current, { role: 'assistant', content }]);
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to send message.');
        } finally {
            setBusy(false);
        }
    };

    return <section className="public-content">
        <section className="provider-card">
            <div className="mode-choice">
                <button className={mode === 'hosted' ? 'selected' : ''} aria-pressed={mode === 'hosted'} onClick={() => setMode('hosted')}>Saksham Hosted</button>
                <button className={mode === 'byok' ? 'selected' : ''} aria-pressed={mode === 'byok'} onClick={() => setMode('byok')}>Use my API key</button>
            </div>
            {mode === 'hosted' ? <p className="muted">Hosted usage is limited to protect beta capacity. Prompts are processed transiently and are not saved as chat history.</p> : <div className="byok-fields"><input aria-label="Provider URL" value={providerUrl} onChange={event => setProviderUrl(event.target.value)} /><input aria-label="Provider API key" type="password" placeholder="Provider API key (session only)" value={providerKey} onChange={event => setProviderKey(event.target.value)} /><input aria-label="Model" placeholder="Model (optional)" value={model} onChange={event => setModel(event.target.value)} /></div>}
        </section>
        <section className="messages" aria-live="polite">{messages.length === 0 ? <p className="muted">Ask Saksham anything. Desktop automation is not available in the public beta.</p> : messages.map((message, index) => <article className={message.role} key={`${message.role}-${index}`}><strong>{message.role === 'assistant' ? 'Saksham' : 'You'}</strong><p>{message.content}</p></article>)}</section>
        <form className="composer" onSubmit={send}><input value={input} onChange={event => setInput(event.target.value)} placeholder="Ask Saksham…" disabled={busy} /><button disabled={busy}>{busy ? 'Thinking…' : 'Send'}</button></form>
        {error && <p className="notice">{error}</p>}
    </section>;
}

export function PublicMeetings({ session, gateway = gatewayUrl }: { session: Session; gateway?: string }) {
    const [file, setFile] = useState<File | null>(null);
    const [consent, setConsent] = useState(false);
    const [mode, setMode] = useState<Mode>('hosted');
    const [providerUrl, setProviderUrl] = useState('https://api.openai.com/v1');
    const [providerKey, setProviderKey] = useState('');
    const [model, setModel] = useState('gpt-4o-mini-transcribe');
    const [result, setResult] = useState<TranscriptionResponse | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const segments = normalizedSegments(result);

    const upload = async (event: FormEvent) => {
        event.preventDefault();
        if (!file || busy || !gateway) return;
        if (!consent) return setError('Confirm that every participant has consented before uploading.');
        if (file.size > maxAudioBytes) return setError('Choose an audio file smaller than 25 MB.');
        if (mode === 'byok' && !providerKey) return setError('Enter your provider key for this session.');

        const form = new FormData();
        form.append('audio', file, file.name);
        form.append('mode', mode);
        if (mode === 'byok') {
            form.append('provider_url', providerUrl);
            form.append('model', model || 'gpt-4o-mini-transcribe');
        }
        setBusy(true);
        setError('');
        setResult(null);
        try {
            const response = await fetch(`${gateway}/v1/meetings/transcribe`, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${session.access_token}`,
                    'X-Saksham-Consent-Confirmed': 'true',
                    ...(mode === 'byok' ? { 'X-Saksham-Provider-Key': providerKey } : {}),
                },
                body: form,
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || 'Unable to transcribe this recording.');
            setResult(payload as TranscriptionResponse);
            setFile(null);
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to transcribe this recording.');
        } finally {
            setBusy(false);
        }
    };

    const selectFile = (nextFile: File | undefined) => {
        setFile(nextFile ?? null);
        setResult(null);
        setError('');
    };

    return <section className="public-content meeting-content">
        <section className="meeting-intro">
            <p className="eyebrow">Meeting Intelligence</p>
            <h2>Transcribe a recording with explicit consent.</h2>
            <p className="muted">Public beta supports one uploaded recording at a time. It does not create a meeting history or enable desktop capture, system control, speaker biometrics, or private commands.</p>
        </section>
        <form className="meeting-form provider-card" onSubmit={upload}>
            <div className="mode-choice">
                <button type="button" className={mode === 'hosted' ? 'selected' : ''} aria-pressed={mode === 'hosted'} onClick={() => setMode('hosted')}>Saksham Hosted</button>
                <button type="button" className={mode === 'byok' ? 'selected' : ''} aria-pressed={mode === 'byok'} onClick={() => setMode('byok')}>Use my API key</button>
            </div>
            <label className="file-field">Recording<input type="file" accept={mode === 'hosted' ? 'audio/wav,.wav' : 'audio/*,.flac,.m4a,.mp3,.mp4,.mpeg,.mpga,.ogg,.wav,.webm'} onChange={event => selectFile(event.target.files?.[0])} /></label>
            {file && <p className="file-name">Selected: {file.name} ({Math.ceil(file.size / 1024 / 1024)} MB)</p>}
            {mode === 'hosted' ? <p className="muted">Hosted transcription accepts a mono PCM16 WAV recording up to 15 minutes and has a daily beta limit. Audio is processed by the private worker and is not retained as public chat history.</p> : <div className="byok-fields"><input aria-label="Transcription provider URL" value={providerUrl} onChange={event => setProviderUrl(event.target.value)} /><input aria-label="Transcription provider API key" type="password" placeholder="Provider API key (session only)" value={providerKey} onChange={event => setProviderKey(event.target.value)} /><input aria-label="Transcription model" value={model} onChange={event => setModel(event.target.value)} /></div>}
            {mode === 'byok' && <p className="muted">Your key is sent only with this request and is never stored. The initial adapter supports allowlisted OpenAI-compatible transcription providers.</p>}
            <label className="consent-check"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} /> <span>I confirm every participant has agreed to this recording being uploaded and transcribed.</span></label>
            <button disabled={busy || !file || !consent}>{busy ? 'Transcribing…' : 'Transcribe recording'}</button>
            {error && <p className="notice">{error}</p>}
        </form>
        {result && <section className="transcript-card" aria-live="polite">
            <div className="transcript-heading"><div><p className="eyebrow">Transcript</p><h3>{result.model ?? 'Transcription complete'}</h3></div>{result.language && <span>{result.language}</span>}</div>
            {segments.length > 0 ? <div className="transcript-list">{segments.map(segment => <article key={segment.id}><time>{formatTime(segment.start)}</time><p>{segment.text}</p></article>)}</div> : <p className="transcript-text">{result.text || 'The transcription provider returned no readable text.'}</p>}
        </section>}
    </section>;
}

function PublicHeader({ workspace, setWorkspace }: { workspace: Workspace; setWorkspace: (workspace: Workspace) => void }) {
    return <header className="public-header">
        <div><p className="eyebrow">Saksham Public Beta</p><h1>Private by default, powerful by choice.</h1></div>
        <div className="header-actions"><nav aria-label="Public beta workspaces"><button className={workspace === 'chat' ? 'selected' : ''} onClick={() => setWorkspace('chat')}>Chat</button><button className={workspace === 'meetings' ? 'selected' : ''} onClick={() => setWorkspace('meetings')}>Meeting Intelligence</button></nav><button className="link-button" onClick={() => void supabase?.auth.signOut()}>Sign out</button></div>
    </header>;
}

export default function PublicApp() {
    const [session, setSession] = useState<Session | null>(null);
    const [workspace, setWorkspace] = useState<Workspace>('chat');
    const configured = useMemo(() => Boolean(supabase && gatewayUrl), []);
    useEffect(() => {
        if (!supabase) return;
        void supabase.auth.getSession().then(({ data }) => setSession(data.session));
        const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, next) => setSession(next));
        return () => subscription.unsubscribe();
    }, []);
    if (!configured) return <main className="public-shell auth-shell"><section className="auth-card"><h1>Public beta is not configured</h1><p className="muted">Set the Supabase and gateway variables in the deployment environment.</p></section></main>;
    if (!session) return <AuthScreen onSession={setSession} />;
    return <main className="public-shell workspace-shell"><PublicHeader workspace={workspace} setWorkspace={setWorkspace} />{workspace === 'chat' ? <PublicChat session={session} /> : <PublicMeetings session={session} />}</main>;
}
