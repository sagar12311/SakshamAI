import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { createClient, type Session } from '@supabase/supabase-js';
import { Clock3, FileText, Mic2, Radio, ShieldCheck, Square, Users } from 'lucide-react';
import './components/Meeting/MeetingWorkspace.css';
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

function pcmFramesToWav(frames: ArrayBuffer[], sampleRate = 16_000): Blob {
    const pcmBytes = frames.reduce((total, frame) => total + frame.byteLength, 0);
    const output = new ArrayBuffer(44 + pcmBytes);
    const view = new DataView(output);
    const write = (offset: number, value: string) => [...value].forEach((character, index) => view.setUint8(offset + index, character.charCodeAt(0)));
    write(0, 'RIFF');
    view.setUint32(4, 36 + pcmBytes, true);
    write(8, 'WAVE');
    write(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    write(36, 'data');
    view.setUint32(40, pcmBytes, true);
    let offset = 44;
    for (const frame of frames) {
        new Uint8Array(output, offset, frame.byteLength).set(new Uint8Array(frame));
        offset += frame.byteLength;
    }
    return new Blob([output], { type: 'audio/wav' });
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
            <div className="auth-brand"><span className="brand-mark" aria-hidden="true">S</span><span>Saksham</span><span className="beta-badge">Beta</span></div>
            <p className="eyebrow">Your private workspace</p>
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

    return <section className="public-content chat-content">
        <section className="provider-card">
            <div className="section-heading"><div><p className="eyebrow">Assistant</p><h2>New conversation</h2></div><span className="session-status"><span />Private session</span></div>
            <div className="mode-choice">
                <button className={mode === 'hosted' ? 'selected' : ''} aria-pressed={mode === 'hosted'} onClick={() => setMode('hosted')}>Saksham Hosted</button>
                <button className={mode === 'byok' ? 'selected' : ''} aria-pressed={mode === 'byok'} onClick={() => setMode('byok')}>Use my API key</button>
            </div>
            {mode === 'hosted' ? <p className="muted">Hosted use is available to approved beta accounts. Prompts are processed transiently and are not saved as chat history.</p> : <div className="byok-fields"><input aria-label="Provider URL" value={providerUrl} onChange={event => setProviderUrl(event.target.value)} /><input aria-label="Provider API key" type="password" placeholder="Provider API key (session only)" value={providerKey} onChange={event => setProviderKey(event.target.value)} /><input aria-label="Model" placeholder="Model (optional)" value={model} onChange={event => setModel(event.target.value)} /></div>}
        </section>
        <section className="messages" aria-live="polite">{messages.length === 0 ? <div className="empty-chat"><span className="brand-mark" aria-hidden="true">S</span><div><h3>How can I help?</h3><p className="muted">Start a conversation with Saksham. Desktop automation is not included in this public beta.</p></div></div> : messages.map((message, index) => <article className={message.role} key={`${message.role}-${index}`}><strong>{message.role === 'assistant' ? 'Saksham' : 'You'}</strong><p>{message.content}</p></article>)}</section>
        <form className="composer" onSubmit={send}><input value={input} onChange={event => setInput(event.target.value)} placeholder="Ask Saksham…" disabled={busy} /><button disabled={busy}>{busy ? 'Thinking…' : 'Send'}</button></form>
        {error && <p className="notice">{error}</p>}
    </section>;
}

export function PublicMeetings({ session, gateway = gatewayUrl }: { session: Session; gateway?: string }) {
    const [consent, setConsent] = useState(false);
    const [mode, setMode] = useState<Mode>('hosted');
    const [providerUrl, setProviderUrl] = useState('https://api.openai.com/v1');
    const [providerKey, setProviderKey] = useState('');
    const [model, setModel] = useState('gpt-4o-mini-transcribe');
    const [result, setResult] = useState<TranscriptionResponse | null>(null);
    const [captureState, setCaptureState] = useState<'ready' | 'recording' | 'processing'>('ready');
    const [error, setError] = useState('');
    const [elapsed, setElapsed] = useState(0);
    const segments = normalizedSegments(result);
    const streamRef = useRef<MediaStream | null>(null);
    const contextRef = useRef<AudioContext | null>(null);
    const workletRef = useRef<AudioWorkletNode | null>(null);
    const framesRef = useRef<ArrayBuffer[]>([]);
    const startedAtRef = useRef(0);

    useEffect(() => () => {
        streamRef.current?.getTracks().forEach(track => track.stop());
        workletRef.current?.disconnect();
        void contextRef.current?.close();
    }, []);

    useEffect(() => {
        if (captureState !== 'recording') return;
        const timer = window.setInterval(() => setElapsed(Date.now() - startedAtRef.current), 250);
        return () => window.clearInterval(timer);
    }, [captureState]);

    const closeCapture = async () => {
        workletRef.current?.port.postMessage({ type: 'flush' });
        await new Promise(resolve => window.setTimeout(resolve, 120));
        streamRef.current?.getTracks().forEach(track => track.stop());
        workletRef.current?.disconnect();
        await contextRef.current?.close().catch(() => undefined);
        streamRef.current = null;
        workletRef.current = null;
        contextRef.current = null;
    };

    const startRecording = async () => {
        if (!consent || captureState !== 'ready') return;
        if (mode === 'byok' && !providerKey) return setError('Enter your provider key for this session.');
        try {
            setError('');
            setResult(null);
            framesRef.current = [];
            const stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
            });
            const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
            if (!AudioContextClass) throw new Error('This browser does not support microphone capture.');
            const context = new AudioContextClass({ latencyHint: 'interactive' });
            await context.audioWorklet.addModule('/meeting-audio-worklet.js');
            if (context.state === 'suspended') await context.resume();
            const source = context.createMediaStreamSource(stream);
            const worklet = new AudioWorkletNode(context, 'saksham-meeting-capture', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
            const mute = context.createGain();
            mute.gain.value = 0;
            worklet.port.onmessage = (event) => {
                if (event.data?.type === 'pcm' && event.data.pcm instanceof ArrayBuffer) framesRef.current.push(event.data.pcm);
            };
            source.connect(worklet);
            worklet.connect(mute);
            mute.connect(context.destination);
            streamRef.current = stream;
            contextRef.current = context;
            workletRef.current = worklet;
            startedAtRef.current = Date.now();
            setElapsed(0);
            setCaptureState('recording');
        } catch (captureError) {
            await closeCapture();
            setError(captureError instanceof Error ? captureError.message : 'Microphone capture could not start.');
        }
    };

    const stopRecording = async () => {
        if (captureState !== 'recording' || !gateway) return;
        if (mode === 'byok' && !providerKey) return setError('Enter your provider key for this session.');
        setCaptureState('processing');
        setError('');
        await closeCapture();
        const audio = pcmFramesToWav(framesRef.current);
        framesRef.current = [];
        if (audio.size <= 44) {
            setCaptureState('ready');
            return setError('No microphone audio was captured. Please try again.');
        }
        const form = new FormData();
        form.append('audio', audio, 'live-meeting.wav');
        form.append('mode', mode);
        if (mode === 'byok') {
            form.append('provider_url', providerUrl);
            form.append('model', model || 'gpt-4o-mini-transcribe');
        }
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
        } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : 'Unable to transcribe this recording.');
        } finally {
            setCaptureState('ready');
        }
    };

    return <section className="public-content meeting-workspace public-meeting-workspace">
        <header className="meeting-hero">
            <div><span className="meeting-eyebrow">Meeting intelligence</span><h2>Listen quietly. Remember precisely.</h2><p>Live microphone capture with explicit consent. No file picker, meeting history, speaker biometrics, desktop capture, or private commands are included in the public beta.</p></div>
            <div className={`worker-pill ${captureState === 'recording' ? 'connected' : 'idle'}`}><span className="worker-dot" />{captureState === 'recording' ? 'Microphone recording' : captureState === 'processing' ? 'Preparing transcript' : 'Ready for a meeting'}</div>
        </header>
        <div className="meeting-launch-grid">
        <article className="meeting-card launch-card">
            <div className="card-heading"><Mic2 size={19} /><div><h3>Live meeting</h3><p>Capture begins only after you explicitly start it.</p></div></div>
            <div className="mode-choice">
                <button type="button" className={mode === 'hosted' ? 'selected' : ''} aria-pressed={mode === 'hosted'} onClick={() => setMode('hosted')}>Saksham Hosted</button>
                <button type="button" className={mode === 'byok' ? 'selected' : ''} aria-pressed={mode === 'byok'} onClick={() => setMode('byok')}>Use my API key</button>
            </div>
            {mode === 'hosted' ? <p className="muted">Hosted use shares one GPU job at a time. The browser produces a mono PCM16 WAV only when you stop; it is processed transiently and not saved as meeting history.</p> : <div className="byok-fields"><input aria-label="Transcription provider URL" value={providerUrl} onChange={event => setProviderUrl(event.target.value)} /><input aria-label="Transcription provider API key" type="password" placeholder="Provider API key (session only)" value={providerKey} onChange={event => setProviderKey(event.target.value)} /><input aria-label="Transcription model" value={model} onChange={event => setModel(event.target.value)} /></div>}
            {mode === 'byok' && <p className="muted">Your key is sent only with this request and is never stored. The initial adapter supports allowlisted OpenAI-compatible transcription providers.</p>}
            <label className="consent-check"><input type="checkbox" checked={consent} disabled={captureState !== 'ready'} onChange={event => setConsent(event.target.checked)} /> <span>I confirm every participant has agreed to this live recording and transcription. Audio is kept in browser memory during capture and discarded after processing.</span></label>
            {captureState === 'ready' ? <button className="record-button" type="button" onClick={() => void startRecording()} disabled={!consent}><Mic2 size={18} /> Start recording</button> : <p className="file-preview">Microphone permission is active. Closing this page or pressing Stop ends capture and clears in-memory audio.</p>}
        </article>
        <article className="meeting-card trust-card"><div className="card-heading"><ShieldCheck size={19} /><div><h3>Privacy boundary</h3><p>Public beta keeps the desktop-only controls out of this workspace.</p></div></div><div className="profile-missing"><p>There is no file upload control and no recording library. Saksham cannot control your system or save reusable voice profiles here.</p><div className="enrollment-prompt"><span>Before you start</span>Everyone being recorded must know and agree.</div></div></article>
        </div>
        {(error || captureState !== 'ready') && <div className={`meeting-alert ${error ? 'warning' : 'success'}`}>{error ? <FileText size={17} /> : <Radio size={17} />}<span>{error || (captureState === 'recording' ? 'Recording locally in this browser. Saksham remains silent.' : 'Microphone is off. Sending the final in-memory audio for transcription.')}</span>{error && <button onClick={() => setError('')}>Dismiss</button>}</div>}
        {captureState !== 'ready' && <div className={`recording-strip ${captureState === 'processing' ? 'processing' : ''}`}><div className="recording-identity"><span className="recording-orbit"><span /></span><div><strong>Live meeting</strong><span>{captureState === 'recording' ? 'Recording continuously' : 'Finalizing the conversation'}</span></div></div><div className="recording-clock"><Clock3 size={18} /> {formatTime(elapsed)}</div>{captureState === 'recording' && <button className="stop-button" onClick={() => void stopRecording()}><Square size={15} fill="currentColor" /> Stop recording</button>}</div>}
        {result && <section className="meeting-results" aria-live="polite"><div className="results-toolbar"><div><span className="state-badge completed">completed</span><h3>Live transcript</h3><p>{result.model ?? 'Transcription complete'}{result.language ? ` · ${result.language}` : ''}</p></div></div><article className="meeting-card final-transcript"><div className="card-heading"><Users size={18} /><div><h3>Transcript</h3><p>Generated after capture stops. Recording audio has been cleared from this browser.</p></div></div>{segments.length > 0 ? <div className="transcript-list">{segments.map(segment => <div className="transcript-row" key={segment.id}><span>{formatTime(segment.start)}</span><p>{segment.text}</p></div>)}</div> : <p className="transcript-text">{result.text || 'The transcription provider returned no readable text.'}</p>}</article></section>}
    </section>;
}

function PublicHeader({ workspace, setWorkspace }: { workspace: Workspace; setWorkspace: (workspace: Workspace) => void }) {
    return <header className="public-header">
        <div className="brand-lockup"><span className="brand-mark" aria-hidden="true">S</span><span>Saksham</span><span className="beta-badge">Beta</span></div>
        <div className="header-actions"><nav aria-label="Public beta workspaces"><button className={workspace === 'chat' ? 'selected' : ''} onClick={() => setWorkspace('chat')}>Chat</button><button className={workspace === 'meetings' ? 'selected' : ''} onClick={() => setWorkspace('meetings')}>Meeting Intelligence</button></nav><span className="header-divider" /><button className="link-button" onClick={() => void supabase?.auth.signOut()}>Sign out</button></div>
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
