import { useCallback, useEffect, useRef, useState } from 'react';
import type { Session } from '@supabase/supabase-js';
import './PublicVoice.css';

type Event = { id: number; role: 'user' | 'assistant'; content: string; remembered: boolean };
type Conversation = { id: string; title: string; updated_at?: string };
type Run = { stopped: boolean; socket?: WebSocket; stream?: MediaStream; audio?: AudioContext; worklet?: AudioWorkletNode; timer?: number; next: number; paused: boolean; sources: Set<AudioBufferSourceNode> };

function dispose(run: Run) {
    run.stopped = true;
    window.clearTimeout(run.timer);
    run.stream?.getTracks().forEach(track => track.stop());
    run.worklet?.disconnect();
    if (run.socket && run.socket.readyState < WebSocket.CLOSING) run.socket.close();
    void run.audio?.close().catch(() => undefined);
}

export default function PublicVoice({ session, gateway }: { session: Session; gateway: string }) {
    const [conversations, setConversations] = useState<Conversation[]>([]);
    const [active, setActive] = useState<string>('');
    const [events, setEvents] = useState<Event[]>([]);
    const [consent, setConsent] = useState(false);
    const [state, setState] = useState<'idle' | 'connecting' | 'live' | 'saving'>('idle');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const runRef = useRef<Run>();
    const alive = useRef(true);
    const token = useRef(session.access_token);
    const selection = useRef('');
    token.current = session.access_token;

    const api = useCallback(async (path: string, method = 'GET', body?: unknown) => {
        const response = await fetch(gateway+path, { method, headers: { Authorization: `Bearer ${token.current}`, 'Content-Type':'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Voice history could not be saved or loaded.');
        return data;
    }, [gateway]);

    const refresh = useCallback(async () => {
        const list = await api('/v1/voice/conversations');
        if (alive.current) setConversations(list);
    }, [api]);

    const load = async (id: string) => {
        selection.current = id; setActive(id); setEvents([]); setLoading(true); setError('');
        try {
            let after: number | null = 0;
            const rows: Event[] = [];
            do {
                const page = await api(`/v1/voice/conversations/${id}/events?after=${after}`);
                rows.push(...page.items); after = page.next_after;
            } while (after !== null && alive.current && selection.current === id);
            if (alive.current && selection.current === id) setEvents(rows);
        } catch (e) { if (alive.current) setError((e as Error).message); }
        finally { if (alive.current) setLoading(false); }
    };

    useEffect(() => {
        alive.current = true;
        void refresh().catch(e => { if (alive.current) setError(e.message); });
        return () => { alive.current = false; if (runRef.current) dispose(runRef.current); };
    }, [refresh]);

    const start = async () => {
        if (!consent || state !== 'idle' || loading) return;
        setError(''); setState('connecting');
        const run: Run = { stopped: false, next: 0, paused: false, sources: new Set() };
        runRef.current = run;
        const fail = (message: string) => {
            if (run.stopped) return;
            dispose(run);
            if (alive.current) { setError(message); setState('idle'); }
        };
        try {
            run.audio = new AudioContext({ latencyHint:'interactive' });
            await run.audio.resume();
            if (run.stopped) return;
            const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount:1, echoCancellation:true, noiseSuppression:true, autoGainControl:true } });
            if (run.stopped) { stream.getTracks().forEach(t => t.stop()); return; }
            run.stream = stream;
            let id = active;
            if (!id) {
                const created = await api('/v1/voice/conversations','POST');
                if (run.stopped) return;
                id = created.id; setActive(id); selection.current = id; setEvents([]);
                void refresh().catch(() => undefined);
            }
            await run.audio.audioWorklet.addModule('/voice-audio-worklet.js');
            if (run.stopped) return;
            const url = new URL(`${gateway}/v1/voice/conversations/${id}/live`, window.location.href);
            url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
            const socket = new WebSocket(url);
            socket.binaryType = 'arraybuffer'; run.socket = socket;
            run.timer = window.setTimeout(() => fail('Voice server did not become ready. Try again after it finishes loading.'), 200000);
            socket.onopen = () => socket.send(JSON.stringify({ type:'auth', token:token.current, consent:true }));
            socket.onerror = () => fail('Unable to connect to voice. Check that the voice server and gateway are running.');
            socket.onclose = () => {
                if (!run.stopped) fail('Voice disconnected. Recent speech may not be saved. Reload history before resuming.');
            };
            socket.onmessage = ({ data }) => {
                if (run.stopped) return;
                try {
                    if (data instanceof ArrayBuffer) {
                        if (run.paused) return;
                        const samples = new Int16Array(data);
                        const audio = run.audio!;
                        if (run.next - audio.currentTime > 1.5) throw new Error('Audio playback fell behind. Pause and resume to reconnect.');
                        const buffer = audio.createBuffer(1,samples.length,24000);
                        const output = buffer.getChannelData(0);
                        for (let i=0; i<samples.length; i++) output[i] = samples[i]/32768;
                        const source = audio.createBufferSource(); source.buffer = buffer; source.connect(audio.destination);
                        run.sources.add(source); source.onended = () => { run.sources.delete(source); source.disconnect(); };
                        run.next = Math.max(run.next,audio.currentTime+0.02); source.start(run.next); run.next += buffer.duration;
                        return;
                    }
                    const event = JSON.parse(data);
                    if (event.type === 'ready') {
                        window.clearTimeout(run.timer);
                        const source = run.audio!.createMediaStreamSource(stream);
                        const worklet = new AudioWorkletNode(run.audio!, 'saksham-voice-capture');
                        run.worklet = worklet;
                        worklet.port.onmessage = ({ data: frame }) => {
                            if (run.stopped || socket.readyState !== WebSocket.OPEN) return;
                            if (frame.type === 'flushed') { socket.send('{"type":"pause"}'); return; }
                            if (frame.type === 'pcm') {
                                if (socket.bufferedAmount>96000) { fail('Connection is too slow for live voice. Please reconnect.'); return; }
                                socket.send(frame.pcm);
                            }
                        };
                        const mute = run.audio!.createGain(); mute.gain.value=0;
                        source.connect(worklet); worklet.connect(mute); mute.connect(run.audio!.destination);
                        setState('live');
                    } else if (event.type === 'transcript') {
                        setEvents(previous => previous.some(e => e.id === event.event.id) ? previous : [...previous,event.event]);
                    } else if (event.type === 'paused') {
                        dispose(run); setState('idle'); void refresh().catch(() => undefined);
                    } else if (event.type === 'error') fail(event.detail);
                } catch (e) { fail((e as Error).message); }
            };
        } catch (e) { fail((e as Error).message); }
    };

    const pause = () => {
        const run = runRef.current;
        if (!run || state !== 'live') return;
        run.paused = true; setState('saving');
        run.sources.forEach(source => source.stop()); run.sources.clear();
        run.worklet?.port.postMessage('pause');
        run.stream?.getTracks().forEach(t => t.stop());
        // Keep the worklet clock active until its flush acknowledgement arrives.
        run.timer = window.setTimeout(() => {
            dispose(run); setState('idle'); setError('Transcript saving timed out. Reload history before resuming.');
        }, 65000);
    };

    const remember = async (event: Event) => {
        try {
            const updated = await api(`/v1/voice/conversations/${active}/events/${event.id}`, 'PATCH', { remembered: !event.remembered });
            if (alive.current) setEvents(previous => previous.map(e => e.id === updated.id ? updated : e));
        } catch (e) { if (alive.current) setError((e as Error).message); }
    };
    const busy = state !== 'idle';
    return <section className="voice-workspace">
        <header><h1>Talk with Saksham</h1><p>A natural conversation, with a transcript you can return to.</p></header>
        <div className="voice-history-actions">
            <label>Voice history<select aria-label="Voice history" value={active} disabled={busy || loading} onChange={e => { if (e.target.value) void load(e.target.value); else { selection.current=''; setActive(''); setEvents([]); } }}><option value="">New conversation</option>{conversations.map(c => <option key={c.id} value={c.id}>{c.title} · {c.updated_at ? new Date(c.updated_at).toLocaleString() : ''}</option>)}</select></label>
            {active && <button disabled={busy || loading} onClick={() => void load(active)}>Reload history</button>}
            {active && <button disabled={busy || loading} onClick={async () => { if (!window.confirm('Permanently delete this voice conversation and its remembered details?')) return; try { await api(`/v1/conversations/${active}`,'DELETE'); setActive(''); selection.current=''; setEvents([]); await refresh(); } catch (e) { setError((e as Error).message); } }}>Delete conversation</button>}
        </div>
        <label className="voice-consent"><input type="checkbox" checked={consent} disabled={busy} onChange={e => setConsent(e.target.checked)} /><span>I agree to stream my microphone audio and save the transcript to my account.</span></label>
        <p className="voice-privacy">Audio is processed on Saksham’s server without recording files. Transcripts remain until you delete the conversation. Captions may contain mistakes; only remember details you have checked.</p>
        <div className="voice-controls"><span role="status">{loading ? 'Loading history…' : state === 'connecting' ? 'Preparing voice…' : state === 'live' ? 'Listening · microphone on' : state === 'saving' ? 'Microphone off · saving transcript…' : 'Ready'}</span>
            {state === 'live' ? <button onClick={pause}>Pause and save</button> : <button disabled={!consent || busy || loading} onClick={() => void start()}>{active ? 'Resume conversation' : 'Start talking'}</button>}
            {state === 'connecting' && <button onClick={() => { if (runRef.current) dispose(runRef.current); setState('idle'); }}>Cancel</button>}
        </div>
        <p className="voice-privacy">Remembered details apply when you resume. Pause and save before leaving this page. Spoken replies and transcription run separately; captions can arrive out of speaking order.</p>
        {error && <p role="alert" className="notice">{error}</p>}
        <div className="voice-transcript" aria-label="Voice transcript" aria-live="polite">{events.length === 0 && <p>Your saved transcript will appear here.</p>}{events.map(event => <article key={event.id}><strong>{event.role === 'user' ? 'You' : 'Saksham'}</strong><p>{event.content}</p>{event.role === 'user' && <button disabled={busy} aria-pressed={event.remembered} onClick={() => void remember(event)}>{event.remembered ? 'Forget this detail' : 'Remember this detail'}</button>}</article>)}</div>
    </section>;
}
