import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import { expect, test, type Route } from '@playwright/test';
import { shouldCleanupAcceptanceData } from './acceptance-data';

const sourceVideo = process.env.MVP_ACCEPTANCE_VIDEO;
const expectedPromptVersion = process.env.MVP_ACCEPTANCE_PROMPT_VERSION ?? 'stage0-summary-v5';
const expectedWebUrl = process.env.MVP_WEB_URL ?? `http://127.0.0.1:${process.env.VIDEOCLIP_WEB_PORT ?? '5174'}`;
const uuidPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/gi;
const repoRoot = resolve(process.cwd(), '..');
const generatedAccounts = new Map<string, string>();

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
}

function deferred(): Deferred {
  let resolve: () => void = () => undefined;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

type TaskState = {
  status: string;
  summary_status: string;
  execution_stop_confirmed: boolean | null;
  recovery_eligible: boolean;
  current_attempt_id: string;
  completed_segments: number;
  expected_segments: number;
  asr_request_count: number;
  asr_elapsed_seconds: number;
  reused_segments: number;
  asr_model: string;
  config_sha256: string;
  source_fingerprint: string;
  auto_recovery_count: number | null;
  auto_recovery_limit: number | null;
  delivery_retry_count: number | null;
  delivery_retry_limit: number | null;
  summary_requests_used: number;
  summary_requests_limit: number;
  attempt: { id: string; parent_attempt_id: string | null; execution_mode: string };
};

function stage2Command(...args: string[]): string {
  return execFileSync(
    'docker',
    [
      'compose', '--env-file', 'data/mvp-stage-2/.env', '-p', 'videoclip-mvp-stage2',
      '-f', 'compose.yaml', ...args,
    ],
    { cwd: repoRoot, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
  ).trim();
}

function readSummaryRequestAudit(taskId: string) {
  if (!/^[0-9a-f-]{36}$/i.test(taskId)) throw new Error('Invalid task identifier for acceptance audit.');
  const sql = `SELECT concat_ws('|', count(*)::text, coalesce(round(sum(elapsed_seconds)::numeric, 3)::text, '0'), coalesce(string_agg(status, ',' ORDER BY started_at), ''), coalesce(max(model), ''), coalesce(max(prompt_version), ''), coalesce(string_agg(error_code, ',' ORDER BY started_at) FILTER (WHERE status = 'failed'), ''), coalesce(string_agg(concat_ws(':', request_number::text, status, coalesce(error_code, '-'), coalesce(round(elapsed_seconds::numeric, 3)::text, 'unknown'), coalesce(finish_reason, '-'), coalesce(response_bytes::text, '-'), coalesce(usage->>'prompt_tokens', '-'), coalesce(usage->>'completion_tokens', '-')), ',' ORDER BY started_at), '')) FROM summary_requests WHERE task_id = '${taskId}'::uuid`;
  const output = stage2Command(
    'exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-qAt',
    '-U', 'videoclip_stage2', '-d', 'videoclip_mvp_stage2', '-c', sql,
  );
  const [count, elapsed, statuses, model, promptVersion, failureCodes, requestDetails] = output.split('|');
  return {
    count: Number(count),
    elapsedSeconds: Number(elapsed),
    statuses: statuses ? statuses.split(',') : [],
    failureCodes: failureCodes ? failureCodes.split(',') : [],
    requestDetails: requestDetails ? requestDetails.split(',').map((entry) => {
      const [number, status, errorCode, requestElapsed, finishReason, responseBytes, promptTokens, completionTokens] = entry.split(':');
      return {
        number: Number(number),
        status,
        errorCode: errorCode === '-' ? null : errorCode,
        elapsedSeconds: Number(requestElapsed),
        finishReason: finishReason === '-' ? null : finishReason,
        responseBytes: responseBytes === '-' ? null : Number(responseBytes),
        promptTokens: promptTokens === '-' ? null : Number(promptTokens),
        completionTokens: completionTokens === '-' ? null : Number(completionTokens),
      };
    }) : [],
    model,
    promptVersion,
  };
}

function readTaskExecutionAudit(taskId: string) {
  if (!/^[0-9a-f-]{36}$/i.test(taskId)) throw new Error('Invalid task identifier for acceptance audit.');
  const sql = `SELECT concat_ws('|',
    (SELECT count(DISTINCT config_sha256)::text FROM analysis_attempts WHERE task_id = '${taskId}'::uuid),
    (SELECT string_agg(DISTINCT config_snapshot->'asr'->>'model', ',') FROM analysis_attempts WHERE task_id = '${taskId}'::uuid),
    (SELECT string_agg(DISTINCT config_snapshot->'summary'->>'model', ',') FROM analysis_attempts WHERE task_id = '${taskId}'::uuid),
    (SELECT count(DISTINCT source_fingerprint)::text FROM analysis_attempts WHERE task_id = '${taskId}'::uuid),
    (SELECT coalesce(sum(s.request_count), 0)::text FROM transcript_segments s JOIN analysis_attempts a ON a.id = s.attempt_id WHERE a.task_id = '${taskId}'::uuid),
    (SELECT count(*)::text FROM analysis_summaries WHERE task_id = '${taskId}'::uuid),
    (SELECT concat_ws(',', coalesce(auto_recovery_count, 0)::text, coalesce(auto_recovery_limit, 0)::text, coalesce(delivery_retry_count, 0)::text, coalesce(delivery_retry_limit, 0)::text, coalesce(summary_requests_used, 0)::text, coalesce(summary_requests_limit, 0)::text) FROM analysis_tasks WHERE id = '${taskId}'::uuid))`;
  const output = stage2Command(
    'exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-qAt',
    '-U', 'videoclip_stage2', '-d', 'videoclip_mvp_stage2', '-c', sql,
  );
  const [configurationCount, asrModels, summaryModels, sourceCount, asrRequestCount, summaryCount, budgets] = output.split('|');
  const [automaticRecoveries, automaticRecoveryLimit, deliveryRetries, deliveryRetryLimit, summaryRequests, summaryRequestLimit] = (budgets ?? '').split(',').map(Number);
  return {
    frozenConfigurationCount: Number(configurationCount),
    asrModels: asrModels?.split(',') ?? [],
    summaryModels: summaryModels?.split(',') ?? [],
    sourceCount: Number(sourceCount),
    asrRequestCount: Number(asrRequestCount),
    summaryCount: Number(summaryCount),
    budgets: { automaticRecoveries, automaticRecoveryLimit, deliveryRetries, deliveryRetryLimit, summaryRequests, summaryRequestLimit },
  };
}

function readResumableVideoAudit(videoId: string, sourceDigest: string) {
  if (!/^[0-9a-f-]{36}$/i.test(videoId)) throw new Error('Invalid video identifier for upload acceptance audit.');
  if (!/^[0-9a-f]{64}$/i.test(sourceDigest)) throw new Error('Invalid source digest for upload acceptance audit.');
  const sql = `SELECT concat_ws('|', u.state, coalesce(u.declared_total_bytes, 0)::text,
    u.confirmed_offset::text, u.expected_content_sha256, v.byte_count::text, v.content_sha256,
    coalesce(round(v.duration_seconds::numeric, 3)::text, '0'),
    (SELECT count(*)::text FROM videos WHERE upload_id = v.upload_id),
    (u.expected_content_sha256 = '${sourceDigest}')::text)
    FROM videos v JOIN upload_handoffs u ON u.id = v.upload_id WHERE v.id = '${videoId}'::uuid`;
  const output = stage2Command(
    'exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-qAt',
    '-U', 'videoclip_stage2', '-d', 'videoclip_mvp_stage2', '-c', sql,
  );
  const [state, declaredBytes, confirmedOffset, expectedSha256, videoBytes, videoSha256, duration, videoCount, browserDigestMatchesServer] = output.split('|');
  return {
    state,
    declaredBytes: Number(declaredBytes),
    confirmedOffset: Number(confirmedOffset),
    digestMatches: expectedSha256 === videoSha256,
    videoBytes: Number(videoBytes),
    durationSeconds: Number(duration),
    videosForUpload: Number(videoCount),
    browserDigestMatchesServer: browserDigestMatchesServer === 't',
  };
}

async function readTask(page: import('@playwright/test').Page, videoId: string, taskId: string): Promise<TaskState> {
  return page.evaluate(async ({ id, analysisId }) => {
    const response = await fetch(`/api/videos/${id}/tasks/${analysisId}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Task read failed with HTTP ${response.status}.`);
    return await response.json() as TaskState;
  }, { id: videoId, analysisId: taskId });
}

async function waitForStage2Health(): Promise<void> {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch('http://127.0.0.1:58002/health', { signal: AbortSignal.timeout(2_000) });
      if (response.status === 200) return;
    } catch {
      // The API container may need a few seconds to restart.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 1_000));
  }
  throw new Error('Stage 2 API did not become healthy after the acceptance restart.');
}

async function cleanupGeneratedAccount(page: import('@playwright/test').Page, username: string, password: string): Promise<void> {
  await page.context().clearCookies();
  await loginAccount(page, username, password);
  const videos = await page.evaluate(async () => {
    const response = await fetch('/api/videos', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Test-account video list failed with HTTP ${response.status}.`);
    return (await response.json() as { items: Array<{ id: string }> }).items;
  });
  for (const video of videos) {
    const initial = await page.evaluate(async (id) => {
      const csrf = await (await fetch('/api/auth/csrf', { cache: 'no-store' })).json() as { csrf_token: string };
      const response = await fetch(`/api/videos/${id}`, {
        method: 'DELETE', headers: { 'X-CSRF-Token': csrf.csrf_token },
      });
      return response.status;
    }, video.id);
    if (![202, 204, 404].includes(initial)) throw new Error(`Test video deletion returned HTTP ${initial}.`);
    if (initial === 202) {
      let deleted = false;
      for (let attempt = 0; attempt < 300; attempt += 1) {
        const state = await page.evaluate(async (id) => {
          const response = await fetch(`/api/videos/${id}/deletion`, { cache: 'no-store' });
          if (response.status === 404) return 'deleted';
          if (!response.ok) throw new Error(`Deletion progress returned HTTP ${response.status}.`);
          return (await response.json() as { state: string }).state;
        }, video.id);
        if (state === 'deleted') { deleted = true; break; }
        if (state === 'cleanup_failed') {
          const retry = await page.evaluate(async (id) => {
            const csrf = await (await fetch('/api/auth/csrf', { cache: 'no-store' })).json() as { csrf_token: string };
            return (await fetch(`/api/videos/${id}`, {
              method: 'DELETE', headers: { 'X-CSRF-Token': csrf.csrf_token },
            })).status;
          }, video.id);
          if (![202, 204, 404].includes(retry)) throw new Error(`Test video cleanup retry returned HTTP ${retry}.`);
        }
        await page.waitForTimeout(2_000);
      }
      if (!deleted) throw new Error('Test video deletion did not complete safely; the task data was retained.');
    }
  }
  await page.context().clearCookies();
  const python = resolve(repoRoot, 'data', 'mvp-stage-2', '.venv', 'Scripts', 'python.exe');
  execFileSync(python, [
    'scripts/stage2/stage5-recovery-fixture.py', 'cleanup-account', '--username', username,
  ], {
    cwd: repoRoot,
    env: { ...process.env, DB_HOST: '127.0.0.1', DB_PORT: '55443', PYTHONPATH: resolve(repoRoot, 'src') },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
}

test.afterEach(async ({ page, baseURL }, testInfo) => {
  if (!baseURL) return;
  if (!shouldCleanupAcceptanceData(testInfo.status)) {
    console.error('stage6_browser_failure_data_retained=1');
    return;
  }
  for (const [username, password] of generatedAccounts) {
    await page.goto(baseURL);
    await cleanupGeneratedAccount(page, username, password);
  }
  generatedAccounts.clear();
});

function responsePath(path: string): string {
  return path.replace(uuidPattern, '{id}');
}

function clockSeconds(value: string): number {
  const parts = value.split(':').map(Number);
  return parts.reduce((seconds, part) => seconds * 60 + part, 0);
}

function citationSeconds(label: string): number | null {
  const match = label.match(/约 ([0-9:]+)/)?.[1];
  return match ? clockSeconds(match) : null;
}

async function createAccount(page: import('@playwright/test').Page, username: string, password: string) {
  generatedAccounts.set(username, password);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByRole('button', { name: '创建账户', exact: true }).click();
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '创建账户', exact: true }).click();
  await expect(page.getByText('账户已创建，请登录继续。')).toBeVisible();
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
}

async function loginAccount(page: import('@playwright/test').Page, username: string, password: string) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
}

test('same-origin registration, resumable long upload, real processing, playback, account isolation, and deletion', async ({ page, baseURL }, testInfo) => {
  test.skip(!sourceVideo, 'Set MVP_ACCEPTANCE_VIDEO to the authorized course sample for the real browser flow.');
  test.setTimeout(1_800_000);
  expect(baseURL).toBe(expectedWebUrl);
  const sourceStat = await stat(sourceVideo!);
  page.on('dialog', (dialog) => void dialog.accept());

  const entropy = randomBytes(6).toString('hex');
  const primaryUser = `stage5fixture-${entropy}`;
  const secondaryUser = `stage5fixture-${randomBytes(6).toString('hex')}`;
  const password = `VideoClip-${randomBytes(16).toString('hex')}!`;
  const requests: Array<{ path: string; method: string; status: number; contentType: string; origin: string | null; csrfPresent: boolean; cookiePresent: boolean }> = [];
  const sessionCookieWasHttpOnly: boolean[] = [];

  page.on('requestfinished', async (request) => {
    const url = new URL(request.url());
    if (url.origin !== baseURL || !url.pathname.startsWith('/api/')) return;
    try {
      const response = await request.response();
      if (!response) return;
      const headers = await request.allHeaders();
      requests.push({
        path: responsePath(url.pathname),
        method: request.method(),
        status: response.status(),
        contentType: headers['content-type'] ?? '',
        origin: headers.origin ?? null,
        csrfPresent: Boolean(headers['x-csrf-token']),
        cookiePresent: Boolean(headers.cookie),
      });
    } catch {
      // A navigation can close the request context before its metadata callback runs.
    }
  });
  page.on('response', async (response) => {
    if (!response.url().endsWith('/api/auth/login')) return;
    const setCookie = (await response.allHeaders())['set-cookie'] ?? '';
    sessionCookieWasHttpOnly.push(/videoclip_session=[^;]+;[^\r\n]*httponly/i.test(setCookie));
  });

  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('anonymous-login.png') });
  await createAccount(page, primaryUser, password);
  await expect(page.getByText(primaryUser)).toBeVisible();
  const csrfRejection = await page.evaluate(async () => {
    const rejected = await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': 'invalid-fixture-token' } });
    const session = await fetch('/api/auth/me', { cache: 'no-store' });
    return { rejected: rejected.status, session: session.status };
  });
  expect(csrfRejection).toEqual({ rejected: 403, session: 200 });
  const uploadStartedAt = Date.now();
  const chunkPaused = deferred();
  const continueChunk = deferred();
  let uploadId = '';
  let heldChunk = false;
  await page.route(/\/api\/uploads\/[^/]+\/chunks\/\d+$/, async (route: Route) => {
    const match = new URL(route.request().url()).pathname.match(/\/uploads\/([^/]+)\/chunks\/(\d+)$/);
    if (match && match[1] === uploadId && match[2] === '1' && !heldChunk) {
      heldChunk = true;
      chunkPaused.resolve();
      await continueChunk.promise;
      try { await route.abort(); } catch { /* The UI may already have cancelled this request. */ }
      return;
    }
    await route.continue();
  });
  const firstCreatePromise = page.waitForResponse((response) => response.url().endsWith('/api/uploads') && response.request().method() === 'POST');
  await page.locator('#video-file').setInputFiles(sourceVideo!);
  await page.getByRole('button', { name: /上传 \/ 继续上传/ }).click();
  const firstCreate = await firstCreatePromise;
  expect(firstCreate.status()).toBe(201);
  uploadId = (await firstCreate.json() as { upload_id: string }).upload_id;
  const sourceDigest = (firstCreate.request().postDataJSON() as { sha256: string }).sha256;
  const idempotencyKey = (await firstCreate.request().allHeaders())['idempotency-key'];
  expect(idempotencyKey).toBeTruthy();
  await chunkPaused.promise;
  await expect(page.getByText(/服务端已确认 8\.0 MiB/)).toBeVisible({ timeout: 60_000 });
  const interruptedUpload = await page.evaluate(async (id) => {
    const response = await fetch(`/api/uploads/${id}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Resumable status returned HTTP ${response.status}.`);
    return await response.json() as { state: string; confirmed_offset: number };
  }, uploadId);
  expect(interruptedUpload).toMatchObject({ state: 'receiving', confirmed_offset: 8_388_608 });
  await page.getByRole('button', { name: '暂停上传' }).click();
  continueChunk.resolve();
  await expect(page.getByText(/上传已暂停/)).toBeVisible({ timeout: 30_000 });
  await expect.poll(async () => (await page.evaluate(async (id) => {
    const response = await fetch(`/api/uploads/${id}`, { cache: 'no-store' });
    return (await response.json() as { confirmed_offset: number }).confirmed_offset;
  }, uploadId))).toBe(8_388_608);

  await page.reload();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/此账户有 1 个临时上传/)).toBeVisible();
  const resumePromise = page.waitForResponse((response) => response.url().endsWith('/api/uploads') && response.request().method() === 'POST');
  await page.locator('#video-file').setInputFiles(sourceVideo!);
  await page.getByRole('button', { name: /上传 \/ 继续上传/ }).click();
  const resumedCreate = await resumePromise;
  expect(resumedCreate.status()).toBe(200);
  expect(await resumedCreate.json()).toEqual(expect.objectContaining({ upload_id: uploadId, replayed: true, confirmed_offset: 8_388_608 }));
  await expect(page).toHaveURL(/\/videos\/[0-9a-f-]{36}$/, { timeout: 80 * 60 * 1000 });
  const uploadElapsedSeconds = (Date.now() - uploadStartedAt) / 1000;
  const videoId = new URL(page.url()).pathname.split('/').at(-1)!;
  await page.unroute(/\/api\/uploads\/[^/]+\/chunks\/\d+$/);
  const replayedCompletion = await page.evaluate(async (id) => {
    const csrf = await (await fetch('/api/auth/csrf', { cache: 'no-store' })).json() as { csrf_token: string };
    const response = await fetch(`/api/uploads/${id}/complete`, {
      method: 'POST', headers: { 'X-CSRF-Token': csrf.csrf_token }, cache: 'no-store',
    });
    const payload = await response.json() as { video: { id: string } };
    return { status: response.status, videoId: payload.video.id };
  }, uploadId);
  expect(replayedCompletion).toEqual({ status: 200, videoId });
  const detailResponse = await page.evaluate(async (id) => {
    const response = await fetch(`/api/videos/${id}`, { cache: 'no-store' });
    return { status: response.status, video: await response.json() };
  }, videoId);
  expect(detailResponse.status).toBe(200);
  const uploadResult = { video: detailResponse.video as { id: string; byte_count: number; display_name: string; analysis_task_id: string | null } };
  expect(videoId).toBe(uploadResult.video.id);
  expect(uploadResult.video.byte_count).toBeGreaterThan(0);
  expect(uploadResult.video.analysis_task_id ?? null).toBeNull();
  const uploadAudit = readResumableVideoAudit(videoId, sourceDigest);
  expect(uploadAudit.state).toBe('complete');
  expect(uploadAudit.declaredBytes).toBe(sourceStat.size);
  expect(uploadAudit.confirmedOffset).toBe(sourceStat.size);
  expect(uploadAudit.videoBytes).toBe(sourceStat.size);
  expect(uploadAudit.digestMatches).toBe(true);
  expect(uploadAudit.browserDigestMatchesServer).toBe(true);
  expect(uploadAudit.videosForUpload).toBe(1);
  expect(uploadAudit.durationSeconds).toBeGreaterThan(1061.2);
  expect(uploadAudit.durationSeconds).toBeLessThan(1061.5);
  const accountVideos = await page.evaluate(async () => {
    const response = await fetch('/api/videos', { cache: 'no-store' });
    return (await response.json() as { items: Array<{ id: string }> }).items;
  });
  expect(accountVideos.filter((item) => item.id === videoId)).toHaveLength(1);
  await expect(page.getByRole('heading', { name: uploadResult.video.display_name })).toBeVisible({ timeout: 20_000 });

  const analysisStartedAt = Date.now();
  const taskResponsePromise = page.waitForResponse((response) => response.url().includes('/api/videos/') && response.url().endsWith('/tasks') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始分析' }).click();
  const taskResponse = await taskResponsePromise;
  expect(taskResponse.status()).toBe(202);
  const task = await taskResponse.json() as { id: string; current_attempt_id: string };

  await expect.poll(async () => page.evaluate(async ({ id, taskId }) => {
    const response = await fetch(`/api/videos/${id}/tasks/${taskId}`, { cache: 'no-store' });
    if (!response.ok) return `http-${response.status}`;
    const current = await response.json() as { status: string; summary_status: string; execution_state: string };
    return `${current.status}:${current.summary_status}:${current.execution_state}`;
  }, { id: videoId, taskId: task.id }), { timeout: 1_200_000, intervals: [2000, 3000, 5000] })
    .toMatch(/^(awaiting_summary:waiting:stopped|failed:failed:stopped|succeeded:succeeded:stopped)$/);
  const checkpoint = await page.evaluate(async ({ id, taskId }) => {
    const response = await fetch(`/api/videos/${id}/tasks/${taskId}`, { cache: 'no-store' });
    return { status: response.status, body: await response.json() };
  }, { id: videoId, taskId: task.id }) as { status: number; body: { status: string; summary_status: string; execution_state: string; execution_stop_confirmed: boolean; completed_segments: number; expected_segments: number } };
  expect(checkpoint.status).toBe(200);
  expect(checkpoint.body.execution_stop_confirmed).toBe(true);
  expect(['awaiting_summary', 'failed', 'succeeded']).toContain(checkpoint.body.status);
  if (checkpoint.body.status === 'failed') expect(checkpoint.body.summary_status).toBe('failed');
  expect(checkpoint.body.completed_segments).toBe(checkpoint.body.expected_segments);
  await expect(page.locator('.transcript-row').first()).toBeVisible();
  const summaryDeadline = Date.now() + 18 * 60_000;
  let recoveredSummaryAttempts = 0;
  let summaryStarted = false;
  let finalTask = await readTask(page, videoId, task.id);
  while (finalTask.status !== 'succeeded' || finalTask.summary_status !== 'succeeded') {
    if (Date.now() >= summaryDeadline) throw new Error('Summary acceptance exceeded its bounded deadline.');
    if (finalTask.status === 'awaiting_summary' && finalTask.summary_status === 'waiting') {
      if (!summaryStarted) {
        await page.getByRole('button', { name: '继续生成总结' }).click();
        summaryStarted = true;
      }
    } else if (finalTask.status === 'failed' && finalTask.summary_status === 'failed') {
      if (finalTask.execution_stop_confirmed !== true) {
        await page.waitForTimeout(2_000);
        finalTask = await readTask(page, videoId, task.id);
        continue;
      }
      const audit = readSummaryRequestAudit(task.id);
      expect(audit.count).toBeLessThanOrEqual(3);
      expect(finalTask.execution_stop_confirmed).toBe(true);
      if (audit.count >= 3 || !finalTask.recovery_eligible) {
        throw new Error(`Summary recovery stopped safely at ${audit.count} task-level requests.`);
      }
      const parentAttemptId = finalTask.current_attempt_id;
      await page.reload();
      await expect(page.getByRole('button', { name: '手动恢复分析' })).toBeVisible({ timeout: 15_000 });
      await page.getByRole('button', { name: '手动恢复分析' }).click();
      await expect.poll(async () => (await readTask(page, videoId, task.id)).current_attempt_id, { timeout: 30_000 })
        .not.toBe(parentAttemptId);
      const child = await readTask(page, videoId, task.id);
      expect(child.attempt.parent_attempt_id).toBe(parentAttemptId);
      expect(child.attempt.execution_mode).toBe('summary_only');
      recoveredSummaryAttempts += 1;
      summaryStarted = true;
    } else if (finalTask.status !== 'queued' && finalTask.status !== 'running') {
      throw new Error(`Summary stopped in ${finalTask.status}:${finalTask.summary_status}.`);
    }
    await page.waitForTimeout(2_000);
    finalTask = await readTask(page, videoId, task.id);
  }
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });

  const summaryAudit = readSummaryRequestAudit(task.id);
  expect(summaryAudit.count).toBeGreaterThan(0);
  expect(summaryAudit.count).toBeLessThanOrEqual(3);
  expect(summaryAudit.statuses.at(-1)).toBe('succeeded');
  if (summaryAudit.failureCodes.some((code) => ['LLM_OUTPUT_TRUNCATED', 'LLM_PHASE_TIMEOUT', 'LLM_TOTAL_TIMEOUT'].includes(code))) {
    expect(recoveredSummaryAttempts).toBeGreaterThan(0);
  }
  expect(summaryAudit.model).toBe('Qwen/Qwen3.5-4B');
  expect(summaryAudit.promptVersion).toBe(expectedPromptVersion);
  const executionAudit = readTaskExecutionAudit(task.id);
  expect(executionAudit.frozenConfigurationCount).toBe(1);
  expect(executionAudit.asrModels).toEqual(['FunAudioLLM/SenseVoiceSmall']);
  expect(executionAudit.summaryModels).toEqual(['Qwen/Qwen3.5-4B']);
  expect(executionAudit.sourceCount).toBe(1);
  expect(executionAudit.asrRequestCount).toBeGreaterThanOrEqual(finalTask.expected_segments);
  expect(executionAudit.summaryCount).toBe(1);
  expect(executionAudit.budgets.automaticRecoveries).toBeLessThanOrEqual(executionAudit.budgets.automaticRecoveryLimit);
  expect(executionAudit.budgets.deliveryRetries).toBeLessThanOrEqual(executionAudit.budgets.deliveryRetryLimit);
  expect(executionAudit.budgets.summaryRequests).toBeLessThanOrEqual(executionAudit.budgets.summaryRequestLimit);
  if (recoveredSummaryAttempts > 0) {
    expect(finalTask.asr_request_count).toBe(0);
    expect(finalTask.reused_segments).toBe(finalTask.expected_segments);
  }

  const citationAudit = await page.evaluate(async (id) => {
    const [summaryResponse, transcriptResponse] = await Promise.all([
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
    ]);
    if (!summaryResponse.ok || !transcriptResponse.ok) throw new Error('Published result readback failed.');
    const summary = await summaryResponse.json() as {
      payload: { chapters: Array<{ evidence: Array<{ segment_id: number; quote: string; start_seconds: number; end_seconds: number }> }>; key_points: Array<{ evidence: Array<{ segment_id: number; quote: string; start_seconds: number; end_seconds: number }> }> };
    };
    const transcript = await transcriptResponse.json() as {
      items: Array<{ segment_number: number; text: string | null; video_start_seconds: number; video_end_seconds: number }>;
    };
    const bySegment = new Map(transcript.items.map((item) => [item.segment_number, item]));
    const evidence = [...summary.payload.chapters, ...summary.payload.key_points].flatMap((item) => item.evidence);
    const normalize = (value: string) => value.normalize('NFKC').toLowerCase().replace(/\s+/gu, '');
    const quoteMatches = evidence.filter((item) => {
      const source = bySegment.get(item.segment_id)?.text;
      return source !== undefined && source !== null && normalize(source).includes(normalize(item.quote));
    }).length;
    const timeMatches = evidence.filter((item) => {
      const source = bySegment.get(item.segment_id);
      return source !== undefined && item.start_seconds === source.video_start_seconds && item.end_seconds === source.video_end_seconds;
    }).length;
    return {
      chapters: summary.payload.chapters.length,
      keyPoints: summary.payload.key_points.length,
      citations: evidence.length,
      quoteMatches,
      timeMatches,
    };
  }, videoId);
  expect(citationAudit.citations).toBeGreaterThan(0);
  expect(citationAudit.quoteMatches).toBe(citationAudit.citations);
  expect(citationAudit.timeMatches).toBe(citationAudit.citations);

  if (process.env.MVP_STAGE6_RESTART_SERVICES === '1') {
    const persisted = await readTask(page, videoId, task.id);
    expect(persisted.status).toBe('succeeded');
    expect(persisted.execution_stop_confirmed).toBe(true);
    stage2Command('restart', '--timeout', '30', 'api', 'worker');
    await waitForStage2Health();
  }
  await page.reload();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });
  const taskAfterReload = await readTask(page, videoId, task.id);
  expect(taskAfterReload.status).toBe('succeeded');
  expect(taskAfterReload.completed_segments).toBe(taskAfterReload.expected_segments);
  expect(taskAfterReload.execution_stop_confirmed).toBe(true);
  const timelineAfterReload = await page.evaluate(async (id) => {
    const response = await fetch(`/api/videos/${id}/events?limit=100`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Persisted event readback failed with HTTP ${response.status}.`);
    return await response.json() as { items: Array<{ event_type: string; request_reference: string | null }> };
  }, videoId);
  const persistedEventTypes = timelineAfterReload.items.map((event) => event.event_type);
  for (const requiredEvent of ['analysis_submitted', 'execution_claimed', 'checkpoint_saved', 'transcription_completed', 'summary_requested', 'summary_published', 'stop_confirmed']) {
    expect(persistedEventTypes).toContain(requiredEvent);
  }
  console.log(`stage7_real_browser_audit=${JSON.stringify({
    uploadElapsedSeconds: Number(uploadElapsedSeconds.toFixed(3)),
    analysisElapsedSeconds: Number(((Date.now() - analysisStartedAt) / 1000).toFixed(3)),
    asrRequests: taskAfterReload.asr_request_count,
    asrElapsedSeconds: Number(taskAfterReload.asr_elapsed_seconds.toFixed(3)),
    completedSegments: taskAfterReload.completed_segments,
    expectedSegments: taskAfterReload.expected_segments,
    summaryRequests: summaryAudit.count,
    summaryElapsedSeconds: summaryAudit.elapsedSeconds,
    summaryStatuses: summaryAudit.statuses,
    summaryFailureCodes: summaryAudit.failureCodes,
    summaryRequestDetails: summaryAudit.requestDetails,
    model: summaryAudit.model,
    promptVersion: summaryAudit.promptVersion,
    recoveredSummaryAttempts,
    reusedSegments: taskAfterReload.reused_segments,
    uploadAudit,
    timelineEventCount: timelineAfterReload.items.length,
    timelineRequestReferences: timelineAfterReload.items.filter((event) => event.request_reference).length,
    executionAudit,
    ...citationAudit,
    apiWorkerRestartReadback: process.env.MVP_STAGE6_RESTART_SERVICES === '1',
  })}`);
  const citeButtons = page.locator('button.cite-button');
  await expect(citeButtons.first()).toBeVisible();
  const labels = await citeButtons.evaluateAll((buttons) => buttons.map((button) => button.getAttribute('aria-label') ?? ''));
  const firstIndex = labels.findIndex((label) => citationSeconds(label) !== null);
  const firstSeconds = firstIndex >= 0 ? citationSeconds(labels[firstIndex]!) : null;
  const secondIndex = labels.findIndex((label, index) => index !== firstIndex && citationSeconds(label) !== null && Math.abs(citationSeconds(label)! - (firstSeconds ?? 0)) > 1);
  expect(firstIndex).toBeGreaterThanOrEqual(0);
  expect(secondIndex).toBeGreaterThanOrEqual(0);

  const firstTarget = citationSeconds(labels[firstIndex]!)!;
  await citeButtons.nth(firstIndex).click();
  const player = page.locator('video');
  await expect.poll(() => player.evaluate((element: HTMLVideoElement) => !element.paused), { timeout: 15_000 }).toBe(true);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, target: number) => Math.abs(element.currentTime - target) < 2.5, firstTarget), { timeout: 10_000 }).toBe(true);
  const firstPlaybackPosition = await player.evaluate((element: HTMLVideoElement) => element.currentTime);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, start: number) => !element.paused && element.currentTime > start + 0.2, firstPlaybackPosition), { timeout: 15_000 }).toBe(true);

  const secondTarget = citationSeconds(labels[secondIndex]!)!;
  await citeButtons.nth(secondIndex).click();
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, target: number) => Math.abs(element.currentTime - target) < 2.5, secondTarget), { timeout: 10_000 }).toBe(true);
  const secondPlaybackPosition = await player.evaluate((element: HTMLVideoElement) => element.currentTime);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, start: number) => !element.paused && element.currentTime > start + 0.2, secondPlaybackPosition), { timeout: 15_000 }).toBe(true);
  expect(Math.abs(secondTarget - firstTarget)).toBeGreaterThan(1);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement) => !element.paused), { timeout: 10_000 }).toBe(true);

  const mediaChecks = await page.evaluate(async (id) => {
    const head = await fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' });
    const range = await fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-15' }, cache: 'no-store' });
    return {
      head: { status: head.status, length: head.headers.get('content-length') },
      range: { status: range.status, contentRange: range.headers.get('content-range'), bytes: (await range.arrayBuffer()).byteLength },
    };
  }, videoId);
  expect(mediaChecks.head.status).toBe(200);
  expect(Number(mediaChecks.head.length)).toBe(uploadResult.video.byte_count);
  expect(mediaChecks.range.status).toBe(206);
  expect(mediaChecks.range.bytes).toBe(16);
  expect(mediaChecks.range.contentRange).toContain(`/${uploadResult.video.byte_count}`);
  const invalidRange = await page.evaluate(async ({ id, byteCount }) => {
    const response = await fetch(`/api/videos/${id}/content`, { headers: { Range: `bytes=${byteCount}-` }, cache: 'no-store' });
    return { status: response.status, contentRange: response.headers.get('content-range') };
  }, { id: videoId, byteCount: uploadResult.video.byte_count });
  expect(invalidRange.status).toBe(416);
  expect(invalidRange.contentRange).toBe(`bytes */${uploadResult.video.byte_count}`);

  await page.context().clearCookies();
  await page.reload();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('video')).toHaveCount(0);
  const expiredSessionStatus = await page.evaluate(async (id) => {
    const [me, content] = await Promise.all([
      fetch('/api/auth/me', { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
    ]);
    return { me: me.status, content: content.status };
  }, videoId);
  expect(expiredSessionStatus).toEqual({ me: 401, content: 401 });
  await loginAccount(page, primaryUser, password);
  await page.goto(`/videos/${videoId}`);
  await expect(page.locator('video')).toBeVisible({ timeout: 20_000 });

  const logoutPrimary = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logoutPrimary).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);
  const anonymousStatus = await page.evaluate(async (id) => {
    const me = await fetch('/api/auth/me', { cache: 'no-store' });
    const content = await fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
    return { me: me.status, content: content.status };
  }, videoId);
  expect(anonymousStatus).toEqual({ me: 401, content: 401 });

  await createAccount(page, secondaryUser, password);
  const isolation = await page.evaluate(async ({ id, taskId }) => {
    const requests = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/tasks/${taskId}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/events`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
    ]);
    return requests.map((response) => response.status);
  }, { id: videoId, taskId: task.id });
  expect(isolation).toEqual([404, 404, 404, 404, 404, 404]);

  const logoutSecondary = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logoutSecondary).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await loginAccount(page, primaryUser, password);

  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: uploadResult.video.display_name })).toBeVisible({ timeout: 20_000 });
  const removeMediaPromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}/media`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除原视频，保留笔记' }).click();
  const removeMediaResponse = await removeMediaPromise;
  expect(removeMediaResponse.status()).toBe(200);
  await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible({ timeout: 60_000 });
  await expect(page.locator('video')).toHaveCount(0);
  const retainedNotesAndClosedMedia = await page.evaluate(async ({ id, taskId }) => {
    const [video, taskRead, transcript, summary, events, mediaGet, mediaHead, mediaRange] = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/tasks/${taskId}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/events?limit=100`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-15' }, cache: 'no-store' }),
    ]);
    const videoBody = await video.json() as { media_status: string };
    const transcriptBody = await transcript.json() as { complete: boolean; items: unknown[] };
    const summaryBody = await summary.json() as { payload: { chapters: unknown[]; key_points: unknown[] } };
    const eventsBody = await events.json() as { items: Array<{ event_type: string }> };
    return {
      videoStatus: video.status,
      mediaStatus: videoBody.media_status,
      taskStatus: taskRead.status,
      transcriptStatus: transcript.status,
      transcriptComplete: transcriptBody.complete,
      transcriptSegments: transcriptBody.items.length,
      summaryStatus: summary.status,
      chapters: summaryBody.payload.chapters.length,
      keyPoints: summaryBody.payload.key_points.length,
      eventStatus: events.status,
      eventTypes: eventsBody.items.map((event) => event.event_type),
      mediaStatuses: [mediaGet.status, mediaHead.status, mediaRange.status],
    };
  }, { id: videoId, taskId: task.id });
  expect(retainedNotesAndClosedMedia.videoStatus).toBe(200);
  expect(retainedNotesAndClosedMedia.mediaStatus).toBe('deleted');
  expect(retainedNotesAndClosedMedia.taskStatus).toBe(200);
  expect(retainedNotesAndClosedMedia.transcriptStatus).toBe(200);
  expect(retainedNotesAndClosedMedia.transcriptComplete).toBe(true);
  expect(retainedNotesAndClosedMedia.transcriptSegments).toBeGreaterThan(0);
  expect(retainedNotesAndClosedMedia.summaryStatus).toBe(200);
  expect(retainedNotesAndClosedMedia.chapters + retainedNotesAndClosedMedia.keyPoints).toBeGreaterThan(0);
  expect(retainedNotesAndClosedMedia.eventStatus).toBe(200);
  expect(retainedNotesAndClosedMedia.eventTypes).toContain('media_cleanup_completed');
  expect(retainedNotesAndClosedMedia.mediaStatuses).toEqual([410, 410, 410]);
  await page.reload();
  await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible();
  await expect(page.locator('.transcript-row').first()).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);

  const deleteResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除记录' }).click();
  const deleteResponse = await deleteResponsePromise;
  expect([202, 204]).toContain(deleteResponse.status());
  if (deleteResponse.status() === 202) {
    await expect(page.getByRole('heading', { name: '正在删除这段视频' })).toBeVisible();
  }
  await expect(page).toHaveURL(/\/$/, { timeout: 60_000 });
  await expect(page.getByText(uploadResult.video.display_name)).toHaveCount(0);
  const deletedContentStatus = await page.evaluate(async ({ id, taskId }) => {
    const responses = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/tasks/${taskId}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/events`, { cache: 'no-store' }),
    ]);
    return responses.map((response) => response.status);
  }, { id: videoId, taskId: task.id });
  expect(deletedContentStatus).toEqual([404, 404, 404, 404, 404, 404, 404]);

  expect(sessionCookieWasHttpOnly.some(Boolean)).toBe(true);
  expect(requests.some((entry) => entry.path === '/api/auth/me' && entry.status === 200 && entry.cookiePresent)).toBe(true);
  expect(requests.every((entry) => entry.origin === null || entry.origin === baseURL)).toBe(true);
  expect(requests.filter((entry) => ['POST', 'PATCH', 'PUT', 'DELETE'].includes(entry.method)).every((entry) => entry.origin === baseURL && entry.csrfPresent)).toBe(true);
  const usedApiPaths = [...new Set(requests.map((entry) => entry.path))].sort();
  expect(usedApiPaths).toContain('/api/auth/csrf');
  expect(usedApiPaths).toContain('/api/videos');
});
