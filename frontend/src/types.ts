export interface User {
  id: string;
  username: string;
}

export interface VideoSummary {
  id: string;
  display_name: string;
  byte_count: number;
  content_sha256: string;
  duration_seconds: number;
  media_info: Record<string, unknown>;
  status: string;
  deletion_error: string | null;
  media_status: 'available' | 'deletion_pending' | 'cleanup_failed' | 'deleted';
  media_error_code: string | null;
  created_at: string;
}

export interface VideoDetail extends VideoSummary {
  analysis_task_exists: boolean;
  analysis_task_id: string | null;
}

export interface VideoList {
  items: VideoSummary[];
}

export interface Attempt {
  id: string;
  attempt_number: number;
  parent_attempt_id: string | null;
  status: string;
  execution_mode: string;
  execution_id: string | null;
  first_claimed_at: string | null;
  stop_confirmed_at: string | null;
  cleanup_state: string;
  cleanup_error_code: string | null;
  failure_code: string | null;
}

export interface AnalysisTask {
  id: string;
  video_id: string;
  current_attempt_id: string;
  notes_attempt_id: string | null;
  status: string;
  stage: string;
  delivery_status: string;
  delivery_updated_at: string;
  delivery_error_code: string | null;
  delivery_operation_generation: number | null;
  queue_entered_at: string | null;
  delivery_next_check_at: string | null;
  delivery_retry_count: number | null;
  delivery_retry_limit: number | null;
  delivery_deadline_at: string | null;
  delivery_wait_reason: string | null;
  delivery_failure_code: string | null;
  model_wait_reason: string | null;
  model_wait_error_code: string | null;
  model_wait_stage: string | null;
  model_wait_until: string | null;
  model_wait_single_deadline_at: string | null;
  model_wait_seconds_used: number | null;
  model_wait_total_limit_seconds: number | null;
  auto_recovery_count: number | null;
  auto_recovery_limit: number | null;
  auto_recovery_budget_exhausted: boolean | null;
  summary_requests_used: number;
  summary_requests_limit: number;
  summary_requests_exhausted: boolean;
  completed_segments: number;
  expected_segments: number;
  transcript_complete: boolean;
  asr_model: string;
  config_sha256: string;
  source_fingerprint: string;
  manifest_fingerprint: string | null;
  asr_request_count: number;
  asr_elapsed_seconds: number;
  asr_calls: Record<string, unknown>[];
  reused_segments: number;
  summary_status: string;
  execution_state: string;
  execution_stop_confirmed: boolean | null;
  failure_code: string | null;
  recovery_eligible: boolean;
  recovery_error_code: string | null;
  recovery_available_at: string | null;
  is_current_attempt: boolean;
  created_at: string;
  attempt: Attempt;
}

export interface TranscriptSegment {
  segment_number: number;
  source_attempt_id: string | null;
  audio_start_seconds: number;
  audio_end_seconds: number;
  actual_audio_seconds: number;
  video_start_seconds: number;
  video_end_seconds: number;
  text: string | null;
  status: string;
}

export interface Transcript {
  task_id: string;
  complete: boolean;
  summary_status: string;
  items: TranscriptSegment[];
}

export interface Evidence {
  segment_id: number;
  quote: string;
  start_seconds: number;
  end_seconds: number;
}

export interface SummaryItem {
  title?: string;
  text?: string;
  evidence: Evidence[];
  start_seconds: number;
  end_seconds: number;
}

export interface Summary {
  video_id: string;
  task_id: string;
  attempt_id: string;
  status: string;
  structure_version: string;
  provider: string;
  model: string;
  prompt_version: string;
  prompt_sha256: string;
  manifest_fingerprint: string;
  transcript_fingerprint: string;
  summary_sha256: string;
  payload: {
    chapters: SummaryItem[];
    key_points: SummaryItem[];
  };
  created_at: string;
}

export interface DeletionProgress {
  video_id: string;
  state: string;
  error_code: string | null;
  pending_attempt_ids: string[];
  stop_confirmation_required: boolean;
}

export interface MediaDeletionProgress {
  video_id: string;
  media_status: 'available' | 'deletion_pending' | 'cleanup_failed' | 'deleted';
  error_code: string | null;
  pending_attempt_ids: string[];
  stop_confirmation_required: boolean;
}
