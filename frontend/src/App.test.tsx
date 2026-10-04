import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('authentication entry', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/');
    window.localStorage.clear();
  });

  it('gets a short-lived CSRF value, registers through the same-origin API, and keeps it out of persistent browser storage', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'ephemeral-csrf' }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'authentication required' }, 401))
      .mockResolvedValueOnce(jsonResponse({ id: 'user-id', username: 'learner-1' }, 201));
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    await screen.findByRole('heading', { name: '欢迎回来' });
    fireEvent.click(screen.getByRole('button', { name: '创建账户' }));
    fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'learner-1' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'ValidPassphrase1!' } });
    fireEvent.click(screen.getByRole('button', { name: '创建账户' }));

    await screen.findByText('账户已创建，请登录继续。');
    const registration = fetchMock.mock.calls[2];
    expect(registration?.[0]).toBe('/api/auth/register');
    expect(registration?.[1]).toMatchObject({ method: 'POST', credentials: 'same-origin' });
    expect(new Headers((registration?.[1] as RequestInit).headers).get('X-CSRF-Token')).toBe('ephemeral-csrf');
    expect((registration?.[1] as RequestInit).body).toBe(JSON.stringify({ username: 'learner-1', password: 'ValidPassphrase1!' }));
    expect(window.localStorage.length).toBe(0);
  });
});

describe('published result cleanup separation', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/videos/video-id');
    window.localStorage.clear();
  });

  it('keeps a published summary readable and retries only temporary cleanup', async () => {
    let cleanupState = 'failed';
    const task = {
      id: 'task-id',
      video_id: 'video-id',
      current_attempt_id: 'attempt-id',
      status: 'succeeded',
      stage: 'succeeded',
      delivery_status: 'sent',
      delivery_updated_at: '2026-10-02T00:00:00Z',
      delivery_error_code: null,
      delivery_operation_generation: 1,
      queue_entered_at: '2026-10-02T00:00:00Z',
      delivery_next_check_at: null,
      delivery_retry_count: 0,
      delivery_retry_limit: 8,
      delivery_deadline_at: '2026-10-03T00:00:00Z',
      delivery_wait_reason: null,
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
      completed_segments: 1,
      expected_segments: 1,
      transcript_complete: true,
      asr_model: 'fixture-model',
      config_sha256: 'config-hash',
      source_fingerprint: 'source-hash',
      manifest_fingerprint: 'manifest-hash',
      asr_request_count: 1,
      asr_elapsed_seconds: 1,
      asr_calls: [],
      reused_segments: 0,
      summary_status: 'succeeded',
      execution_state: 'stopped',
      execution_stop_confirmed: true,
      failure_code: null,
      recovery_eligible: false,
      recovery_error_code: null,
      recovery_available_at: null,
      is_current_attempt: true,
      created_at: '2026-10-02T00:00:00Z',
      attempt: {
        id: 'attempt-id',
        attempt_number: 1,
        parent_attempt_id: null,
        status: 'succeeded',
        execution_mode: 'full',
        execution_id: 'execution-id',
        first_claimed_at: '2026-10-02T00:00:00Z',
        stop_confirmed_at: '2026-10-02T00:00:01Z',
        cleanup_state: 'failed',
        cleanup_error_code: 'ATTEMPT_TEMP_CLEANUP_FAILED',
        failure_code: null,
      },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'ephemeral-csrf' });
      if (path === '/api/auth/me') return jsonResponse({ id: 'user-id', username: 'learner-1' });
      if (path === '/api/videos/video-id') return jsonResponse({
        id: 'video-id', display_name: 'fixture-video.mp4', byte_count: 1024, content_sha256: 'media-hash',
        duration_seconds: 30, media_info: {}, status: 'ready', deletion_error: null,
        created_at: '2026-10-02T00:00:00Z', analysis_task_exists: true, analysis_task_id: 'task-id',
      });
      if (path === '/api/videos/video-id/tasks/task-id') return jsonResponse({
        ...task,
        attempt: { ...task.attempt, cleanup_state: cleanupState, cleanup_error_code: cleanupState === 'failed' ? 'ATTEMPT_TEMP_CLEANUP_FAILED' : null },
      });
      if (path === '/api/videos/video-id/transcript') return jsonResponse({
        task_id: 'task-id', complete: true, summary_status: 'succeeded',
        items: [{ segment_number: 0, source_attempt_id: 'attempt-id', audio_start_seconds: 0, audio_end_seconds: 1, actual_audio_seconds: 1, video_start_seconds: 0, video_end_seconds: 1, text: 'synthetic transcript', status: 'succeeded' }],
      });
      if (path === '/api/videos/video-id/summary') return jsonResponse({
        video_id: 'video-id', task_id: 'task-id', attempt_id: 'attempt-id', status: 'succeeded',
        structure_version: 'summary-v1', provider: 'fixture', model: 'fixture-model', prompt_version: 'fixture',
        prompt_sha256: 'prompt-hash', manifest_fingerprint: 'manifest-hash', transcript_fingerprint: 'transcript-hash',
        summary_sha256: 'summary-hash',
        payload: { chapters: [{ title: 'Synthetic chapter', evidence: [], start_seconds: 0, end_seconds: 1 }], key_points: [{ text: 'Synthetic summary remains available', evidence: [], start_seconds: 0, end_seconds: 1 }] },
        created_at: '2026-10-02T00:00:00Z',
      });
      if (path === '/api/videos/video-id/cleanup' && init?.method === 'POST') {
        cleanupState = 'cleaned';
        return jsonResponse({ video_id: 'video-id', attempt_id: 'attempt-id', cleanup_state: cleanupState, cleanup_error_code: null });
      }
      throw new Error(`Unexpected fixture API request: ${path}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    expect(await screen.findByText('Synthetic summary remains available')).toBeInTheDocument();
    expect(screen.getByText('临时文件清理失败。分析结果状态保持不变，可单独重试清理。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试清理' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: '重试清理' })).not.toBeInTheDocument());
    expect(screen.getByText('Synthetic summary remains available')).toBeInTheDocument();
    const writes = fetchMock.mock.calls
      .map(([input, init]) => ({ path: String(input), method: init?.method ?? 'GET' }))
      .filter((request) => request.method !== 'GET');
    expect(writes).toEqual([{ path: '/api/videos/video-id/cleanup', method: 'POST' }]);
  });
});
