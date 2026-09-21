/**
 * Saksham AI - Main Application with Voice-to-Voice Conversation
 */

import { useState, useCallback, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import Chat from './components/Chat/Chat';
import ModeIndicator from './components/Mode/ModeIndicator';
import TitleBar from './components/TitleBar/TitleBar';
import AmbientIndicator from './components/Voice/AmbientIndicator';
import MeetingWorkspace from './components/Meeting/MeetingWorkspace';
import CommandCenter, {
    type CommandCenterActivity,
    type CommandCenterTask,
} from './components/CommandCenter';
import { useAmbientListening } from './hooks/useAmbientListening';
import './App.css';

type Mode = 'assist' | 'execute' | 'autonomous' | 'shadow';
type Workspace = 'chat' | 'meetings' | 'tasks';

type IntegrationProposal = {
    id: string;
    title: string;
    status: string;
    research?: { summary?: string };
    plan?: Array<{ phase: string; detail: string }>;
    safety?: { chat_only?: boolean; auto_apply?: boolean };
    validation?: { result?: { configured?: boolean; requirement?: string } } | null;
};

type ChatMessage = {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: Date;
    fromVoice?: boolean;
    proposal?: IntegrationProposal;
};

type CommandCenterApiTask = CommandCenterTask & {
    approval_request?: { id?: string; status?: string; level?: string } | null;
    events?: Array<{
        id: string;
        type?: string;
        event_type?: string;
        message: string;
        level?: string;
        created_at?: string;
    }>;
};

const CONVERSATION_STORAGE_KEY = 'saksham.activeConversationId';

function getOrCreateConversationId(): string {
    const existing = window.localStorage.getItem(CONVERSATION_STORAGE_KEY);
    if (existing) return existing;

    const created = window.crypto?.randomUUID?.()
        ?? `session-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    window.localStorage.setItem(CONVERSATION_STORAGE_KEY, created);
    return created;
}

function App() {
    const [conversationId] = useState(getOrCreateConversationId);
    const [mode, setMode] = useState<Mode>('assist');
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [ambientEnabled, setAmbientEnabled] = useState(false);
    const [workspace, setWorkspace] = useState<Workspace>('chat');
    const [meetingRecording, setMeetingRecording] = useState(false);
    const [commandTasks, setCommandTasks] = useState<CommandCenterApiTask[]>([]);
    const [commandActivity, setCommandActivity] = useState<CommandCenterActivity[]>([]);
    const [commandBusy, setCommandBusy] = useState(false);
    const [commandError, setCommandError] = useState('');

    const handleVoiceError = useCallback((error: Error) => {
        console.error('Voice error:', error);
    }, []);

    useEffect(() => {
        const loadMode = async () => {
            try {
                const response = await fetch('/api/modes/current');
                if (!response.ok) {
                    throw new Error(`Failed to load mode: ${response.status}`);
                }

                const data = await response.json();
                if (data.mode && ['assist', 'execute', 'autonomous', 'shadow'].includes(data.mode)) {
                    setMode(data.mode as Mode);
                }
            } catch (error) {
                console.error('Mode load error:', error);
            }
        };

        void loadMode();
    }, []);

    // Helper to add unique messages
    const addUniqueMessage = useCallback((msg: any) => {
        // Filter out specific backend placeholder if it ever leaks through
        if (msg.content === "I'm processing your request...") {
            return;
        }

        setMessages(prev => {
            if (prev.some(m => m.id === msg.id || (m.content === msg.content && Math.abs(m.timestamp.getTime() - msg.timestamp.getTime()) < 2000))) {
                return prev;
            }
            return [...prev, msg];
        });
    }, []);

    const handleTranscription = useCallback((event: any) => {
        console.log('📝 App received transcription:', event);

        if (event.shouldRespond) {
            addUniqueMessage({
                id: Date.now().toString(),
                role: 'user',
                content: event.text,
                timestamp: new Date(),
                fromVoice: true
            });
        }
    }, [addUniqueMessage]);

    const handleVoiceResponse = useCallback((text: string) => {
        console.log('🤖 App received response:', text);
        addUniqueMessage({
            id: (Date.now() + 1).toString(),
            role: 'assistant',
            content: text,
            timestamp: new Date(),
            fromVoice: true
        });
    }, [addUniqueMessage]);

    const {
        isListening,
        hasPermission,
        transcriptionBuffer,
        isSpeaking,
        isUserSpeaking,
        audioLevel,
        startListening,
        stopListening,
    } = useAmbientListening({
        conversationId,
        onError: handleVoiceError,
        onTranscription: handleTranscription,
        onResponse: handleVoiceResponse,
        enabled: ambientEnabled
    });



    const handleSendMessage = useCallback(async (text: string, hiddenContext?: string) => {
        const userMessage = {
            id: Date.now().toString(),
            role: 'user' as const,
            content: text,
            timestamp: new Date(),
        };
        addUniqueMessage(userMessage);

        try {
            const response = await fetch('/api/chat/message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: text,
                    context: hiddenContext,
                    conversation_id: conversationId,
                }),
            });

            const data = await response.json();

            if (data.type === 'acknowledgement' || !data.response) {
                return;
            }

            const assistantMessage = {
                id: (Date.now() + 1).toString(),
                role: 'assistant' as const,
                content: data.response,
                timestamp: new Date(),
                proposal: data.proposal as IntegrationProposal | undefined,
            };
            addUniqueMessage(assistantMessage);
        } catch (error) {
            // handle error
        }
    }, [addUniqueMessage, conversationId]);

    const updateProposal = useCallback((proposal: IntegrationProposal) => {
        setMessages(previous => previous.map(message => (
            message.proposal?.id === proposal.id ? { ...message, proposal } : message
        )));
    }, []);

    const handleProposalFeedback = useCallback(async (proposalId: string, approved: boolean) => {
        try {
            const response = await fetch(`/api/integrations/proposals/${encodeURIComponent(proposalId)}/feedback`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ approved }),
            });
            if (!response.ok) throw new Error(`Feedback failed: ${response.status}`);
            const data = await response.json();
            if (data.proposal) updateProposal(data.proposal as IntegrationProposal);
        } catch (error) {
            console.error('Integration proposal feedback error:', error);
        }
    }, [updateProposal]);

    const handleProposalValidate = useCallback(async (proposalId: string) => {
        try {
            const response = await fetch(`/api/integrations/proposals/${encodeURIComponent(proposalId)}/validate`, {
                method: 'POST',
            });
            if (!response.ok) throw new Error(`Validation failed: ${response.status}`);
            const data = await response.json();
            if (data.proposal) updateProposal(data.proposal as IntegrationProposal);
        } catch (error) {
            console.error('Integration proposal validation error:', error);
        }
    }, [updateProposal]);

    const toggleAmbientListening = useCallback(() => {
        if (ambientEnabled) {
            setAmbientEnabled(false);
            stopListening();
            return;
        }

        setAmbientEnabled(true);
        void startListening();
    }, [ambientEnabled, startListening, stopListening]);

    const suspendAmbientForMeeting = useCallback(() => {
        const wasEnabled = ambientEnabled;
        if (wasEnabled) {
            setAmbientEnabled(false);
            stopListening();
        }
        return wasEnabled;
    }, [ambientEnabled, stopListening]);

    const restoreAmbientAfterMeeting = useCallback((wasEnabled: boolean) => {
        if (!wasEnabled) return;
        setAmbientEnabled(true);
        void startListening();
    }, [startListening]);

    const handleModeChange = useCallback(async (nextMode: Mode) => {
        try {
            const response = await fetch('/api/modes/switch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: nextMode }),
            });

            if (!response.ok) {
                throw new Error(`Failed to switch mode: ${response.status}`);
            }

            const data = await response.json();
            if (data.mode && ['assist', 'execute', 'autonomous', 'shadow'].includes(data.mode)) {
                setMode(data.mode as Mode);
            }
        } catch (error) {
            console.error('Mode switch error:', error);
        }
    }, []);

    const refreshCommandCenter = useCallback(async () => {
        try {
            const response = await fetch('/api/tasks?limit=30');
            if (!response.ok) throw new Error(`Failed to load tasks: ${response.status}`);
            const payload = await response.json();
            const tasks = Array.isArray(payload.tasks) ? payload.tasks as CommandCenterApiTask[] : [];
            setCommandTasks(tasks);

            const active = tasks.find((task) => [
                'running', 'cancelling', 'awaiting_approval', 'awaiting_confirmation',
                'awaiting_admin_activation', 'planning', 'queued', 'pending',
            ].includes(task.status)) || tasks[0];
            if (!active?.id) {
                setCommandActivity([]);
                return;
            }

            const detailResponse = await fetch(`/api/tasks/${encodeURIComponent(active.id)}`);
            if (!detailResponse.ok) return;
            const detailPayload = await detailResponse.json();
            const detailed = detailPayload.task as CommandCenterApiTask;
            if (!detailed) return;
            setCommandTasks((current) => current.map((task) => task.id === detailed.id ? detailed : task));
            setCommandActivity((detailed.events || []).slice().reverse().map((event) => ({
                id: event.id,
                title: String(event.type || event.event_type || 'Task update').replace(/[._-]/g, ' '),
                detail: event.message,
                timestamp: event.created_at,
                kind: event.level === 'error'
                    ? 'error'
                    : event.level === 'warning'
                        ? 'warning'
                        : event.level === 'success'
                            ? 'success'
                            : event.level === 'security'
                                ? 'security'
                                : 'info',
            })));
        } catch (error) {
            setCommandError(error instanceof Error ? error.message : 'Unable to load Command Center');
        }
    }, []);

    const resolveTaskApproval = useCallback(async (taskId: string, decision: 'approved' | 'rejected') => {
        const task = commandTasks.find((item) => item.id === taskId);
        const approvalId = task?.approval_request?.id;
        if (!approvalId) {
            setCommandError('This task no longer has an actionable approval request.');
            return;
        }
        if (task?.approval === 'admin' && decision === 'approved') {
            setCommandError('Admin activation will require the upcoming live voice and code challenge. A normal button cannot unlock this task.');
            return;
        }
        setCommandBusy(true);
        setCommandError('');
        try {
            const response = await fetch(
                `/api/tasks/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}`,
                {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ decision }),
                },
            );
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || `Approval update failed: ${response.status}`);
            await refreshCommandCenter();
        } catch (error) {
            setCommandError(error instanceof Error ? error.message : 'Unable to update approval');
        } finally {
            setCommandBusy(false);
        }
    }, [commandTasks, refreshCommandCenter]);

    const cancelCommandTask = useCallback(async (taskId: string) => {
        setCommandBusy(true);
        setCommandError('');
        try {
            const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ reason: 'Cancelled from Command Center' }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.detail || `Cancellation failed: ${response.status}`);
            await refreshCommandCenter();
        } catch (error) {
            setCommandError(error instanceof Error ? error.message : 'Unable to cancel task');
        } finally {
            setCommandBusy(false);
        }
    }, [refreshCommandCenter]);

    useEffect(() => {
        if (workspace !== 'tasks') return;
        void refreshCommandCenter();
        const intervalId = window.setInterval(() => void refreshCommandCenter(), 1500);
        return () => window.clearInterval(intervalId);
    }, [workspace, refreshCommandCenter]);

    // Polling for async updates with deduplication
    useEffect(() => {
        const fetchHistory = async () => {
            try {
                const response = await fetch(
                    `/api/chat/history/${encodeURIComponent(conversationId)}`
                );
                if (response.ok) {
                    const data = await response.json();
                    if (data.messages && Array.isArray(data.messages)) {
                        const historyMessages = data.messages
                            .filter((msg: any) => msg.content && msg.content.trim() !== "")
                            .map((msg: any) => ({
                                id: msg.id,
                                role: msg.role,
                                content: msg.content,
                                timestamp: new Date(msg.timestamp),
                                proposal: msg.proposal as IntegrationProposal | undefined,
                            }));

                        setMessages(prev => {
                            // Smart Merge: Only add new messages that aren't in prev
                            const newMessages = historyMessages.filter((h: { id: string; role: string; content: string; timestamp: Date }) =>
                                !prev.some(p => p.id === h.id || (p.content === h.content && Math.abs(p.timestamp.getTime() - h.timestamp.getTime()) < 5000))
                            );

                            if (newMessages.length === 0) return prev;

                            // Re-sort to be safe, though usually appended
                            return [...prev, ...newMessages].sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());
                        });
                    }
                }
            } catch (error) {
                // Silent fail
            }
        };

        const intervalId = setInterval(fetchHistory, 1000);
        return () => clearInterval(intervalId);
    }, [conversationId]);

    return (
        <div className="app">
            <TitleBar />

            <main className="app-main">
                <div className="app-header">
                    <div className="app-brand-group">
                        <motion.h1
                            className="app-title"
                            initial={{ opacity: 0, y: -20 }}
                            animate={{ opacity: 1, y: 0 }}
                        >
                            Saksham
                        </motion.h1>
                        <nav className="workspace-switch" aria-label="Workspace">
                            <button
                                className={workspace === 'chat' ? 'active' : ''}
                                onClick={() => setWorkspace('chat')}
                                disabled={meetingRecording}
                                title={meetingRecording ? 'Stop the meeting before leaving Meeting Mode' : 'Open chat'}
                            >
                                Chat
                            </button>
                            <button
                                className={workspace === 'tasks' ? 'active' : ''}
                                onClick={() => setWorkspace('tasks')}
                                disabled={meetingRecording}
                                title={meetingRecording ? 'Stop the meeting before leaving Meeting Mode' : 'Open Command Center'}
                            >
                                Tasks
                            </button>
                            <button
                                className={workspace === 'meetings' ? 'active' : ''}
                                onClick={() => setWorkspace('meetings')}
                            >
                                Meetings
                            </button>
                        </nav>
                    </div>
                    <div className="header-controls">
                        {workspace === 'chat' && (
                            <AmbientIndicator
                                isListening={isListening}
                                hasPermission={hasPermission}
                                onClick={toggleAmbientListening}
                            />
                        )}
                        <ModeIndicator mode={mode} onModeChange={handleModeChange} />
                    </div>
                </div>

                {workspace === 'chat' ? (
                    <>
                        <Chat
                            messages={messages}
                            onSendMessage={handleSendMessage}
                            onProposalFeedback={handleProposalFeedback}
                            onProposalValidate={handleProposalValidate}
                        />

                        <div className="voice-container">
                            <AnimatePresence mode="wait">
                                {isSpeaking ? (
                                    <motion.div
                                        key="speaking"
                                        className="speaking-indicator"
                                        initial={{ opacity: 0, scale: 0.9 }}
                                        animate={{ opacity: 1, scale: 1 }}
                                        exit={{ opacity: 0, scale: 0.9 }}
                                    >
                                        <div className="speaking-waves">
                                            <span></span><span></span><span></span><span></span><span></span>
                                        </div>
                                        <p>Saksham is speaking...</p>
                                    </motion.div>
                                ) : isUserSpeaking ? (
                                    <motion.div
                                        key="user-speaking"
                                        className="user-speaking-indicator"
                                        initial={{ opacity: 0, scale: 0.9 }}
                                        animate={{ opacity: 1, scale: 1 }}
                                        exit={{ opacity: 0, scale: 0.9 }}
                                    >
                                        <div className="listening-waves" style={{ '--audio-level': audioLevel } as React.CSSProperties}>
                                            <span></span><span></span><span></span><span></span><span></span>
                                        </div>
                                        <p>Listening...</p>
                                    </motion.div>
                                ) : isListening ? (
                                    <motion.p
                                        key="listening"
                                        className="ambient-hint"
                                        initial={{ opacity: 0 }}
                                        animate={{ opacity: 1 }}
                                        exit={{ opacity: 0 }}
                                        style={{ color: '#4CAF50' }}
                                    >
                                        Mic open - say anything
                                    </motion.p>
                                ) : (
                                    <motion.p
                                        key="off"
                                        className="ambient-hint"
                                        initial={{ opacity: 0 }}
                                        animate={{ opacity: 1 }}
                                        exit={{ opacity: 0 }}
                                    >
                                        Voice disabled
                                    </motion.p>
                                )}
                            </AnimatePresence>

                            {transcriptionBuffer.length > 0 && !isSpeaking && (
                                <motion.div
                                    className="transcription-preview"
                                    initial={{ opacity: 0, height: 0 }}
                                    animate={{ opacity: 1, height: 'auto' }}
                                >
                                    <p className="transcription-text">
                                        {transcriptionBuffer.slice(-2).map(t => t.text).join(' ')}
                                    </p>
                                </motion.div>
                            )}
                        </div>
                    </>
                ) : workspace === 'tasks' ? (
                    <section className="command-center-workspace" aria-label="Saksham Command Center">
                        {commandError && <p className="command-center-notice" role="alert">{commandError}</p>}
                        <CommandCenter
                            tasks={commandTasks}
                            activity={commandActivity}
                            isBusy={commandBusy}
                            onApprove={(taskId) => void resolveTaskApproval(taskId, 'approved')}
                            onReject={(taskId) => void resolveTaskApproval(taskId, 'rejected')}
                            onCancel={(taskId) => void cancelCommandTask(taskId)}
                            onRefresh={() => void refreshCommandCenter()}
                        />
                    </section>
                ) : (
                    <MeetingWorkspace
                        suspendAmbient={suspendAmbientForMeeting}
                        restoreAmbient={restoreAmbientAfterMeeting}
                        onRecordingChange={setMeetingRecording}
                    />
                )}
            </main>
        </div>
    );
}

export default App;
