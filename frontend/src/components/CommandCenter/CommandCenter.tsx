import { useId } from 'react';
import {
    Activity,
    AlertTriangle,
    Ban,
    Check,
    CheckCircle2,
    ChevronRight,
    Circle,
    CircleDotDashed,
    Clock3,
    LockKeyhole,
    Play,
    RefreshCw,
    RotateCcw,
    ShieldAlert,
    ShieldCheck,
    X,
    XCircle,
    type LucideIcon,
} from 'lucide-react';
import './CommandCenter.css';

/**
 * The policy level assigned by Saksham's deterministic action policy.
 * The component only presents the level; it must never be used as the policy
 * decision itself.
 */
export type CommandCenterApproval = 'none' | 'confirm' | 'admin' | 'blocked';

export type CommandCenterExecutionStatus =
    | 'draft'
    | 'pending'
    | 'queued'
    | 'running'
    | 'waiting_for_confirmation'
    | 'waiting_for_admin'
    | 'completed'
    | 'failed'
    | 'cancelled'
    | 'blocked'
    | (string & {});

export type CommandCenterStepStatus =
    | 'pending'
    | 'queued'
    | 'running'
    | 'awaiting_approval'
    | 'completed'
    | 'failed'
    | 'skipped'
    | 'blocked'
    | (string & {});

export type CommandCenterActivityKind =
    | 'info'
    | 'debug'
    | 'success'
    | 'warning'
    | 'error'
    | 'security';

export type CommandCenterStep = {
    id?: string;
    title?: string;
    description?: string;
    status?: CommandCenterStepStatus;
    action?: string;
    target?: string;
    evidence?: string;
};

export type CommandCenterProgress = {
    completed: number;
    total: number;
    label?: string;
};

export type CommandCenterTask = {
    id: string;
    title: string;
    description?: string;
    status: CommandCenterExecutionStatus;
    /** `approvalLevel` is accepted as a friendlier alias for API adapters. */
    approval?: CommandCenterApproval;
    approvalLevel?: CommandCenterApproval;
    approval_level?: CommandCenterApproval;
    approvalReason?: string;
    approval_reason?: string;
    riskScore?: number;
    risk_score?: number;
    steps?: CommandCenterStep[];
    progress?: CommandCenterProgress;
    updatedAt?: string;
    updated_at?: string;
    resultSummary?: string;
    result_summary?: string;
    error?: string;
};

export type CommandCenterActivity = {
    id: string;
    title: string;
    detail?: string;
    timestamp?: string;
    kind?: CommandCenterActivityKind;
};

export type CommandCenterProps = {
    /** The task currently being planned or executed. */
    task?: CommandCenterTask | null;
    /**
     * A task collection returned by the Command Center API. When `task` is not
     * supplied, the highest-priority active task is displayed automatically.
     */
    tasks?: readonly CommandCenterTask[];
    /** Newest first. This is intentionally independent from `task` for easy polling. */
    activity?: CommandCenterActivity[];
    /** Disables controls while a parent request is in flight. */
    isBusy?: boolean;
    /** Limits displayed activity without mutating the supplied collection. */
    activityLimit?: number;
    className?: string;
    onStart?: (task: CommandCenterTask) => void;
    onConfirm?: (task: CommandCenterTask) => void;
    onRequestAdminActivation?: (task: CommandCenterTask) => void;
    onRetry?: (task: CommandCenterTask) => void;
    /** Convenience callbacks for API-backed containers. */
    onApprove?: (taskId: string) => void;
    onReject?: (taskId: string) => void;
    onCancel?: (taskId: string) => void;
    onRefresh?: () => void;
};

type Tone = 'neutral' | 'active' | 'success' | 'warning' | 'danger' | 'security';

type StatusMeta = {
    label: string;
    tone: Tone;
    Icon: LucideIcon;
};

const STATUS_META: Record<string, StatusMeta> = {
    draft: { label: 'Draft plan', tone: 'neutral', Icon: Circle },
    pending: { label: 'Pending', tone: 'neutral', Icon: Clock3 },
    planning: { label: 'Preparing plan', tone: 'active', Icon: CircleDotDashed },
    queued: { label: 'Queued', tone: 'neutral', Icon: Clock3 },
    running: { label: 'Executing now', tone: 'active', Icon: CircleDotDashed },
    waiting_for_confirmation: { label: 'Awaiting your confirmation', tone: 'warning', Icon: AlertTriangle },
    awaiting_confirmation: { label: 'Awaiting your confirmation', tone: 'warning', Icon: AlertTriangle },
    awaiting_approval: { label: 'Awaiting your confirmation', tone: 'warning', Icon: AlertTriangle },
    waiting_for_admin: { label: 'Awaiting admin activation', tone: 'security', Icon: LockKeyhole },
    awaiting_admin_activation: { label: 'Awaiting admin activation', tone: 'security', Icon: LockKeyhole },
    cancelling: { label: 'Cancelling', tone: 'warning', Icon: CircleDotDashed },
    completed: { label: 'Completed', tone: 'success', Icon: CheckCircle2 },
    failed: { label: 'Needs attention', tone: 'danger', Icon: XCircle },
    cancelled: { label: 'Cancelled', tone: 'neutral', Icon: Ban },
    blocked: { label: 'Blocked by policy', tone: 'danger', Icon: Ban },
};

const APPROVAL_META: Record<CommandCenterApproval, StatusMeta & { description: string }> = {
    none: {
        label: 'Routine action',
        tone: 'success',
        Icon: ShieldCheck,
        description: 'This plan stays within routine, pre-approved capabilities.',
    },
    confirm: {
        label: 'Confirmation needed',
        tone: 'warning',
        Icon: AlertTriangle,
        description: 'Review the plan before Saksham changes anything personal.',
    },
    admin: {
        label: 'Admin activation needed',
        tone: 'security',
        Icon: LockKeyhole,
        description: 'A live voice check and your admin code are required before execution.',
    },
    blocked: {
        label: 'Blocked by policy',
        tone: 'danger',
        Icon: ShieldAlert,
        description: 'Saksham will not execute this action under the current safety policy.',
    },
};

const STEP_META: Record<string, StatusMeta> = {
    pending: { label: 'Pending', tone: 'neutral', Icon: Circle },
    planning: { label: 'Preparing', tone: 'active', Icon: CircleDotDashed },
    queued: { label: 'Queued', tone: 'neutral', Icon: Clock3 },
    running: { label: 'In progress', tone: 'active', Icon: CircleDotDashed },
    awaiting_approval: { label: 'Needs approval', tone: 'warning', Icon: AlertTriangle },
    awaiting_confirmation: { label: 'Needs approval', tone: 'warning', Icon: AlertTriangle },
    completed: { label: 'Verified', tone: 'success', Icon: Check },
    failed: { label: 'Failed', tone: 'danger', Icon: X },
    skipped: { label: 'Skipped', tone: 'neutral', Icon: ChevronRight },
    blocked: { label: 'Blocked', tone: 'danger', Icon: Ban },
};

const ACTIVITY_ICON: Record<string, LucideIcon> = {
    info: Activity,
    debug: Activity,
    success: CheckCircle2,
    warning: AlertTriangle,
    error: XCircle,
    security: LockKeyhole,
};

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled', 'blocked']);

function prettify(value: string): string {
    return value.replace(/[_-]/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusMeta(status: string): StatusMeta {
    return STATUS_META[status] ?? {
        label: prettify(status),
        tone: 'neutral',
        Icon: Circle,
    };
}

function stepMeta(status: string): StatusMeta {
    return STEP_META[status] ?? {
        label: prettify(status),
        tone: 'neutral',
        Icon: Circle,
    };
}

function resolveProgress(task: CommandCenterTask): CommandCenterProgress {
    if (task.progress) {
        return {
            completed: Math.max(0, task.progress.completed),
            total: Math.max(0, task.progress.total),
            label: task.progress.label,
        };
    }

    const steps = task.steps ?? [];
    return {
        completed: steps.filter((step) => ['completed', 'skipped'].includes(step.status ?? 'pending')).length,
        total: steps.length,
    };
}

function resolveApproval(task: CommandCenterTask): CommandCenterApproval {
    return task.approval ?? task.approvalLevel ?? task.approval_level ?? 'none';
}

function stepTitle(step: CommandCenterStep, index: number): string {
    return step.title || step.description || (step.action ? prettify(step.action) : `Step ${index + 1}`);
}

function resolveActiveTask(
    explicitTask: CommandCenterTask | null | undefined,
    tasks: readonly CommandCenterTask[],
): CommandCenterTask | null {
    if (explicitTask) return explicitTask;
    if (tasks.length === 0) return null;

    const priority = ['running', 'awaiting_admin_activation', 'waiting_for_admin', 'awaiting_confirmation', 'waiting_for_confirmation', 'awaiting_approval', 'planning', 'queued', 'pending'];
    for (const status of priority) {
        const candidate = tasks.find((item) => item.status === status);
        if (candidate) return candidate;
    }
    return tasks[0] ?? null;
}

function formatActivityTime(value?: string): string {
    if (!value) return 'Just now';
    const timestamp = new Date(value).getTime();
    if (Number.isNaN(timestamp)) return value;

    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 15) return 'Just now';
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(timestamp);
}

function riskLabel(score?: number): string {
    if (typeof score !== 'number') return 'Policy assessed';
    if (score >= 8) return 'High impact';
    if (score >= 5) return 'Personal change';
    return 'Low impact';
}

function primaryAction(
    task: CommandCenterTask,
    approval: CommandCenterApproval,
    handlers: Pick<CommandCenterProps, 'onStart' | 'onConfirm' | 'onRequestAdminActivation' | 'onRetry' | 'onApprove'>,
): { label: string; Icon: LucideIcon; handler?: (task: CommandCenterTask) => void; tone: 'primary' | 'admin' | 'retry' } | null {
    if (task.status === 'failed') {
        return { label: 'Try again', Icon: RotateCcw, handler: handlers.onRetry, tone: 'retry' };
    }
    if (TERMINAL_STATUSES.has(task.status) || approval === 'blocked') return null;
    if (['planning', 'running', 'cancelling'].includes(task.status)) return null;

    if (approval === 'admin') {
        return {
            label: 'Start admin activation',
            Icon: LockKeyhole,
            handler: handlers.onRequestAdminActivation
                ?? (handlers.onApprove ? (item) => handlers.onApprove?.(item.id) : handlers.onConfirm),
            tone: 'admin',
        };
    }
    if (approval === 'confirm') {
        return {
            label: 'Approve plan',
            Icon: Check,
            handler: handlers.onApprove ? (item) => handlers.onApprove?.(item.id) : handlers.onConfirm,
            tone: 'primary',
        };
    }
    return { label: 'Start task', Icon: Play, handler: handlers.onStart ?? handlers.onConfirm, tone: 'primary' };
}

/**
 * A presentational, policy-aware view of one active task and its audited activity.
 * It emits user intent through callbacks; execution and authorization stay in the parent.
 */
export default function CommandCenter({
    task: taskProp = null,
    tasks = [],
    activity = [],
    isBusy = false,
    activityLimit = 5,
    className = '',
    onStart,
    onConfirm,
    onRequestAdminActivation,
    onRetry,
    onApprove,
    onReject,
    onCancel,
    onRefresh,
}: CommandCenterProps) {
    const headingId = useId();
    const task = resolveActiveTask(taskProp, tasks);
    const visibleActivity = activity.slice(0, Math.max(0, activityLimit));
    const taskStatus = task ? statusMeta(task.status) : null;
    const approvalLevel = task ? resolveApproval(task) : null;
    const approval = approvalLevel ? APPROVAL_META[approvalLevel] : null;
    const progress = task ? resolveProgress(task) : null;
    const progressPercent = progress && progress.total > 0
        ? Math.min(100, Math.round((progress.completed / progress.total) * 100))
        : 0;
    const action = task && approvalLevel
        ? primaryAction(task, approvalLevel, { onStart, onConfirm, onRequestAdminActivation, onRetry, onApprove })
        : null;
    const canCancel = task
        ? !TERMINAL_STATUSES.has(task.status) && approvalLevel !== 'blocked'
        : false;
    const canReject = task && approvalLevel
        ? !TERMINAL_STATUSES.has(task.status) && ['confirm', 'admin'].includes(approvalLevel) && Boolean(onReject)
        : false;

    return (
        <section
            className={`command-center ${className}`.trim()}
            aria-labelledby={headingId}
            aria-busy={isBusy || task?.status === 'running'}
        >
            <header className="command-center__header">
                <div>
                    <span className="command-center__eyebrow"><Activity size={14} aria-hidden="true" /> Command center</span>
                    <h2 id={headingId}>Execution, made visible.</h2>
                    <p>Review what Saksham plans to do, approve only what matters, and keep every outcome accountable.</p>
                </div>
                {taskStatus && (
                    <span className={`command-center__status command-center__status--${taskStatus.tone}`} aria-live="polite">
                        <taskStatus.Icon size={15} aria-hidden="true" />
                        {taskStatus.label}
                    </span>
                )}
            </header>

            <div className="command-center__layout">
                <div className="command-center__main">
                    {task && approval && progress ? (
                        <>
                            <article className="command-center__task-card">
                                <div className="command-center__task-topline">
                                    <span className="command-center__task-id">Task {task.id}</span>
                                    <span className="command-center__risk-label">
                                        {riskLabel(task.riskScore ?? task.risk_score)}{typeof (task.riskScore ?? task.risk_score) === 'number' ? ` · ${task.riskScore ?? task.risk_score}/10` : ''}
                                    </span>
                                </div>
                                <h3>{task.title}</h3>
                                {task.description && <p className="command-center__task-description">{task.description}</p>}

                                <div className={`command-center__approval command-center__approval--${approval.tone}`}>
                                    <approval.Icon size={18} aria-hidden="true" />
                                    <div>
                                        <strong>{approval.label}</strong>
                                        <p>{task.approvalReason || task.approval_reason || approval.description}</p>
                                    </div>
                                </div>

                                <div className="command-center__progress" aria-label={`${progress.completed} of ${progress.total} steps complete`}>
                                    <div className="command-center__progress-label">
                                        <span>{progress.label || 'Plan progress'}</span>
                                        <strong>{progress.total > 0 ? `${progress.completed} / ${progress.total}` : 'Preparing plan'}</strong>
                                    </div>
                                    <div className="command-center__progress-track" aria-hidden="true">
                                        <span style={{ width: `${progressPercent}%` }} />
                                    </div>
                                </div>

                                {(task.resultSummary || task.result_summary || task.error) && (
                                    <div className={`command-center__outcome ${task.error ? 'command-center__outcome--error' : ''}`}>
                                        {task.error ? <XCircle size={16} aria-hidden="true" /> : <CheckCircle2 size={16} aria-hidden="true" />}
                                        <span>{task.error || task.resultSummary || task.result_summary}</span>
                                    </div>
                                )}

                                {(action || canReject || canCancel) && (
                                    <div className="command-center__actions">
                                        {action && (
                                            <button
                                                type="button"
                                                className={`command-center__button command-center__button--${action.tone}`}
                                                onClick={() => action.handler?.(task)}
                                                disabled={isBusy || !action.handler}
                                            >
                                                <action.Icon size={16} aria-hidden="true" />
                                                {action.label}
                                            </button>
                                        )}
                                        {canReject && (
                                            <button
                                                type="button"
                                                className="command-center__button command-center__button--quiet"
                                                onClick={() => onReject?.(task.id)}
                                                disabled={isBusy}
                                            >
                                                <Ban size={16} aria-hidden="true" />
                                                Reject plan
                                            </button>
                                        )}
                                        {canCancel && (
                                            <button
                                                type="button"
                                                className="command-center__button command-center__button--quiet"
                                                onClick={() => onCancel?.(task.id)}
                                                disabled={isBusy || !onCancel}
                                            >
                                                <X size={16} aria-hidden="true" />
                                                Cancel task
                                            </button>
                                        )}
                                    </div>
                                )}
                            </article>

                            <article className="command-center__plan-card">
                                <div className="command-center__section-heading">
                                    <div>
                                        <span>Execution plan</span>
                                        <h3>Every step, before it runs.</h3>
                                    </div>
                                    {task.steps && task.steps.length > 0 && <span>{task.steps.length} steps</span>}
                                </div>

                                {task.steps && task.steps.length > 0 ? (
                                    <ol className="command-center__steps">
                                        {task.steps.map((step, index) => {
                                            const metadata = stepMeta(step.status ?? 'pending');
                                            const title = stepTitle(step, index);
                                            const showDescription = Boolean(step.description && step.description !== title);
                                            return (
                                                <li
                                                    key={step.id || `${step.action || 'step'}-${index}`}
                                                    className={`command-center__step command-center__step--${metadata.tone}`}
                                                    aria-current={step.status === 'running' ? 'step' : undefined}
                                                >
                                                    <span className="command-center__step-index">{index + 1}</span>
                                                    <div className="command-center__step-content">
                                                        <div className="command-center__step-title-row">
                                                            <strong>{title}</strong>
                                                            <span className="command-center__step-status">
                                                                <metadata.Icon size={13} aria-hidden="true" />
                                                                {metadata.label}
                                                            </span>
                                                        </div>
                                                        {showDescription && <p>{step.description}</p>}
                                                        {(step.action || step.target) && (
                                                            <code>{[step.action, step.target].filter(Boolean).join(' · ')}</code>
                                                        )}
                                                        {step.evidence && (
                                                            <span className="command-center__evidence">
                                                                <CheckCircle2 size={13} aria-hidden="true" />
                                                                {step.evidence}
                                                            </span>
                                                        )}
                                                    </div>
                                                </li>
                                            );
                                        })}
                                    </ol>
                                ) : (
                                    <div className="command-center__plan-empty">
                                        <CircleDotDashed size={18} aria-hidden="true" />
                                        <p>Saksham is still preparing the individual execution steps.</p>
                                    </div>
                                )}
                            </article>
                        </>
                    ) : (
                        <article className="command-center__empty-state">
                            <span className="command-center__empty-icon"><ShieldCheck size={23} aria-hidden="true" /></span>
                            <div>
                                <h3>No active task</h3>
                                <p>When Saksham plans or runs an action, its approvals, progress, and proof will appear here.</p>
                            </div>
                        </article>
                    )}
                </div>

                <aside className="command-center__activity" aria-label="Recent activity">
                    <div className="command-center__section-heading">
                        <div>
                            <span>Activity log</span>
                            <h3>Recent evidence</h3>
                        </div>
                        {onRefresh ? (
                            <button
                                type="button"
                                className="command-center__refresh"
                                onClick={onRefresh}
                                disabled={isBusy}
                                aria-label="Refresh command center"
                                title="Refresh command center"
                            >
                                <RefreshCw size={15} aria-hidden="true" />
                            </button>
                        ) : <Activity size={17} aria-hidden="true" />}
                    </div>

                    {visibleActivity.length > 0 ? (
                        <ol className="command-center__activity-list">
                            {visibleActivity.map((item) => {
                                const kind = item.kind || 'info';
                                const Icon = ACTIVITY_ICON[kind] ?? Activity;
                                return (
                                    <li key={item.id} className={`command-center__activity-item command-center__activity-item--${kind}`}>
                                        <span className="command-center__activity-icon"><Icon size={14} aria-hidden="true" /></span>
                                        <div>
                                            <strong>{item.title}</strong>
                                            {item.detail && <p>{item.detail}</p>}
                                            <time dateTime={item.timestamp}>{formatActivityTime(item.timestamp)}</time>
                                        </div>
                                    </li>
                                );
                            })}
                        </ol>
                    ) : (
                        <div className="command-center__activity-empty">
                            <Clock3 size={18} aria-hidden="true" />
                            <p>Verified actions and decisions will be recorded here.</p>
                        </div>
                    )}
                </aside>
            </div>
        </section>
    );
}
