import { execFileSync } from 'node:child_process';
import { expect, test, type Page } from '@playwright/test';

const username = process.env.MVP_STAGE7_RESUME_USERNAME ?? '';
const password = process.env.MVP_STAGE7_RESUME_PASSWORD ?? '';
const videoId = process.env.MVP_STAGE7_RESUME_VIDEO_ID ?? '';
const taskId = process.env.MVP_STAGE7_RESUME_TASK_ID ?? '';
const repoRoot = process.cwd().replace(/[\\/]frontend$/, '');

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

async function readTask(page: Page) {
  return page.evaluate(async ({ id, analysisId }) => {
    const response = await fetch(`/api/videos/${id}/tasks/${analysisId}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Task read failed with HTTP ${response.status}.`);
    return await response.json() as {
      status: string;
      summary_status: string;
      execution_stop_confirmed: boolean | null;
      completed_segments: number;
      expected_segments: number;
      summary_requests_used: number;
      summary_requests_limit: number;
    };
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

function citationSeconds(label: string): number | null {
  const match = label.match(/约 ([0-9:]+)/)?.[1];
  if (!match) return null;
  return match.split(':').map(Number).reduce((seconds, part) => seconds * 60 + part, 0);
}

test('Stage 7 resumes real-result audit after trusted stop and verifies readback and deletion', async ({ page, baseURL }) => {
  test.skip(process.env.MVP_STAGE7_FINAL_RECORD_ALREADY_DELETED === '1', 'The private Stage 7 record has already been fully deleted.');
  test.skip(!username || !password || !videoId || !taskId, 'Private Stage 7 continuation identifiers are required.');
  test.setTimeout(240_000);
  expect(baseURL).toBe(process.env.MVP_WEB_URL ?? 'http://127.0.0.1:5174');
  expect(videoId).toMatch(/^[0-9a-f-]{36}$/i);
  expect(taskId).toMatch(/^[0-9a-f-]{36}$/i);
  expect(username).toMatch(/^stage5fixture-[a-f0-9]+$/);
  page.on('dialog', (dialog) => void dialog.accept());

  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });

  const beforeRestart = await readTask(page);
  expect(beforeRestart).toMatchObject({
    status: 'succeeded',
    summary_status: 'succeeded',
    execution_stop_confirmed: true,
    completed_segments: 36,
    expected_segments: 36,
    summary_requests_used: 1,
    summary_requests_limit: 3,
  });
  stage2Command('restart', '--timeout', '30', 'api', 'worker');
  await waitForStage2Health();
  await page.reload();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('.transcript-row').first()).toBeVisible();
  expect(await readTask(page)).toMatchObject({
    status: 'succeeded',
    summary_status: 'succeeded',
    execution_stop_confirmed: true,
    completed_segments: 36,
    expected_segments: 36,
  });

  const persistedEvents = await page.evaluate(async (id) => {
    const response = await fetch(`/api/videos/${id}/events?limit=100`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Persisted timeline read failed with HTTP ${response.status}.`);
    return await response.json() as { items: Array<{ event_type: string }> };
  }, videoId);
  const eventTypes = persistedEvents.items.map((event) => event.event_type);
  for (const required of ['analysis_submitted', 'execution_claimed', 'checkpoint_saved', 'transcription_completed', 'summary_requested', 'summary_published', 'stop_confirmed']) {
    expect(eventTypes).toContain(required);
  }

  if (process.env.MVP_STAGE7_MEDIA_ALREADY_DELETED !== '1') {
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
    return { citations: evidence.length, quoteMatches, timeMatches };
  }, videoId);
  expect(citationAudit.citations).toBeGreaterThan(0);
  expect(citationAudit.quoteMatches).toBe(citationAudit.citations);
  expect(citationAudit.timeMatches).toBe(citationAudit.citations);

  const citeButtons = page.locator('button.cite-button');
  await expect(citeButtons.first()).toBeVisible();
  const labels = await citeButtons.evaluateAll((buttons) => buttons.map((button) => button.getAttribute('aria-label') ?? ''));
  const firstIndex = labels.findIndex((label) => citationSeconds(label) !== null);
  const firstSeconds = firstIndex >= 0 ? citationSeconds(labels[firstIndex]!) : null;
  const secondIndex = labels.findIndex((label, index) => index !== firstIndex && citationSeconds(label) !== null && Math.abs(citationSeconds(label)! - (firstSeconds ?? 0)) > 1);
  expect(firstIndex).toBeGreaterThanOrEqual(0);
  expect(secondIndex).toBeGreaterThanOrEqual(0);
  const player = page.locator('video');
  const firstTarget = citationSeconds(labels[firstIndex]!)!;
  await citeButtons.nth(firstIndex).click();
  await expect.poll(() => player.evaluate((element: HTMLVideoElement) => !element.paused), { timeout: 15_000 }).toBe(true);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, target: number) => Math.abs(element.currentTime - target) < 2.5, firstTarget), { timeout: 10_000 }).toBe(true);
  const firstPosition = await player.evaluate((element: HTMLVideoElement) => element.currentTime);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, start: number) => !element.paused && element.currentTime > start + 0.2, firstPosition), { timeout: 15_000 }).toBe(true);
  const secondTarget = citationSeconds(labels[secondIndex]!)!;
  await citeButtons.nth(secondIndex).click();
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, target: number) => Math.abs(element.currentTime - target) < 2.5, secondTarget), { timeout: 10_000 }).toBe(true);
  const secondPosition = await player.evaluate((element: HTMLVideoElement) => element.currentTime);
  await expect.poll(() => player.evaluate((element: HTMLVideoElement, start: number) => !element.paused && element.currentTime > start + 0.2, secondPosition), { timeout: 15_000 }).toBe(true);
  expect(Math.abs(secondTarget - firstTarget)).toBeGreaterThan(1);

  const liveMedia = await page.evaluate(async (id) => {
    const [head, range] = await Promise.all([
      fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-15' }, cache: 'no-store' }),
    ]);
    return { head: head.status, range: range.status, rangeBytes: (await range.arrayBuffer()).byteLength };
  }, videoId);
  expect(liveMedia).toEqual({ head: 200, range: 206, rangeBytes: 16 });

  const removeMedia = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}/media`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除原视频，保留笔记' }).click();
  expect((await removeMedia).status()).toBe(200);
  await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible({ timeout: 60_000 });
  await expect(page.locator('video')).toHaveCount(0);
  const retained = await page.evaluate(async ({ id, analysisId }) => {
    const [video, task, transcript, summary, events, get, head, range] = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/tasks/${analysisId}`, { cache: 'no-store' }),
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
    const eventBody = await events.json() as { items: Array<{ event_type: string }> };
    return {
      status: [video.status, task.status, transcript.status, summary.status, events.status],
      mediaState: videoBody.media_status,
      transcriptComplete: transcriptBody.complete,
      transcriptSegments: transcriptBody.items.length,
      summaryItems: summaryBody.payload.chapters.length + summaryBody.payload.key_points.length,
      cleanupEvent: eventBody.items.some((event) => event.event_type === 'media_cleanup_completed'),
      mediaAccess: [get.status, head.status, range.status],
    };
  }, { id: videoId, analysisId: taskId });
  expect(retained.status).toEqual([200, 200, 200, 200, 200]);
  expect(retained.mediaState).toBe('deleted');
  expect(retained.transcriptComplete).toBe(true);
  expect(retained.transcriptSegments).toBe(36);
  expect(retained.summaryItems).toBeGreaterThan(0);
  expect(retained.cleanupEvent).toBe(true);
  expect(retained.mediaAccess).toEqual([410, 410, 410]);
  await page.reload();
  await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible();
  await expect(page.locator('.transcript-row').first()).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);
  } else {
    const retainedReadback = await page.evaluate(async ({ id, analysisId }) => {
      const [video, task, transcript, summary, events, get, head, range] = await Promise.all([
        fetch(`/api/videos/${id}`, { cache: 'no-store' }),
        fetch(`/api/videos/${id}/tasks/${analysisId}`, { cache: 'no-store' }),
        fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
        fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
        fetch(`/api/videos/${id}/events?limit=100`, { cache: 'no-store' }),
        fetch(`/api/videos/${id}/content`, { cache: 'no-store' }),
        fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
        fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-15' }, cache: 'no-store' }),
      ]);
      const videoBody = await video.json() as { media_status: string };
      const taskBody = await task.json() as { status: string; execution_stop_confirmed: boolean | null };
      const transcriptBody = await transcript.json() as { complete: boolean; items: unknown[] };
      const summaryBody = await summary.json() as { payload: { chapters: unknown[]; key_points: unknown[] } };
      const eventBody = await events.json() as { items: Array<{ event_type: string }> };
      return {
        statuses: [video.status, task.status, transcript.status, summary.status, events.status],
        mediaState: videoBody.media_status,
        taskState: taskBody.status,
        trustedStop: taskBody.execution_stop_confirmed,
        transcriptComplete: transcriptBody.complete,
        transcriptCount: transcriptBody.items.length,
        summaryItems: summaryBody.payload.chapters.length + summaryBody.payload.key_points.length,
        mediaStatuses: [get.status, head.status, range.status],
        cleanupEvent: eventBody.items.some((event) => event.event_type === 'media_cleanup_completed'),
      };
    }, { id: videoId, analysisId: taskId });
    expect(retainedReadback.statuses).toEqual([200, 200, 200, 200, 200]);
    expect(retainedReadback.mediaState).toBe('deleted');
    expect(retainedReadback.taskState).toBe('succeeded');
    expect(retainedReadback.trustedStop).toBe(true);
    expect(retainedReadback.transcriptComplete).toBe(true);
    expect(retainedReadback.transcriptCount).toBe(36);
    expect(retainedReadback.summaryItems).toBeGreaterThan(0);
    expect(retainedReadback.mediaStatuses).toEqual([410, 410, 410]);
    expect(retainedReadback.cleanupEvent).toBe(true);
    await page.reload();
    await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible();
    await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible();
    await expect(page.locator('.transcript-row').first()).toBeVisible();
    await expect(page.locator('video')).toHaveCount(0);
    await expect(page.locator('button.cite-button')).toHaveCount(0);
  }

  const deletion = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除整条记录' }).click();
  const deletionResponse = await deletion;
  expect([202, 204]).toContain(deletionResponse.status());
  await expect(page).toHaveURL(/\/$/, { timeout: 60_000 });
  const deletedStatuses = await page.evaluate(async ({ id, analysisId }) => {
    const responses = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/tasks/${analysisId}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/events`, { cache: 'no-store' }),
    ]);
    return responses.map((response) => response.status);
  }, { id: videoId, analysisId: taskId });
  expect(deletedStatuses).toEqual([404, 404, 404, 404, 404, 404, 404, 404]);
  console.log(`stage7_real_result_continuation=${JSON.stringify({
    trustedStopBeforeRestart: true,
    apiWorkerRestartReadback: true,
    completedSegments: 36,
    summaryRequestsUsed: 1,
    summaryRequestsLimit: 3,
    persistedTimelineEvents: persistedEvents.items.length,
    mediaAlreadyDeletedAtResume: process.env.MVP_STAGE7_MEDIA_ALREADY_DELETED === '1',
    wholeRecordDeleteStatuses: deletedStatuses,
  })}`);
});

test('Stage 7 confirms every endpoint hides the deleted private record', async ({ page, baseURL }) => {
  test.skip(process.env.MVP_STAGE7_VERIFY_DELETION !== '1', 'Enable the private Stage 7 post-deletion readback.');
  test.setTimeout(45_000);
  expect(baseURL).toBe(process.env.MVP_WEB_URL ?? 'http://127.0.0.1:5174');
  expect(videoId).toMatch(/^[0-9a-f-]{36}$/i);
  expect(taskId).toMatch(/^[0-9a-f-]{36}$/i);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
  const afterDelete = await page.evaluate(async ({ id, analysisId }) => {
    const [videoList, ...responses] = await Promise.all([
      fetch('/api/videos', { cache: 'no-store' }),
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/tasks/${analysisId}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/events`, { cache: 'no-store' }),
    ]);
    const list = await videoList.json() as { items: Array<{ id: string }> };
    return { listed: list.items.some((item) => item.id === id), statuses: responses.map((response) => response.status) };
  }, { id: videoId, analysisId: taskId });
  expect(afterDelete).toEqual({ listed: false, statuses: [404, 404, 404, 404, 404, 404, 404, 404] });
  console.log(`stage7_deleted_record_readback=${JSON.stringify(afterDelete)}`);
});
