import { describe, expect, it } from 'vitest';
import { deliveryWaitLabel, errorMessage, formatBytes, formatDuration, shouldPollTask, stageLabel, taskStatusLabel } from './model';
import type { AnalysisTask } from './types';

function task(overrides: Partial<AnalysisTask> = {}): AnalysisTask {
  return {
    id: 'task',
    video_id: 'video',
    current_attempt_id: 'attempt',
    notes_attempt_id: null,
    status: 'queued',
    stage: 'queued',
    delivery_status: 'pending',
    delivery_updated_at: '2026-10-02T00:00:00Z',
    delivery_error_code: null,
    delivery_operation_generation: 0,
    queue_entered_at: '2026-10-02T00:00:00Z',
    delivery_next_check_at: null,
    delivery_retry_count: 0,
    delivery_retry_limit: 8,
    delivery_deadline_at: null,
    delivery_wait_reason: 'queued_for_delivery',
    delivery_failure_code: null,
    model_wait_reason: null,
    model_wait_error_code: null,
    model_wait_stage: null,
    model_wait_until: null,
    model_wait_single_deadline_at: null,
    model_wait_seconds_used: 0,
    model_wait_total_limit_seconds: 259200,
    auto_recovery_count: 0,
    auto_recovery_limit: 2,
    auto_recovery_budget_exhausted: false,
    summary_requests_used: 0,
    summary_requests_limit: 3,
    summary_requests_exhausted: false,
    completed_segments: 0,
    expected_segments: 0,
    transcript_complete: false,
    asr_model: 'model',
    config_sha256: 'hash',
    source_fingerprint: 'hash',
    manifest_fingerprint: null,
    asr_request_count: 0,
    asr_elapsed_seconds: 0,
    asr_calls: [],
    reused_segments: 0,
    summary_status: 'not_started',
    execution_state: 'not_started',
    execution_stop_confirmed: null,
    failure_code: null,
    recovery_eligible: false,
    recovery_error_code: null,
    recovery_available_at: null,
    is_current_attempt: true,
    created_at: '2026-10-02T00:00:00Z',
    attempt: {
      id: 'attempt',
      attempt_number: 1,
      parent_attempt_id: null,
      status: 'queued',
      execution_mode: 'full',
      execution_id: null,
      first_claimed_at: null,
      stop_confirmed_at: null,
      cleanup_state: 'pending',
      cleanup_error_code: null,
      failure_code: null,
    },
    ...overrides,
  };
}

describe('presentation rules for backend facts', () => {
  it('polls only while the backend reports queued/running work', () => {
    expect(shouldPollTask(task({ status: 'queued' }))).toBe(true);
    expect(shouldPollTask(task({ status: 'running', summary_status: 'running' }))).toBe(true);
    expect(shouldPollTask(task({ status: 'awaiting_summary', summary_status: 'waiting' }))).toBe(false);
    expect(shouldPollTask(task({ status: 'failed', recovery_eligible: true }))).toBe(false);
    expect(shouldPollTask(task({ status: 'succeeded', summary_status: 'succeeded' }))).toBe(false);
  });

  it('keeps stable recovery errors actionable and localized', () => {
    expect(errorMessage('STOP_CONFIRMATION_REQUIRED')).toContain('停止');
    expect(errorMessage('TRANSCRIPT_SOURCE_MISMATCH')).toContain('历史转写来源校验未通过');
    expect(errorMessage('ATTEMPT_TEMP_CLEANUP_FAILED')).toContain('临时文件清理失败');
    expect(errorMessage('ATTEMPT_CLEANUP_NOT_ELIGIBLE')).toContain('尚未确认');
    expect(errorMessage('STORAGE_OPERATION_FAILED')).toContain('原视频');
    expect(errorMessage('LLM_AUTHENTICATION_FAILED')).toContain('鉴权失败');
    expect(errorMessage(null)).toBeNull();
    expect(errorMessage('ANALYSIS_DELIVERY_RETRIES_EXHAUSTED')).toContain('投递补偿次数已耗尽');
    expect(deliveryWaitLabel('stop_confirmation_required')).toContain('停止确认阻断');
  });

  it('maps known API states and formats media metadata without inventing percentages', () => {
    expect(taskStatusLabel('awaiting_summary')).toBe('转写已完成，等待总结');
    expect(stageLabel('transcribing')).toBe('正在转写');
    expect(formatDuration(368.9)).toBe('06:08');
    expect(formatDuration(3661)).toBe('1:01:01');
    expect(formatBytes(1_116_803_236)).toBe('1.0 GiB');
    expect(formatBytes(-1)).toBe('—');
  });
});
