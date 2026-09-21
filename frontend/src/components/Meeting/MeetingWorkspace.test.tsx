import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import MeetingWorkspace from './MeetingWorkspace';

const project = {
    id: 'project-1',
    name: 'Client Alpha',
    slug: 'client-alpha',
    folder_path: '/tmp/client-alpha',
    created_at: '2026-08-12T10:00:00Z',
    updated_at: '2026-08-12T10:00:00Z',
};

const meeting = {
    id: 'meeting-1',
    project_id: project.id,
    project_name: project.name,
    title: 'Client meeting',
    capture_state: 'completed',
    assistant_state: 'silent_monitoring',
    consent_confirmed: true,
    expected_speaker_count: 2,
    voice_memory_consent_confirmed: true,
    sample_rate: 16000,
    last_sequence: 5,
    total_samples: 160000,
    transcribed_samples: 160000,
    pointer_samples: 160000,
    started_at: '2026-08-12T10:00:00Z',
    stopped_at: '2026-08-12T10:00:10Z',
};

function response(payload: unknown, ok = true) {
    return Promise.resolve({
        ok,
        status: ok ? 200 : 500,
        json: () => Promise.resolve(payload),
    } as Response);
}

describe('MeetingWorkspace', () => {
    beforeEach(() => {
        Object.defineProperty(HTMLMediaElement.prototype, 'play', {
            configurable: true,
            value: vi.fn().mockResolvedValue(undefined),
        });
        Object.defineProperty(HTMLMediaElement.prototype, 'pause', {
            configurable: true,
            value: vi.fn(),
        });
        vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
            const url = String(input);
            if (url.endsWith('/projects-root')) return response({ path: '/tmp/projects' });
            if (url.endsWith('/projects')) return response({ projects: [project] });
            if (url.includes('/voice-profiles/list')) return response({ profiles: [] });
            if (url === '/api/meetings?limit=100') return response({ meetings: [meeting] });
            if (url.endsWith('/private-commands') && init?.method === 'POST') {
                return response({ type: 'speaker_mapping', response: 'Speaker 1 is now Joshua.' });
            }
            if (url.endsWith('/reprocess') && init?.method === 'POST') {
                return response({ meeting: { ...meeting, capture_state: 'processing' } });
            }
            if (url.endsWith(`/meetings/${meeting.id}`)) {
                return response({
                    meeting,
                    project,
                    segments: [{
                        id: 'segment-1', meeting_id: meeting.id, start_ms: 1000, end_ms: 3000,
                        text: 'Welcome Joshua', speaker_cluster: 'SPEAKER_00', speaker_label: 'Speaker 1',
                        is_private: false, is_provisional: false,
                    }],
                    pointers: [],
                    private_notes: [],
                    private_messages: [{
                        id: 'private-1', meeting_id: meeting.id, role: 'assistant', command_type: 'response',
                        status: 'completed', text: 'Ready privately.', anchor_ms: 1000,
                        created_at: '2026-08-12T10:00:11Z',
                    }],
                    pending_speaker_mappings: [],
                    processing_jobs: [],
                    audio: { available: true, duration_ms: 10000, expires_at: '2026-09-11T10:00:10Z' },
                    speakers: [],
                    mom: null,
                });
            }
            return response({});
        }));
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('shows speaker-count and separate biometric consent controls', async () => {
        render(<MeetingWorkspace suspendAmbient={() => false} restoreAmbient={() => undefined} onRecordingChange={() => undefined} />);

        expect(await screen.findByLabelText('Expected speakers')).toBeInTheDocument();
        expect(screen.getByText(/separately consented to encrypted, reusable voice identification/i)).toBeInTheDocument();
    });

    it('loads the private console and anchors typed commands to a selected transcript line', async () => {
        render(<MeetingWorkspace suspendAmbient={() => false} restoreAmbient={() => undefined} onRecordingChange={() => undefined} />);

        fireEvent.click(await screen.findByRole('button', { name: /Client meeting/i }));
        expect(await screen.findByText('Ready privately.')).toBeInTheDocument();
        fireEvent.click(screen.getByText('Welcome Joshua'));
        fireEvent.change(screen.getByPlaceholderText(/Ask privately/i), { target: { value: 'Speaker 1 is Joshua' } });
        fireEvent.click(screen.getByRole('button', { name: 'Send privately' }));

        await waitFor(() => {
            const calls = vi.mocked(fetch).mock.calls;
            const commandCall = calls.find(([url]) => String(url).endsWith('/private-commands'));
            expect(commandCall).toBeDefined();
            expect(String(commandCall?.[1]?.body)).toContain('segment-1');
        });
    });

    it('offers timestamp playback controls and sends an exact re-diarization count', async () => {
        render(<MeetingWorkspace suspendAmbient={() => false} restoreAmbient={() => undefined} onRecordingChange={() => undefined} />);

        fireEvent.click(await screen.findByRole('button', { name: /Client meeting/i }));
        expect(await screen.findByRole('button', { name: 'Back 10 seconds' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Forward 10 seconds' })).toBeInTheDocument();
        expect(screen.getByRole('slider', { name: 'Meeting playback position' })).toBeInTheDocument();
        fireEvent.click(screen.getByRole('button', { name: 'Play' }));
        expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();

        fireEvent.change(screen.getByLabelText('Re-diarization speaker count'), { target: { value: '3' } });
        fireEvent.click(screen.getByRole('button', { name: /Re-diarize/i }));

        await waitFor(() => {
            const calls = vi.mocked(fetch).mock.calls;
            const reprocessCall = calls.find(([url]) => String(url).endsWith('/reprocess'));
            expect(reprocessCall).toBeDefined();
            expect(String(reprocessCall?.[1]?.body)).toContain('"expected_speaker_count":3');
        });
    });
});
