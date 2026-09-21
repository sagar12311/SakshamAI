/**
 * Saksham AI - Chat Component
 */

import { useState, useRef, useEffect, FormEvent, ChangeEvent } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Send, Loader2, Paperclip, FileText, X, Check, RotateCcw, ShieldCheck } from 'lucide-react';
import './Chat.css';

interface Message {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: Date;
    proposal?: IntegrationProposal;
}

interface IntegrationProposal {
    id: string;
    title: string;
    status: string;
    research?: { summary?: string };
    plan?: Array<{ phase: string; detail: string }>;
    safety?: { chat_only?: boolean; auto_apply?: boolean };
    validation?: { result?: { configured?: boolean; requirement?: string } } | null;
}

interface ChatProps {
    messages: Message[];
    onSendMessage: (text: string, hiddenContext?: string) => void;
    onProposalFeedback: (proposalId: string, approved: boolean) => void;
    onProposalValidate: (proposalId: string) => void;
}

interface AttachedFile {
    filename: string;
    text: string;
}

export default function Chat({
    messages,
    onSendMessage,
    onProposalFeedback,
    onProposalValidate,
}: ChatProps) {
    const [input, setInput] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const [attachedFile, setAttachedFile] = useState<AttachedFile | null>(null);
    const [isUploading, setIsUploading] = useState(false);

    const messagesEndRef = useRef<HTMLDivElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    const handleFileUpload = async (e: ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setIsUploading(true);
        const formData = new FormData();
        formData.append('file', file);

        try {
            const response = await fetch('/api/documents/upload', {
                method: 'POST',
                body: formData,
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Upload failed');
            }

            const data = await response.json();
            setAttachedFile({
                filename: data.filename,
                text: data.text
            });
        } catch (error) {
            console.error('File upload error:', error);
            // Ideally show toast error here
            alert('Failed to upload file: ' + (error as Error).message);
        } finally {
            setIsUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    const removeFile = () => {
        setAttachedFile(null);
    };

    const handleSubmit = async (e: FormEvent) => {
        e.preventDefault();
        if ((!input.trim() && !attachedFile) || isLoading || isUploading) return;

        const text = input.trim();
        const fileContext = attachedFile ? attachedFile.text : undefined;

        // Clear state immediately
        setInput('');
        setAttachedFile(null);
        setIsLoading(true);

        await onSendMessage(text, fileContext);
        setIsLoading(false);
    };

    return (
        <div className="chat">
            <div className="chat-messages">
                {messages.length === 0 ? (
                    <div className="chat-empty">
                        <motion.div
                            initial={{ opacity: 0, scale: 0.9 }}
                            animate={{ opacity: 1, scale: 1 }}
                            className="chat-empty-content"
                        >
                            <h2>Hello! I'm Saksham</h2>
                            <p>Your personal cognitive assistant. Ask me anything or give me a task.</p>

                            <div className="chat-suggestions">
                                <button onClick={() => onSendMessage("Open Visual Studio Code")}>
                                    Open VS Code
                                </button>
                                <button onClick={() => onSendMessage("What's on my schedule today?")}>
                                    Check Schedule
                                </button>
                                <button onClick={() => onSendMessage("Start a new project")}>
                                    New Project
                                </button>
                            </div>
                        </motion.div>
                    </div>
                ) : (
                    <AnimatePresence initial={false}>
                        {messages.map((message) => (
                            <motion.div
                                key={message.id}
                                initial={{ opacity: 0, y: 10 }}
                                animate={{ opacity: 1, y: 0 }}
                                exit={{ opacity: 0, y: -10 }}
                                className={`chat-message ${message.role}`}
                            >
                                <div className="message-content">
                                    {message.content}
                                </div>
                                {message.proposal && (
                                    <section className="integration-proposal-card" aria-label="Integration proposal">
                                        <div className="integration-proposal-heading">
                                            <div>
                                                <span className="integration-eyebrow">Integration Lab</span>
                                                <h3>{message.proposal.title}</h3>
                                            </div>
                                            <span className={`proposal-status ${message.proposal.status}`}>
                                                {message.proposal.status.replace(/_/g, ' ')}
                                            </span>
                                        </div>
                                        {message.proposal.research?.summary && (
                                            <p>{message.proposal.research.summary}</p>
                                        )}
                                        {message.proposal.plan && (
                                            <ol className="integration-plan">
                                                {message.proposal.plan.map(step => (
                                                    <li key={step.phase}>
                                                        <strong>{step.phase}</strong>
                                                        <span>{step.detail}</span>
                                                    </li>
                                                ))}
                                            </ol>
                                        )}
                                        <div className="integration-safety-note">
                                            <ShieldCheck size={15} />
                                            <span>Chat-only review. No code or external action runs automatically.</span>
                                        </div>
                                        {message.proposal.validation?.result && (
                                            <p className="integration-validation">
                                                {message.proposal.validation.result.configured
                                                    ? 'Configuration detected. The next step is an authorized playback test.'
                                                    : message.proposal.validation.result.requirement}
                                            </p>
                                        )}
                                        <div className="integration-actions">
                                            <button
                                                type="button"
                                                className="integration-action-primary"
                                                onClick={() => onProposalFeedback(message.proposal!.id, true)}
                                            >
                                                <Check size={15} /> Looks right
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => onProposalFeedback(message.proposal!.id, false)}
                                            >
                                                <RotateCcw size={15} /> Needs changes
                                            </button>
                                            <button
                                                type="button"
                                                onClick={() => onProposalValidate(message.proposal!.id)}
                                            >
                                                Check setup
                                            </button>
                                        </div>
                                    </section>
                                )}
                                <div className="message-time">
                                    {message.timestamp.toLocaleTimeString([], {
                                        hour: '2-digit',
                                        minute: '2-digit'
                                    })}
                                </div>
                            </motion.div>
                        ))}
                    </AnimatePresence>
                )}
                <div ref={messagesEndRef} />
            </div>

            <div className="chat-input-container">
                {attachedFile && (
                    <motion.div
                        initial={{ opacity: 0, y: 10 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: 10 }}
                        className="attached-file-badge"
                    >
                        <FileText size={14} className="file-icon" />
                        <span className="filename">{attachedFile.filename}</span>
                        <button onClick={removeFile} className="remove-file-btn">
                            <X size={14} />
                        </button>
                    </motion.div>
                )}

                <form className="chat-input-form" onSubmit={handleSubmit}>
                    <input
                        type="file"
                        ref={fileInputRef}
                        onChange={handleFileUpload}
                        style={{ display: 'none' }}
                        accept=".txt,.md,.py,.js,.ts,.tsx,.json,.pdf"
                    />

                    <button
                        type="button"
                        className="chat-attach-btn"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={isLoading || isUploading}
                        title="Attach file"
                    >
                        {isUploading ? <Loader2 size={18} className="spin" /> : <Paperclip size={18} />}
                    </button>

                    <input
                        type="text"
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        placeholder="Ask Saksham anything..."
                        className="chat-input"
                        disabled={isLoading}
                    />
                    <button
                        type="submit"
                        className="chat-send-btn"
                        disabled={(!input.trim() && !attachedFile) || isLoading || isUploading}
                    >
                        {isLoading ? (
                            <Loader2 size={18} className="spin" />
                        ) : (
                            <Send size={18} />
                        )}
                    </button>
                </form>
            </div>
        </div>
    );
}
