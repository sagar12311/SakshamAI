import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import CommandCenter, { type CommandCenterTask } from './CommandCenter';

const awaitingAdminTask: CommandCenterTask = {
    id: 'task-admin',
    title: 'Prepare the project handoff',
    description: 'Collect the current build, verify its status, and place a copy in the shared handoff folder.',
    status: 'awaiting_admin_activation',
    approval: 'admin',
    approvalReason: 'This will move files outside the current workspace.',
    riskScore: 8,
    steps: [
        {
            id: 'inspect',
            title: 'Inspect the current build',
            status: 'completed',
            evidence: 'The production build completed successfully.',
        },
        {
            id: 'copy',
            title: 'Copy the verified build',
            status: 'awaiting_approval',
            action: 'file_operation',
            target: '/tmp/saksham-fixture/Shared/Handoff',
        },
    ],
};

describe('CommandCenter', () => {
    it('selects the active task, exposes its policy state, progress, and audited activity', () => {
        const onApprove = vi.fn();
        const onCancel = vi.fn();
        const onRefresh = vi.fn();

        render(
            <CommandCenter
                tasks={[{ id: 'task-pending', title: 'Read notes', status: 'pending' }, awaitingAdminTask]}
                activity={[{
                    id: 'event-1',
                    title: 'Build verification completed',
                    detail: 'Evidence was recorded before the copy step started.',
                    kind: 'success',
                    timestamp: '2026-09-07T05:30:00Z',
                }]}
                onApprove={onApprove}
                onCancel={onCancel}
                onRefresh={onRefresh}
            />,
        );

        expect(screen.getByText('Prepare the project handoff')).toBeInTheDocument();
        expect(screen.getByText('Admin activation needed')).toBeInTheDocument();
        expect(screen.getByText('1 / 2')).toBeInTheDocument();
        expect(screen.getByText('Copy the verified build')).toBeInTheDocument();
        expect(screen.getByText('Build verification completed')).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: 'Start admin activation' }));
        expect(onApprove).toHaveBeenCalledWith('task-admin');

        fireEvent.click(screen.getByRole('button', { name: 'Cancel task' }));
        expect(onCancel).toHaveBeenCalledWith('task-admin');

        fireEvent.click(screen.getByRole('button', { name: 'Refresh command center' }));
        expect(onRefresh).toHaveBeenCalledOnce();
    });

    it('uses ID-based approval and rejection callbacks for a confirmation-gated plan', () => {
        const onApprove = vi.fn();
        const onReject = vi.fn();

        render(
            <CommandCenter
                task={{
                    id: 'task-confirm',
                    title: 'Create a local project note',
                    status: 'awaiting_confirmation',
                    approvalLevel: 'confirm',
                    steps: [{ action: 'file_operation', description: 'Write the note', status: 'awaiting_confirmation' }],
                }}
                onApprove={onApprove}
                onReject={onReject}
            />,
        );

        expect(screen.getByText('Confirmation needed')).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Approve plan' }));
        expect(onApprove).toHaveBeenCalledWith('task-confirm');

        fireEvent.click(screen.getByRole('button', { name: 'Reject plan' }));
        expect(onReject).toHaveBeenCalledWith('task-confirm');
        expect(screen.getByText('Write the note')).toBeInTheDocument();
    });

    it('shows a calm empty state while preserving the recent activity panel', () => {
        render(<CommandCenter activity={[]} />);

        expect(screen.getByText('No active task')).toBeInTheDocument();
        expect(screen.getByText('Recent evidence')).toBeInTheDocument();
        expect(screen.getByText(/Verified actions and decisions will be recorded here/i)).toBeInTheDocument();
    });
});
