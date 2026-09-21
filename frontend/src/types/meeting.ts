export type CaptureState =
    | 'recording'
    | 'stopping'
    | 'processing'
    | 'completed'
    | 'failed'
    | 'interrupted';

export type AssistantState = 'silent_monitoring' | 'private_request';

export type Project = {
    id: string;
    name: string;
    slug: string;
    folder_path: string;
    created_at: string;
    updated_at: string;
};

export type Meeting = {
    id: string;
    project_id: string;
    project_name?: string;
    title: string;
    capture_state: CaptureState;
    assistant_state: AssistantState;
    consent_confirmed: boolean;
    expected_speaker_count?: number | null;
    voice_memory_consent_confirmed: boolean;
    voice_memory_consent_at?: string | null;
    voice_memory_consent_scope?: string | null;
    sample_rate: number;
    last_sequence: number;
    total_samples: number;
    transcribed_samples: number;
    pointer_samples: number;
    mom_path?: string | null;
    processing_error?: string | null;
    started_at: string;
    stopped_at?: string | null;
};

export type TranscriptSegment = {
    id: string;
    meeting_id: string;
    start_ms: number;
    end_ms: number;
    text: string;
    words?: Array<{
        start_ms: number;
        end_ms: number;
        word: string;
        probability?: number | null;
    }>;
    speaker_cluster: string;
    speaker_label: string;
    profile_id?: string | null;
    is_private: boolean;
    is_provisional: boolean;
    confidence?: number | null;
};

export type MeetingPointer = {
    id: string;
    meeting_id: string;
    category: string;
    content: string;
    start_ms: number;
    end_ms: number;
    source_segment_ids: string[];
    is_provisional: boolean;
    metadata?: Record<string, unknown>;
};

export type MomRevision = {
    id: string;
    meeting_id: string;
    content: string;
    structured: Record<string, unknown>;
    is_final: boolean;
    created_at: string;
};

export type VoiceProfile = {
    id: string;
    display_name: string;
    profile_type: 'owner' | 'participant';
    model_name: string;
    consent_confirmed: boolean;
    consent_at: string;
    revoked_at?: string | null;
};

export type MeetingPrivateMessage = {
    id: string;
    meeting_id: string;
    role: 'user' | 'assistant';
    command_type: string;
    status: string;
    text: string;
    anchor_ms: number;
    execution_id?: string | null;
    metadata?: Record<string, unknown>;
    created_at: string;
};

export type PendingSpeakerMapping = {
    meeting_id: string;
    speaker_ordinal: number;
    display_name: string;
    status: string;
    cluster_label?: string | null;
    profile_status?: string | null;
    error?: string | null;
    updated_at: string;
};

export type MeetingAudioAvailability = {
    available: boolean;
    duration_ms: number;
    expires_at?: string | null;
};

export type MeetingDetail = {
    meeting: Meeting;
    project: Project;
    segments: TranscriptSegment[];
    pointers: MeetingPointer[];
    private_notes: MeetingPointer[];
    mom?: MomRevision | null;
    speakers: Array<{
        meeting_id: string;
        cluster_label: string;
        display_name: string;
        profile_id?: string | null;
    }>;
    processing_jobs: Array<{
        id: string;
        meeting_id: string;
        kind: string;
        state: 'queued' | 'running' | 'completed' | 'failed';
        provider?: string | null;
        attempt?: number;
        metadata?: Record<string, unknown>;
        error?: string | null;
        created_at: string;
        updated_at: string;
    }>;
    private_messages: MeetingPrivateMessage[];
    pending_speaker_mappings: PendingSpeakerMapping[];
    audio: MeetingAudioAvailability;
};

export type MeetingEvent =
    | { type: 'meeting_ready'; meeting: Meeting; ack: number }
    | { type: 'audio_ack'; ack: number; total_samples: number }
    | { type: 'audio_nack'; expected: number; received: number }
    | { type: 'transcript_segment'; segment: TranscriptSegment }
    | { type: 'meeting_pointer'; pointer: MeetingPointer }
    | { type: 'private_response'; text: string }
    | { type: 'private_message'; message: MeetingPrivateMessage }
    | { type: 'private_pointer'; pointer: MeetingPointer }
    | { type: 'speaker_mapping_updated'; speaker_ordinal: number; display_name: string; profile_id?: string | null; profile_status: string }
    | { type: 'reprocessing_started'; expected_speaker_count?: number | null }
    | { type: 'reprocessing_completed'; success: boolean; meeting: Meeting; warnings?: string[]; error?: string }
    | { type: 'worker_fallback'; stage: string; provider: string; message: string }
    | { type: 'pending_confirmation'; status: 'awaiting_confirmation'; message: MeetingPrivateMessage; execution_id?: string | null }
    | { type: 'action_progress'; status: string; message: MeetingPrivateMessage; execution_id?: string | null }
    | { type: 'assistant_state'; state: AssistantState; text: string }
    | { type: 'processing_progress'; stage: string; state: 'running' | 'completed' | 'failed'; progress: number; message: string; error?: string | null }
    | { type: 'meeting_state'; state?: CaptureState; meeting?: Meeting; message?: string }
    | { type: 'meeting_completed'; meeting: Meeting; mom: MomRevision; segments: TranscriptSegment[]; pointers: MeetingPointer[]; warnings: string[] }
    | { type: 'meeting_failed'; meeting: Meeting; message: string }
    | { type: 'recoverable_error'; stage: string; message: string }
    | { type: 'pong' };
