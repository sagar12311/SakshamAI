import { FormEvent, useEffect, useMemo, useState } from 'react';
import { createClient, type Session } from '@supabase/supabase-js';
import './PublicApp.css';

type Message = { role: 'user' | 'assistant'; content: string };
type Mode = 'hosted' | 'byok';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string | undefined;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined;
const gatewayUrl = (import.meta.env.VITE_SAKSHAM_GATEWAY_URL as string | undefined)?.replace(/\/$/, '');
const supabase = supabaseUrl && supabaseAnonKey ? createClient(supabaseUrl, supabaseAnonKey) : null;

function AuthScreen({ onSession }: { onSession: (session: Session) => void }) {
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [signup, setSignup] = useState(false);
    const [notice, setNotice] = useState('');
    const [busy, setBusy] = useState(false);

    const submit = async (event: FormEvent) => {
        event.preventDefault();
        if (!supabase) return;
        setBusy(true); setNotice('');
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
            <p className="muted">Hosted requests are processed on Saksham-operated infrastructure. You can also use your own supported provider key for a session.</p>
            <form onSubmit={submit}>
                <label>Email<input type="email" value={email} onChange={e => setEmail(e.target.value)} required autoComplete="email" /></label>
                <label>Password<input type="password" value={password} onChange={e => setPassword(e.target.value)} required minLength={8} autoComplete={signup ? 'new-password' : 'current-password'} /></label>
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
        setMessages(next); setInput(''); setBusy(true); setError('');
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
        } finally { setBusy(false); }
    };

    return <main className="public-shell chat-shell">
        <header><div><p className="eyebrow">Saksham Public Beta</p><h1>Private by default, powerful by choice.</h1></div><button className="link-button" onClick={() => void supabase?.auth.signOut()}>Sign out</button></header>
        <section className="provider-card">
            <div className="mode-choice"><button className={mode === 'hosted' ? 'selected' : ''} onClick={() => setMode('hosted')}>Saksham Hosted</button><button className={mode === 'byok' ? 'selected' : ''} onClick={() => setMode('byok')}>Use my API key</button></div>
            {mode === 'hosted' ? <p className="muted">Hosted usage is limited to protect beta capacity. Prompts are processed transiently and are not saved as chat history.</p> : <div className="byok-fields"><input aria-label="Provider URL" value={providerUrl} onChange={e => setProviderUrl(e.target.value)} /><input aria-label="Provider API key" type="password" placeholder="Provider API key (session only)" value={providerKey} onChange={e => setProviderKey(e.target.value)} /><input aria-label="Model" placeholder="Model (optional)" value={model} onChange={e => setModel(e.target.value)} /></div>}
        </section>
        <section className="messages" aria-live="polite">{messages.length === 0 ? <p className="muted">Ask Saksham anything. Desktop automation is not available in the public beta.</p> : messages.map((message, index) => <article className={message.role} key={`${message.role}-${index}`}><strong>{message.role === 'assistant' ? 'Saksham' : 'You'}</strong><p>{message.content}</p></article>)}</section>
        <form className="composer" onSubmit={send}><input value={input} onChange={e => setInput(e.target.value)} placeholder="Ask Saksham…" disabled={busy} /><button disabled={busy}>{busy ? 'Thinking…' : 'Send'}</button></form>
        {error && <p className="notice">{error}</p>}
    </main>;
}

export default function PublicApp() {
    const [session, setSession] = useState<Session | null>(null);
    const configured = useMemo(() => Boolean(supabase && gatewayUrl), []);
    useEffect(() => {
        if (!supabase) return;
        void supabase.auth.getSession().then(({ data }) => setSession(data.session));
        const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, next) => setSession(next));
        return () => subscription.unsubscribe();
    }, []);
    if (!configured) return <main className="public-shell auth-shell"><section className="auth-card"><h1>Public beta is not configured</h1><p className="muted">Set the Supabase and gateway variables in the deployment environment.</p></section></main>;
    return session ? <PublicChat session={session} /> : <AuthScreen onSession={setSession} />;
}
