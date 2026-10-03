import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { resolve } from 'node:path';
import { expect, test } from '@playwright/test';

const sourceVideo = process.env.MVP_ACCEPTANCE_VIDEO;
const uuidPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/gi;
const repoRoot = resolve(process.cwd(), '..');
const generatedAccounts = new Map<string, string>();

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
  const sql = `SELECT concat_ws('|', count(*)::text, coalesce(round(sum(elapsed_seconds)::numeric, 3)::text, '0'), coalesce(string_agg(status, ',' ORDER BY started_at), ''), coalesce(max(model), ''), coalesce(max(prompt_version), ''), coalesce(string_agg(error_code, ',' ORDER BY request_number) FILTER (WHERE status = 'failed'), '')) FROM summary_requests WHERE task_id = '${taskId}'::uuid`;
  const output = stage2Command(
    'exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-qAt',
    '-U', 'videoclip_stage2', '-d', 'videoclip_mvp_stage2', '-c', sql,
  );
  const [count, elapsed, statuses, model, promptVersion, failureCodes] = output.split('|');
  return {
    count: Number(count),
    elapsedSeconds: Number(elapsed),
    statuses: statuses ? statuses.split(',') : [],
    failureCodes: failureCodes ? failureCodes.split(',') : [],
    model,
    promptVersion,
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

test.afterEach(async ({ page, baseURL }) => {
  if (!baseURL) return;
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

test('same-origin registration, raw upload, real processing, playback, account isolation, and deletion', async ({ page, baseURL }) => {
  test.skip(!sourceVideo, 'Set MVP_ACCEPTANCE_VIDEO to the authorized course sample for the real browser flow.');
  test.setTimeout(1_800_000);
  expect(baseURL).toBe('http://127.0.0.1:5174');

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
  await page.screenshot({ path: 'test-results/stage5-anonymous-login.png' });
  await createAccount(page, primaryUser, password);
  await expect(page.getByText(primaryUser)).toBeVisible();
  const csrfRejection = await page.evaluate(async () => {
    const rejected = await fetch('/api/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': 'invalid-fixture-token' } });
    const session = await fetch('/api/auth/me', { cache: 'no-store' });
    return { rejected: rejected.status, session: session.status };
  });
  expect(csrfRejection).toEqual({ rejected: 403, session: 200 });
  const uploadResponsePromise = page.waitForResponse((response) => response.url().endsWith('/api/videos') && response.request().method() === 'POST');
  await page.getByLabel('视频文件').setInputFiles(sourceVideo!);
  await page.getByRole('button', { name: '上传视频' }).click();
  const uploadResponse = await uploadResponsePromise;
  expect(uploadResponse.status()).toBe(201);
  const uploadHeaders = await uploadResponse.request().allHeaders();
  expect(uploadHeaders['content-type'] ?? '').not.toContain('multipart/form-data');
  expect(uploadHeaders['x-video-filename']).toBeTruthy();
  expect(uploadHeaders.origin).toBe(baseURL);
  await expect(page).toHaveURL(/\/videos\/[0-9a-f-]{36}$/);
  const videoId = new URL(page.url()).pathname.split('/').at(-1)!;
  const detailResponse = await page.evaluate(async (id) => {
    const response = await fetch(`/api/videos/${id}`, { cache: 'no-store' });
    return { status: response.status, video: await response.json() };
  }, videoId);
  expect(detailResponse.status).toBe(200);
  const uploadResult = { video: detailResponse.video as { id: string; byte_count: number; display_name: string; analysis_task_id: string | null } };
  expect(videoId).toBe(uploadResult.video.id);
  expect(uploadResult.video.byte_count).toBeGreaterThan(0);
  expect(uploadResult.video.analysis_task_id ?? null).toBeNull();
  await expect(page.getByRole('heading', { name: uploadResult.video.display_name })).toBeVisible({ timeout: 20_000 });

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
  expect(summaryAudit.promptVersion).toBe('stage0-summary-v4');
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
  console.log(`stage6_browser_audit=${JSON.stringify({
    asrRequests: taskAfterReload.asr_request_count,
    asrElapsedSeconds: Number(taskAfterReload.asr_elapsed_seconds.toFixed(3)),
    completedSegments: taskAfterReload.completed_segments,
    summaryRequests: summaryAudit.count,
    summaryElapsedSeconds: summaryAudit.elapsedSeconds,
    summaryStatuses: summaryAudit.statuses,
    summaryFailureCodes: summaryAudit.failureCodes,
    model: summaryAudit.model,
    promptVersion: summaryAudit.promptVersion,
    recoveredSummaryAttempts,
    reusedSegments: taskAfterReload.reused_segments,
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
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
    ]);
    return requests.map((response) => response.status);
  }, { id: videoId, taskId: task.id });
  expect(isolation).toEqual([404, 404, 404, 404, 404]);

  const logoutSecondary = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logoutSecondary).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await loginAccount(page, primaryUser, password);

  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: uploadResult.video.display_name })).toBeVisible({ timeout: 20_000 });
  const deleteResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除记录' }).click();
  const deleteResponse = await deleteResponsePromise;
  expect([202, 204]).toContain(deleteResponse.status());
  if (deleteResponse.status() === 202) {
    await expect(page.getByRole('heading', { name: '正在删除这段视频' })).toBeVisible();
  }
  await expect(page).toHaveURL(/\/$/, { timeout: 60_000 });
  await expect(page.getByText(uploadResult.video.display_name)).toHaveCount(0);
  const deletedContentStatus = await page.evaluate(async (id) => {
    const responses = await Promise.all([
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
    ]);
    return responses.map((response) => response.status);
  }, videoId);
  expect(deletedContentStatus.every((status) => [404, 410].includes(status))).toBe(true);

  expect(sessionCookieWasHttpOnly.some(Boolean)).toBe(true);
  expect(requests.some((entry) => entry.path === '/api/auth/me' && entry.status === 200 && entry.cookiePresent)).toBe(true);
  expect(requests.every((entry) => entry.origin === null || entry.origin === baseURL)).toBe(true);
  expect(requests.filter((entry) => ['POST', 'PATCH', 'PUT', 'DELETE'].includes(entry.method)).every((entry) => entry.origin === baseURL && entry.csrfPresent)).toBe(true);
  const usedApiPaths = [...new Set(requests.map((entry) => entry.path))].sort();
  expect(usedApiPaths).toContain('/api/auth/csrf');
  expect(usedApiPaths).toContain('/api/videos');
});
