import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    AlertTriangle,
    CheckCircle2,
    Clock3,
    FileText,
    FolderKanban,
    MessageSquare,
    Mic2,
    Pause,
    Play,
    Plus,
    Radio,
    RefreshCw,
    Save,
    ShieldCheck,
    SkipBack,
    SkipForward,
    Square,
    Users,
} from 'lucide-react';
import { pcmFramesToWav, useMeetingCapture } from '../../hooks/useMeetingCapture';
import type {
    Meeting,
    MeetingDetail,
    MeetingEvent,
    MeetingPrivateMessage,
    MeetingPointer,
    Project,
    TranscriptSegment,
    VoiceProfile,
} from '../../types/meeting';
import './MeetingWorkspace.css';

type MeetingWorkspaceProps = {
    suspendAmbient: () => boolean;
    restoreAmbient: (wasEnabled: boolean) => void;
    onRecordingChange: (recording: boolean) => void;
};

const ENROLLMENT_PROMPTS = [
    'Introduce yourself and describe your role.',
    'Describe what you want Saksham to help with.',
    'Talk naturally about your plans for this week.',
];

async function api<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(path, init);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
        throw new Error(payload.detail || `Request failed with ${response.status}`);
    }
    return payload as T;
}

function formatTime(milliseconds: number): string {
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return [hours, minutes, remainder].map((value) => String(value).padStart(2, '0')).join(':');
}

function elapsedSince(startedAt: string): string {
    return formatTime(Date.now() - new Date(startedAt).getTime());
}

function upsertById<T extends { id: string }>(items: T[], incoming: T): T[] {
    const index = items.findIndex((item) => item.id === incoming.id);
    if (index < 0) return [...items, incoming];
    return items.map((item, itemIndex) => itemIndex === index ? incoming : item);
}

export default function MeetingWorkspace({
    suspendAmbient,
    restoreAmbient,
    onRecordingChange,
}: MeetingWorkspaceProps) {
    const [projects, setProjects] = useState<Project[]>([]);
    const [meetings, setMeetings] = useState<Meeting[]>([]);
    const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
    const [projectsRoot, setProjectsRoot] = useState('');
    const [selectedProjectId, setSelectedProjectId] = useState('');
    const [projectName, setProjectName] = useState('');
    const [meetingTitle, setMeetingTitle] = useState('Client meeting');
    const [consentConfirmed, setConsentConfirmed] = useState(false);
    const [voiceMemoryConsentConfirmed, setVoiceMemoryConsentConfirmed] = useState(false);
    const [expectedSpeakerCount, setExpectedSpeakerCount] = useState('');
    const [activeMeeting, setActiveMeeting] = useState<Meeting | null>(null);
    const [detail, setDetail] = useState<MeetingDetail | null>(null);
    const [segments, setSegments] = useState<TranscriptSegment[]>([]);
    const [pointers, setPointers] = useState<MeetingPointer[]>([]);
    const [momContent, setMomContent] = useState('');
    const [privateMessages, setPrivateMessages] = useState<MeetingPrivateMessage[]>([]);
    const [privateCommand, setPrivateCommand] = useState('');
    const [isPrivateBusy, setIsPrivateBusy] = useState(false);
    const [selectedSegmentId, setSelectedSegmentId] = useState<string | null>(null);
    const [reprocessSpeakerCount, setReprocessSpeakerCount] = useState('');
    const [playbackMs, setPlaybackMs] = useState(0);
    const [isPlaying, setIsPlaying] = useState(false);
    const [assistantStatus, setAssistantStatus] = useState('Silent monitoring');
    const [processingProgress, setProcessingProgress] = useState(0);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [isBusy, setIsBusy] = useState(false);
    const [elapsed, setElapsed] = useState('00:00:00');
    const [enrollmentPrompt, setEnrollmentPrompt] = useState('');
    const [isEnrolling, setIsEnrolling] = useState(false);
    const ambientWasEnabledRef = useRef(false);
    const ambientRestoredRef = useRef(true);
    const audioRef = useRef<HTMLAudioElement | null>(null);

    const handleMeetingEvent = useCallback((event: MeetingEvent) => {
        if (event.type === 'transcript_segment') {
            setSegments((current) => upsertById(current, event.segment));
        } else if (event.type === 'meeting_pointer') {
            setPointers((current) => upsertById(current, event.pointer));
        } else if (event.type === 'private_message') {
            setPrivateMessages((current) => upsertById(current, event.message));
        } else if (event.type === 'assistant_state') {
            setAssistantStatus(event.text);
        } else if (event.type === 'meeting_state') {
            if (event.meeting) setActiveMeeting(event.meeting);
            else if (event.state) {
                setActiveMeeting((current) => current
                    ? { ...current, capture_state: event.state as Meeting['capture_state'] }
                    : current);
            }
        } else if (event.type === 'processing_progress') {
            setProcessingProgress(event.progress);
            setAssistantStatus(event.message);
        } else if (event.type === 'meeting_completed') {
            setActiveMeeting(event.meeting);
            setSegments(event.segments);
            setPointers(event.pointers);
            setMomContent(event.mom.content);
            setNotice(event.warnings.length ? 'MOM created with processing warnings.' : 'MOM draft is ready.');
        } else if (event.type === 'meeting_failed') {
            setActiveMeeting(event.meeting);
            setError(event.message);
        } else if (event.type === 'recoverable_error') {
            setError(`${event.stage}: ${event.message}`);
        } else if (event.type === 'worker_fallback') {
            setNotice(event.message);
        } else if (event.type === 'pending_confirmation') {
            setPrivateMessages((current) => upsertById(current, event.message));
            setAssistantStatus('Waiting for your private confirmation');
        } else if (event.type === 'action_progress') {
            setPrivateMessages((current) => upsertById(current, event.message));
            setAssistantStatus(event.message.text);
        } else if (event.type === 'reprocessing_completed') {
            setActiveMeeting(event.meeting);
            setNotice(event.success ? 'Speaker reprocessing is complete.' : 'Speaker reprocessing failed; the retained audio is still available to retry.');
        }
    }, []);

    const {
        isCapturing,
        connectionState,
        bufferedFrames,
        bufferWarning,
        startCapture,
        stopCapture,
    } = useMeetingCapture({
        onEvent: handleMeetingEvent,
        onError: setError,
    });

    const loadProjects = useCallback(async () => {
        const data = await api<{ projects: Project[] }>('/api/meetings/projects');
        setProjects(data.projects);
        setSelectedProjectId((current) => current || data.projects[0]?.id || '');
    }, []);

    const loadMeetings = useCallback(async () => {
        const data = await api<{ meetings: Meeting[] }>('/api/meetings?limit=100');
        setMeetings(data.meetings);
    }, []);

    const loadProfiles = useCallback(async () => {
        const data = await api<{ profiles: VoiceProfile[] }>('/api/meetings/voice-profiles/list');
        setProfiles(data.profiles);
    }, []);

    const loadDetail = useCallback(async (meetingId: string) => {
        const data = await api<MeetingDetail>(`/api/meetings/${encodeURIComponent(meetingId)}`);
        setDetail(data);
        setActiveMeeting(data.meeting);
        setSegments(data.segments);
        setPointers(data.pointers);
        setPrivateMessages(data.private_messages);
        setReprocessSpeakerCount(
            data.meeting.expected_speaker_count
                ? String(data.meeting.expected_speaker_count)
                : '',
        );
        setMomContent(data.mom?.content || '');
        return data;
    }, []);

    useEffect(() => {
        const load = async () => {
            try {
                const root = await api<{ path: string }>('/api/meetings/projects-root');
                setProjectsRoot(root.path);
                await Promise.all([loadProjects(), loadMeetings(), loadProfiles()]);
            } catch (loadError) {
                setError(String(loadError));
            }
        };
        void load();
    }, [loadMeetings, loadProfiles, loadProjects]);

    useEffect(() => {
        if (!activeMeeting || activeMeeting.capture_state !== 'recording') return;
        setElapsed(elapsedSince(activeMeeting.started_at));
        const timer = window.setInterval(() => setElapsed(elapsedSince(activeMeeting.started_at)), 1000);
        return () => window.clearInterval(timer);
    }, [activeMeeting]);

    useEffect(() => {
        const state = activeMeeting?.capture_state;
        if (!activeMeeting || !['processing', 'stopping'].includes(state || '')) return;
        const poll = window.setInterval(() => {
            void loadDetail(activeMeeting.id).then((result) => {
                if (['completed', 'failed', 'interrupted'].includes(result.meeting.capture_state)) {
                    void loadMeetings();
                }
            }).catch((pollError) => setError(String(pollError)));
        }, 2500);
        return () => window.clearInterval(poll);
    }, [activeMeeting, loadDetail, loadMeetings]);

    useEffect(() => {
        if (!activeMeeting || !['completed', 'failed', 'interrupted'].includes(activeMeeting.capture_state)) return;
        void loadDetail(activeMeeting.id).catch((detailError) => setError(String(detailError)));
    }, [activeMeeting?.id, activeMeeting?.capture_state, loadDetail]);

    useEffect(() => {
        onRecordingChange(isCapturing);
    }, [isCapturing, onRecordingChange]);

    const ownerProfile = profiles.find((profile) => profile.profile_type === 'owner');

    const restoreAmbientOnce = useCallback(() => {
        if (ambientRestoredRef.current) return;
        ambientRestoredRef.current = true;
        restoreAmbient(ambientWasEnabledRef.current);
    }, [restoreAmbient]);

    useEffect(() => {
        if (!isCapturing || activeMeeting?.capture_state === 'recording') return;
        void stopCapture({ serverAlreadyStopped: true })
            .then(() => {
                restoreAmbientOnce();
                setNotice('Recording has stopped. Final processing is underway.');
            })
            .catch((stopError) => setError(String(stopError)));
    }, [activeMeeting?.capture_state, isCapturing, restoreAmbientOnce, stopCapture]);

    const createProject = async () => {
        if (!projectName.trim()) return;
        setIsBusy(true);
        setError('');
        try {
            const data = await api<{ project: Project }>('/api/meetings/projects', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: projectName }),
            });
            setProjects((current) => [data.project, ...current]);
            setSelectedProjectId(data.project.id);
            setProjectName('');
        } catch (projectError) {
            setError(String(projectError));
        } finally {
            setIsBusy(false);
        }
    };

    const saveProjectsRoot = async () => {
        setIsBusy(true);
        try {
            const data = await api<{ path: string }>('/api/meetings/projects-root', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: projectsRoot }),
            });
            setProjectsRoot(data.path);
            setNotice('Project root updated. Existing projects keep their current folders.');
        } catch (rootError) {
            setError(String(rootError));
        } finally {
            setIsBusy(false);
        }
    };

    const startMeeting = async () => {
        if (!selectedProjectId || !consentConfirmed) return;
        setIsBusy(true);
        setError('');
        setNotice('');
        const wasEnabled = suspendAmbient();
        ambientWasEnabledRef.current = wasEnabled;
        ambientRestoredRef.current = false;
        let createdMeeting: Meeting | null = null;
        try {
            const data = await api<{ meeting: Meeting }>('/api/meetings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_id: selectedProjectId,
                    title: meetingTitle,
                    consent_confirmed: consentConfirmed,
                    expected_speaker_count: expectedSpeakerCount ? Number(expectedSpeakerCount) : null,
                    voice_memory_consent_confirmed: voiceMemoryConsentConfirmed,
                    voice_memory_consent_scope: voiceMemoryConsentConfirmed ? 'all_participants' : null,
                }),
            });
            createdMeeting = data.meeting;
            setActiveMeeting(data.meeting);
            setDetail(null);
            setSegments([]);
            setPointers([]);
            setMomContent('');
            setPrivateMessages([]);
            setSelectedSegmentId(null);
            setPlaybackMs(0);
            setAssistantStatus('Silent monitoring');
            setProcessingProgress(0);
            await startCapture(data.meeting.id);
            await loadMeetings();
        } catch (startError) {
            if (createdMeeting) {
                await fetch(`/api/meetings/${encodeURIComponent(createdMeeting.id)}/stop`, { method: 'POST' }).catch(() => undefined);
            }
            restoreAmbientOnce();
            setError(String(startError));
        } finally {
            setIsBusy(false);
        }
    };

    const stopMeeting = async () => {
        setIsBusy(true);
        try {
            await stopCapture();
            restoreAmbientOnce();
            if (activeMeeting) {
                await api(`/api/meetings/${encodeURIComponent(activeMeeting.id)}/stop`, { method: 'POST' });
            }
            if (activeMeeting) {
                const refreshed = await loadDetail(activeMeeting.id);
                setActiveMeeting(refreshed.meeting);
            }
            await loadMeetings();
        } catch (stopError) {
            setError(String(stopError));
        } finally {
            setIsBusy(false);
        }
    };

    const retryProcessing = async () => {
        if (!activeMeeting) return;
        setIsBusy(true);
        try {
            const data = await api<{ meeting: Meeting }>(
                `/api/meetings/${encodeURIComponent(activeMeeting.id)}/retry`,
                { method: 'POST' },
            );
            setActiveMeeting(data.meeting);
            setError('');
        } catch (retryError) {
            setError(String(retryError));
        } finally {
            setIsBusy(false);
        }
    };

    const reprocessMeeting = async () => {
        if (!activeMeeting) return;
        setIsBusy(true);
        setError('');
        try {
            const automatic = !reprocessSpeakerCount;
            const data = await api<{ meeting: Meeting }>(
                `/api/meetings/${encodeURIComponent(activeMeeting.id)}/reprocess`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        expected_speaker_count: automatic ? null : Number(reprocessSpeakerCount),
                        automatic_speaker_count: automatic,
                    }),
                },
            );
            setActiveMeeting(data.meeting);
            setNotice(automatic
                ? 'Reprocessing with automatic speaker detection.'
                : `Reprocessing with exactly ${reprocessSpeakerCount} speakers.`);
        } catch (reprocessError) {
            setError(String(reprocessError));
        } finally {
            setIsBusy(false);
        }
    };

    const submitPrivateCommand = async () => {
        if (!activeMeeting || !privateCommand.trim()) return;
        const command = privateCommand.trim();
        setPrivateCommand('');
        setIsPrivateBusy(true);
        setError('');
        try {
            const anchorMs = ['recording', 'stopping', 'processing'].includes(activeMeeting.capture_state)
                ? Math.max(0, Date.now() - new Date(activeMeeting.started_at).getTime())
                : playbackMs;
            await api(`/api/meetings/${encodeURIComponent(activeMeeting.id)}/private-commands`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: command,
                    anchor_ms: Math.round(anchorMs),
                    selected_segment_ids: selectedSegmentId ? [selectedSegmentId] : [],
                }),
            });
            await loadDetail(activeMeeting.id);
        } catch (commandError) {
            setPrivateCommand(command);
            setError(String(commandError));
        } finally {
            setIsPrivateBusy(false);
        }
    };

    const seekTo = useCallback((milliseconds: number, autoplay = true) => {
        const audio = audioRef.current;
        if (!audio) return;
        const seconds = Math.max(0, milliseconds / 1000);
        audio.currentTime = Math.min(seconds, Number.isFinite(audio.duration) ? audio.duration : seconds);
        setPlaybackMs(Math.round(audio.currentTime * 1000));
        if (autoplay) void audio.play().catch(() => setIsPlaying(false));
    }, []);

    const togglePlayback = () => {
        const audio = audioRef.current;
        if (!audio) return;
        if (audio.paused) void audio.play().catch(() => setIsPlaying(false));
        else audio.pause();
    };

    const nudgePlayback = (seconds: number) => {
        const audio = audioRef.current;
        if (!audio) return;
        seekTo((audio.currentTime + seconds) * 1000, false);
    };

    const saveMom = async (final: boolean) => {
        if (!activeMeeting || !momContent.trim()) return;
        setIsBusy(true);
        try {
            const path = final
                ? `/api/meetings/${encodeURIComponent(activeMeeting.id)}/mom/finalize`
                : `/api/meetings/${encodeURIComponent(activeMeeting.id)}/mom`;
            const data = await api<{ mom: { content: string; is_final: boolean } }>(path, {
                method: final ? 'POST' : 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: momContent }),
            });
            setMomContent(data.mom.content);
            setNotice(final ? 'MOM finalized and indexed in project memory.' : 'MOM draft saved.');
            await loadDetail(activeMeeting.id);
        } catch (momError) {
            setError(String(momError));
        } finally {
            setIsBusy(false);
        }
    };

    const regenerateMom = async () => {
        if (!activeMeeting) return;
        setIsBusy(true);
        setError('');
        try {
            const data = await api<{ mom: { content: string } }>(
                `/api/meetings/${encodeURIComponent(activeMeeting.id)}/mom/regenerate`,
                { method: 'POST' },
            );
            setMomContent(data.mom.content);
            setNotice('MOM regenerated from the final transcript. Review it before finalizing.');
            await loadDetail(activeMeeting.id);
        } catch (momError) {
            setError(String(momError));
        } finally {
            setIsBusy(false);
        }
    };

    const updateSpeaker = async (cluster: string, displayName: string) => {
        if (!activeMeeting) return;
        try {
            await api(`/api/meetings/${encodeURIComponent(activeMeeting.id)}/speakers/${encodeURIComponent(cluster)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ display_name: displayName }),
            });
            await loadDetail(activeMeeting.id);
        } catch (speakerError) {
            setError(String(speakerError));
        }
    };

    const rememberSpeaker = async (cluster: string, displayName: string) => {
        if (!activeMeeting) return;
        const confirmed = window.confirm(
            `${displayName} has explicitly consented to saving a reusable voice profile. Continue?`,
        );
        if (!confirmed) return;
        setIsBusy(true);
        try {
            await api(
                `/api/meetings/${encodeURIComponent(activeMeeting.id)}/speakers/${encodeURIComponent(cluster)}/remember`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ display_name: displayName, consent_confirmed: true }),
                },
            );
            await Promise.all([loadProfiles(), loadDetail(activeMeeting.id)]);
            setNotice(`${displayName}'s consented voice profile was saved securely.`);
        } catch (profileError) {
            setError(String(profileError));
        } finally {
            setIsBusy(false);
        }
    };

    const enrollOwner = async () => {
        setIsEnrolling(true);
        setError('');
        let stream: MediaStream | null = null;
        let context: AudioContext | null = null;
        try {
            stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
            });
            const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
            context = new AudioContextClass({ latencyHint: 'interactive' });
            await context.audioWorklet.addModule('/meeting-audio-worklet.js');
            if (context.state === 'suspended') await context.resume();
            const source = context.createMediaStreamSource(stream);
            const worklet = new AudioWorkletNode(context, 'saksham-meeting-capture');
            const mute = context.createGain();
            mute.gain.value = 0;
            const groups: Array<ArrayBuffer[]> = [[], [], []];
            let frameCount = 0;
            setEnrollmentPrompt(ENROLLMENT_PROMPTS[0]);
            const completed = new Promise<void>((resolve) => {
                worklet.port.onmessage = (event) => {
                    if (event.data?.type !== 'pcm' || !(event.data.pcm instanceof ArrayBuffer)) return;
                    const group = Math.min(2, Math.floor(frameCount / 7));
                    groups[group].push(event.data.pcm);
                    frameCount += 1;
                    if (frameCount % 7 === 0 && frameCount < 21) {
                        setEnrollmentPrompt(ENROLLMENT_PROMPTS[Math.floor(frameCount / 7)]);
                    }
                    if (frameCount >= 21) resolve();
                };
            });
            source.connect(worklet);
            worklet.connect(mute);
            mute.connect(context.destination);
            await Promise.race([
                completed,
                new Promise<never>((_, reject) => window.setTimeout(
                    () => reject(new Error('Voice enrollment timed out.')),
                    30000,
                )),
            ]);
            worklet.disconnect();
            const form = new FormData();
            form.append('display_name', 'You');
            form.append('consent_confirmed', 'true');
            groups.forEach((frames, index) => {
                form.append('audio_samples', pcmFramesToWav(frames), `owner-${index + 1}.wav`);
            });
            await api('/api/meetings/voice-profiles/enroll-owner', { method: 'POST', body: form });
            await loadProfiles();
            setNotice('Your encrypted owner voice profile is ready.');
        } catch (enrollError) {
            setError(String(enrollError));
        } finally {
            stream?.getTracks().forEach((track) => track.stop());
            await context?.close().catch(() => undefined);
            setEnrollmentPrompt('');
            setIsEnrolling(false);
        }
    };

    const speakerRows = useMemo(() => {
        const rows = new Map<string, string>();
        for (const segment of segments) {
            rows.set(segment.speaker_cluster, segment.speaker_label);
        }
        return Array.from(rows, ([cluster, label]) => ({ cluster, label }));
    }, [segments]);

    const activeSegmentId = useMemo(() => (
        segments.find((segment) => playbackMs >= segment.start_ms && playbackMs <= segment.end_ms)?.id
        || null
    ), [playbackMs, segments]);
    const activePointerId = useMemo(() => (
        pointers.find((pointer) => playbackMs >= pointer.start_ms && playbackMs <= pointer.end_ms)?.id
        || null
    ), [playbackMs, pointers]);
    const audioDurationMs = detail?.audio.duration_ms || 0;

    const privateConsole = (
        <article className="meeting-card private-panel">
            <div className="card-heading">
                <MessageSquare size={18} />
                <div><h3>Private console</h3><p>Typed commands and results stay private and are never read aloud.</p></div>
            </div>
            <div className="private-thread">
                {privateMessages.length ? privateMessages.map((message) => (
                    <div className={`private-message ${message.role}`} key={message.id}>
                        <div>
                            <span>{message.role === 'user' ? 'You' : 'Saksham'}</span>
                            <em>{message.status.replace('_', ' ')}</em>
                        </div>
                        <p>{message.text}</p>
                        {message.anchor_ms > 0 && detail?.audio.available && (
                            <button onClick={() => seekTo(message.anchor_ms)}>
                                {formatTime(message.anchor_ms)} / play context
                            </button>
                        )}
                    </div>
                )) : (
                    <p className="empty-copy">Try “Speaker 1 is Joshua”, “private action: send the proposal”, or ask a question about the transcript.</p>
                )}
            </div>
            <div className="private-composer">
                {selectedSegmentId && (
                    <div className="private-anchor">
                        Anchored to {formatTime(segments.find((item) => item.id === selectedSegmentId)?.start_ms || 0)}
                        <button onClick={() => setSelectedSegmentId(null)}>Clear</button>
                    </div>
                )}
                <textarea
                    value={privateCommand}
                    onChange={(event) => setPrivateCommand(event.target.value)}
                    onKeyDown={(event) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                            event.preventDefault();
                            void submitPrivateCommand();
                        }
                    }}
                    placeholder="Ask privately, map a speaker, save a note, or run a Saksham action..."
                    rows={3}
                />
                <button
                    className="secondary-button"
                    onClick={() => void submitPrivateCommand()}
                    disabled={isPrivateBusy || !privateCommand.trim()}
                >
                    {isPrivateBusy ? 'Working...' : 'Send privately'}
                </button>
            </div>
        </article>
    );

    const selectedProject = projects.find((project) => project.id === selectedProjectId);
    const isActive = activeMeeting?.capture_state === 'recording';
    const canRetry = activeMeeting && ['failed', 'interrupted'].includes(activeMeeting.capture_state);

    return (
        <section className="meeting-workspace">
            <header className="meeting-hero">
                <div>
                    <span className="meeting-eyebrow">Private meeting intelligence</span>
                    <h2>Listen quietly. Remember precisely.</h2>
                    <p>Mic-only capture, source-linked decisions, and project-ready minutes without a second voice joining the call.</p>
                </div>
                <div className={`worker-pill ${connectionState}`}>
                    <span className="worker-dot" />
                    {isActive ? `${connectionState} / ${bufferedFrames} queued` : 'Ready for a meeting'}
                </div>
            </header>

            {(error || bufferWarning || notice) && (
                <div className={`meeting-alert ${error || bufferWarning ? 'warning' : 'success'}`}>
                    {error || bufferWarning ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}
                    <span>{error || bufferWarning || notice}</span>
                    {(error || notice) && <button onClick={() => { setError(''); setNotice(''); }}>Dismiss</button>}
                </div>
            )}

            {!isActive && activeMeeting?.capture_state !== 'processing' && activeMeeting?.capture_state !== 'stopping' && (
                <div className="meeting-launch-grid">
                    <article className="meeting-card launch-card">
                        <div className="card-heading">
                            <FolderKanban size={19} />
                            <div><h3>Project and call</h3><p>The MOM will be filed under this project.</p></div>
                        </div>
                        <label>
                            Project
                            <select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}>
                                <option value="">Select a project</option>
                                {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
                            </select>
                        </label>
                        <div className="inline-create">
                            <input value={projectName} onChange={(event) => setProjectName(event.target.value)} placeholder="New client or project" />
                            <button className="secondary-button" onClick={() => void createProject()} disabled={isBusy || !projectName.trim()}>
                                <Plus size={16} /> Add
                            </button>
                        </div>
                        <label>
                            Meeting title
                            <input value={meetingTitle} onChange={(event) => setMeetingTitle(event.target.value)} />
                        </label>
                        <label>
                            Expected speakers
                            <select value={expectedSpeakerCount} onChange={(event) => setExpectedSpeakerCount(event.target.value)}>
                                <option value="">Auto detect</option>
                                {Array.from({ length: 9 }, (_, index) => index + 2).map((count) => (
                                    <option value={count} key={count}>{count} speakers</option>
                                ))}
                            </select>
                        </label>
                        <label className="consent-check">
                            <input type="checkbox" checked={consentConfirmed} onChange={(event) => setConsentConfirmed(event.target.checked)} />
                            <span>I confirm every participant has consented to this recording.</span>
                        </label>
                        <label className="consent-check biometric-consent">
                            <input
                                type="checkbox"
                                checked={voiceMemoryConsentConfirmed}
                                onChange={(event) => setVoiceMemoryConsentConfirmed(event.target.checked)}
                            />
                            <span>I confirm every participant separately consented to encrypted, reusable voice identification. This is optional.</span>
                        </label>
                        <button
                            className="record-button"
                            onClick={() => void startMeeting()}
                            disabled={isBusy || !selectedProjectId || !consentConfirmed}
                        >
                            <Mic2 size={18} /> Start recording
                        </button>
                        {selectedProject && <p className="file-preview">MOM destination: {selectedProject.folder_path}/Meetings</p>}
                    </article>

                    <article className="meeting-card trust-card">
                        <div className="card-heading">
                            <ShieldCheck size={19} />
                            <div><h3>Owner voice</h3><p>Only your verified voice can wake or stop Saksham.</p></div>
                        </div>
                        {ownerProfile ? (
                            <div className="profile-ready"><CheckCircle2 size={22} /><div><strong>{ownerProfile.display_name}</strong><span>Encrypted voiceprint active</span></div></div>
                        ) : (
                            <div className="profile-missing">
                                <p>Record three natural samples so clients cannot activate private mode.</p>
                                <button className="secondary-button" onClick={() => void enrollOwner()} disabled={isEnrolling}>
                                    <Radio size={16} /> {isEnrolling ? 'Recording samples...' : 'Enroll my voice'}
                                </button>
                                {enrollmentPrompt && <div className="enrollment-prompt"><span>Speak now</span>{enrollmentPrompt}</div>}
                            </div>
                        )}
                        <div className="root-setting">
                            <label>Projects root<input value={projectsRoot} onChange={(event) => setProjectsRoot(event.target.value)} /></label>
                            <button className="text-button" onClick={() => void saveProjectsRoot()} disabled={isBusy}>Update root</button>
                        </div>
                    </article>
                </div>
            )}

            {activeMeeting && ['recording', 'stopping', 'processing'].includes(activeMeeting.capture_state) && (
                <div className={`live-meeting ${activeMeeting.capture_state}`}>
                    <div className={`recording-strip ${activeMeeting.capture_state}`}>
                        <div className="recording-identity">
                            <span className="recording-orbit"><span /></span>
                            <div><strong>{activeMeeting.title}</strong><span>{activeMeeting.capture_state === 'recording' ? 'Recording continuously' : 'Finalizing the conversation'}</span></div>
                        </div>
                        <div className="recording-clock"><Clock3 size={18} /> {elapsed}</div>
                        {activeMeeting.capture_state === 'recording' && (
                            <button className="stop-button" onClick={() => void stopMeeting()} disabled={isBusy}>
                                <Square size={15} fill="currentColor" /> Stop recording
                            </button>
                        )}
                    </div>
                    <div className="silent-status">
                        <ShieldCheck size={16} />
                        {activeMeeting.capture_state === 'recording'
                            ? `${assistantStatus}. Audio capture is active and Saksham will not speak.`
                            : activeMeeting.capture_state === 'stopping'
                                ? 'Closing microphone capture and confirming the final frames.'
                                : `Microphone is off. ${assistantStatus}`}
                    </div>
                    {activeMeeting.capture_state === 'processing' && (
                        <div className="processing-meter" aria-label={`Meeting processing ${processingProgress}%`}>
                            <span style={{ width: `${processingProgress}%` }} />
                        </div>
                    )}
                    <div className="live-columns">
                        <article className="meeting-card live-panel">
                            <div className="card-heading"><FileText size={18} /><div><h3>Live pointers</h3><p>Provisional until final processing.</p></div></div>
                            <div className="pointer-list">
                                {pointers.length ? pointers.map((pointer) => (
                                    <div className="pointer-row" key={pointer.id}>
                                        <span>{formatTime(pointer.start_ms)}</span>
                                        <div><small>{pointer.category.replace('_', ' ')}</small><p>{pointer.content}</p></div>
                                    </div>
                                )) : <p className="empty-copy">Pointers appear after the first meaningful discussion window.</p>}
                            </div>
                        </article>
                        <article className="meeting-card live-panel transcript-panel">
                            <div className="card-heading"><Users size={18} /><div><h3>Transcript</h3><p>Speaker labels are corrected after stop.</p></div></div>
                            <div className="transcript-list">
                                {segments.length ? segments.map((segment) => (
                                    <div
                                        className={`transcript-row ${selectedSegmentId === segment.id ? 'selected' : ''}`}
                                        key={segment.id}
                                        onClick={() => setSelectedSegmentId(segment.id)}
                                    >
                                        <span>{formatTime(segment.start_ms)}</span>
                                        <p><strong>{segment.speaker_label}</strong>{segment.text}</p>
                                    </div>
                                )) : <p className="empty-copy">Listening without interrupting...</p>}
                            </div>
                        </article>
                        {privateConsole}
                    </div>
                </div>
            )}

            {activeMeeting && ['completed', 'failed', 'interrupted'].includes(activeMeeting.capture_state) && (
                <div className="meeting-results">
                    <div className="results-toolbar">
                        <div><span className={`state-badge ${activeMeeting.capture_state}`}>{activeMeeting.capture_state}</span><h3>{activeMeeting.title}</h3><p>{detail?.project?.name || activeMeeting.project_name}</p></div>
                        <div className="results-actions">
                            {canRetry && <button className="secondary-button" onClick={() => void retryProcessing()} disabled={isBusy}><RefreshCw size={16} /> Retry processing</button>}
                            {detail?.audio.available && (
                                <div className="reprocess-control">
                                    <select
                                        aria-label="Re-diarization speaker count"
                                        value={reprocessSpeakerCount}
                                        onChange={(event) => setReprocessSpeakerCount(event.target.value)}
                                    >
                                        <option value="">Auto speakers</option>
                                        {Array.from({ length: 9 }, (_, index) => index + 2).map((count) => (
                                            <option value={count} key={count}>{count} speakers</option>
                                        ))}
                                    </select>
                                    <button className="secondary-button" onClick={() => void reprocessMeeting()} disabled={isBusy}>
                                        <RefreshCw size={16} /> Re-diarize
                                    </button>
                                </div>
                            )}
                        </div>
                    </div>
                    {activeMeeting.processing_error && <div className="processing-warning"><AlertTriangle size={16} />{activeMeeting.processing_error}</div>}
                    {detail?.audio.available ? (
                        <article className="meeting-card audio-player-card">
                            <audio
                                ref={audioRef}
                                src={`/api/meetings/${encodeURIComponent(activeMeeting.id)}/audio`}
                                preload="metadata"
                                onTimeUpdate={(event) => setPlaybackMs(Math.round(event.currentTarget.currentTime * 1000))}
                                onPlay={() => setIsPlaying(true)}
                                onPause={() => setIsPlaying(false)}
                                onEnded={() => setIsPlaying(false)}
                            />
                            <button onClick={() => nudgePlayback(-10)} aria-label="Back 10 seconds"><SkipBack size={17} /></button>
                            <button className="play-toggle" onClick={togglePlayback} aria-label={isPlaying ? 'Pause' : 'Play'}>
                                {isPlaying ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}
                            </button>
                            <button onClick={() => nudgePlayback(10)} aria-label="Forward 10 seconds"><SkipForward size={17} /></button>
                            <span>{formatTime(playbackMs)}</span>
                            <input
                                type="range"
                                min="0"
                                max={Math.max(1, audioDurationMs)}
                                value={Math.min(playbackMs, Math.max(1, audioDurationMs))}
                                onChange={(event) => seekTo(Number(event.target.value), false)}
                                aria-label="Meeting playback position"
                            />
                            <span>{formatTime(audioDurationMs)}</span>
                            <small>Audio expires {detail.audio.expires_at ? new Date(detail.audio.expires_at).toLocaleDateString() : 'after retention'}</small>
                        </article>
                    ) : (
                        <div className="audio-unavailable">The retained recording is unavailable or has expired. Transcript and MOM remain available.</div>
                    )}
                    <div className="results-grid">
                        <article className="meeting-card speaker-editor">
                            <div className="card-heading"><Users size={18} /><div><h3>Speakers</h3><p>Rename first; remember only with consent.</p></div></div>
                            {speakerRows.map(({ cluster, label }) => <SpeakerRow key={cluster} cluster={cluster} initialName={label} onSave={updateSpeaker} onRemember={rememberSpeaker} />)}
                            {!speakerRows.length && <p className="empty-copy">Speaker labels will appear after diarization.</p>}
                        </article>
                        <article className="meeting-card final-pointers">
                            <div className="card-heading"><FileText size={18} /><div><h3>Source timeline</h3><p>Every item links back to a timestamp.</p></div></div>
                            {pointers.map((pointer) => (
                                <div className={`pointer-row ${activePointerId === pointer.id ? 'active' : ''}`} key={pointer.id}>
                                    <button className="timestamp-link" onClick={() => seekTo(pointer.start_ms)} disabled={!detail?.audio.available}>{formatTime(pointer.start_ms)}</button>
                                    <div><small>{pointer.category.replace('_', ' ')}</small><p>{pointer.content}</p></div>
                                </div>
                            ))}
                        </article>
                    </div>
                    <div className="results-lower-grid">
                        <article className="meeting-card final-transcript transcript-panel">
                            <div className="card-heading"><Users size={18} /><div><h3>Final transcript</h3><p>Select a line to anchor a private command, or click its time to play it.</p></div></div>
                            <div className="transcript-list">
                                {segments.map((segment) => (
                                    <div
                                        className={`transcript-row ${activeSegmentId === segment.id ? 'active' : ''} ${selectedSegmentId === segment.id ? 'selected' : ''}`}
                                        key={segment.id}
                                        onClick={() => setSelectedSegmentId(segment.id)}
                                    >
                                        <button className="timestamp-link" onClick={(event) => { event.stopPropagation(); seekTo(segment.start_ms); }} disabled={!detail?.audio.available}>{formatTime(segment.start_ms)}</button>
                                        <p><strong>{segment.speaker_label}</strong>{segment.text}</p>
                                    </div>
                                ))}
                            </div>
                        </article>
                        {privateConsole}
                    </div>
                    {momContent && (
                        <article className="meeting-card mom-editor">
                            <div className="mom-toolbar">
                                <div className="card-heading"><FileText size={19} /><div><h3>Minutes of Meeting</h3><p>{activeMeeting.mom_path || 'Project note'}</p></div></div>
                                <div><button className="secondary-button" onClick={() => void regenerateMom()} disabled={isBusy}><RefreshCw size={16} /> Regenerate MOM</button><button className="secondary-button" onClick={() => void saveMom(false)} disabled={isBusy}><Save size={16} /> Save draft</button><button className="finalize-button" onClick={() => void saveMom(true)} disabled={isBusy}><CheckCircle2 size={16} /> Finalize MOM</button></div>
                            </div>
                            <textarea value={momContent} onChange={(event) => setMomContent(event.target.value)} spellCheck />
                        </article>
                    )}
                </div>
            )}

            {!isActive && (
                <article className="meeting-card history-card">
                    <div className="card-heading"><Clock3 size={18} /><div><h3>Meeting history</h3><p>Audio expires after 30 days; notes stay with the project.</p></div></div>
                    <div className="history-list">
                        {meetings.map((meeting) => (
                            <button key={meeting.id} onClick={() => void loadDetail(meeting.id)} className={activeMeeting?.id === meeting.id ? 'selected' : ''}>
                                <span><strong>{meeting.title}</strong><small>{meeting.project_name} / {new Date(meeting.started_at).toLocaleString()}</small></span>
                                <em className={`state-badge ${meeting.capture_state}`}>{meeting.capture_state}</em>
                            </button>
                        ))}
                        {!meetings.length && <p className="empty-copy">Your first recorded meeting will appear here.</p>}
                    </div>
                </article>
            )}
        </section>
    );
}

function SpeakerRow({
    cluster,
    initialName,
    onSave,
    onRemember,
}: {
    cluster: string;
    initialName: string;
    onSave: (cluster: string, name: string) => Promise<void>;
    onRemember: (cluster: string, name: string) => Promise<void>;
}) {
    const [name, setName] = useState(initialName);
    useEffect(() => setName(initialName), [initialName]);
    return (
        <div className="speaker-row">
            <span>{cluster}</span>
            <input value={name} onChange={(event) => setName(event.target.value)} />
            <button onClick={() => void onSave(cluster, name)}>Rename</button>
            <button onClick={() => void onRemember(cluster, name)}>Remember voice</button>
        </div>
    );
}
