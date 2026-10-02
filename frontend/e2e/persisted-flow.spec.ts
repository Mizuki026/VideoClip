import { randomBytes } from 'node:crypto';
import { expect, test } from '@playwright/test';

const username = process.env.MVP_REUSE_USERNAME;
const password = process.env.MVP_REUSE_PASSWORD;
const videoId = process.env.MVP_REUSE_VIDEO_ID;

function citationSeconds(label: string): number | null {
  const match = label.match(/约 ([0-9:]+)/)?.[1];
  if (!match) return null;
  return match.split(':').map(Number).reduce((total, part) => total * 60 + part, 0);
}

async function login(page: import('@playwright/test').Page, name: string, secret: string) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(name);
  await page.getByLabel('密码').fill(secret);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
}

async function createAccount(page: import('@playwright/test').Page, name: string, secret: string) {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByRole('button', { name: '创建账户' }).click();
  await page.getByLabel('用户名').fill(name);
  await page.getByLabel('密码').fill(secret);
  await page.getByRole('button', { name: '创建账户', exact: true }).click();
  await expect(page.getByText('账户已创建，请登录继续。')).toBeVisible();
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
}

test('real persisted task, summary, playback, isolation, logout, and deletion', async ({ page, baseURL }) => {
  test.skip(!username || !password || !videoId, 'Set the three MVP_REUSE_* variables for the isolated persisted browser scenario.');
  test.setTimeout(180_000);

  const requestRecords: Array<{ path: string; method: string; status: number; contentType: string; origin: string | null; csrfPresent: boolean; cookiePresent: boolean }> = [];
  const pendingRecords: Promise<void>[] = [];
  const httpOnlySession: boolean[] = [];
  page.on('requestfinished', (request) => {
    const url = new URL(request.url());
    if (url.origin !== baseURL || !url.pathname.startsWith('/api/')) return;
    pendingRecords.push((async () => {
      try {
        const [response, headers] = await Promise.all([request.response(), request.allHeaders()]);
        if (!response) return;
        requestRecords.push({
          path: url.pathname.replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/gi, '{id}'),
          method: request.method(),
          status: response.status(),
          contentType: headers['content-type'] ?? '',
          origin: headers.origin ?? null,
          csrfPresent: Boolean(headers['x-csrf-token']),
          cookiePresent: Boolean(headers.cookie),
        });
      } catch {
        // Ignore request metadata that the browser releases after navigation.
      }
    })());
  });
  page.on('response', (response) => {
    if (!response.url().endsWith('/api/auth/login')) return;
    pendingRecords.push((async () => {
      try {
        const setCookie = (await response.allHeaders())['set-cookie'] ?? '';
        httpOnlySession.push(/videoclip_session=[^;]+;[^\r\n]*httponly/i.test(setCookie));
      } catch { /* The page may navigate after login. */ }
    })());
  });

  await login(page, username!, password!);
  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('.transcript-row').first()).toBeVisible();
  const persisted = await page.evaluate(async (id) => {
    const video = await fetch(`/api/videos/${id}`, { cache: 'no-store' });
    const detail = await video.json() as { analysis_task_id?: string | null; byte_count: number };
    const taskResponse = await fetch(`/api/videos/${id}/tasks/${detail.analysis_task_id}`, { cache: 'no-store' });
    const task = await taskResponse.json() as { status: string; summary_status: string; execution_stop_confirmed: boolean; completed_segments: number; expected_segments: number };
    const [transcript, summary] = await Promise.all([
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
    ]);
    return {
      videoStatus: video.status,
      taskStatus: taskResponse.status,
      task: {
        status: task.status,
        summaryStatus: task.summary_status,
        stopConfirmed: task.execution_stop_confirmed,
        completedSegments: task.completed_segments,
        expectedSegments: task.expected_segments,
      },
      transcriptStatus: transcript.status,
      summaryStatus: summary.status,
      byteCount: detail.byte_count,
    };
  }, videoId!) as { videoStatus: number; taskStatus: number; task: { status: string; summaryStatus: string; stopConfirmed: boolean; completedSegments: number; expectedSegments: number }; transcriptStatus: number; summaryStatus: number; byteCount: number };
  expect(persisted.videoStatus).toBe(200);
  expect(persisted.taskStatus).toBe(200);
  expect(persisted.task).toMatchObject({ status: 'succeeded', summaryStatus: 'succeeded', stopConfirmed: true });
  expect(persisted.task.completedSegments).toBeGreaterThan(0);
  expect(persisted.task.completedSegments).toBe(persisted.task.expectedSegments);
  expect(persisted.transcriptStatus).toBe(200);
  expect(persisted.summaryStatus).toBe(200);
  expect(persisted.byteCount).toBeGreaterThan(1_073_741_824);

  await page.reload();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });
  const citeButtons = page.locator('button.cite-button');
  await expect(citeButtons.first()).toBeVisible();
  const labels = await citeButtons.evaluateAll((buttons) => buttons.map((button) => button.getAttribute('aria-label') ?? ''));
  const firstIndex = labels.findIndex((label) => citationSeconds(label) !== null);
  const firstTarget = firstIndex >= 0 ? citationSeconds(labels[firstIndex]!) : null;
  const secondIndex = labels.findIndex((label, index) => index !== firstIndex && citationSeconds(label) !== null && Math.abs(citationSeconds(label)! - (firstTarget ?? 0)) > 1);
  expect(firstIndex).toBeGreaterThanOrEqual(0);
  expect(secondIndex).toBeGreaterThanOrEqual(0);
  const player = page.locator('video');
  for (const index of [firstIndex, secondIndex]) {
    const target = citationSeconds(labels[index]!);
    expect(target).not.toBeNull();
    await citeButtons.nth(index).click();
    await expect.poll(() => player.evaluate((element: HTMLVideoElement) => !element.paused), { timeout: 15_000 }).toBe(true);
    await expect.poll(() => player.evaluate((element: HTMLVideoElement, expected: number) => Math.abs(element.currentTime - expected) < 2.5, target ?? 0), { timeout: 10_000 }).toBe(true);
    const positionedTime = await player.evaluate((element: HTMLVideoElement) => element.currentTime);
    await expect.poll(() => player.evaluate((element: HTMLVideoElement, start: number) => !element.paused && element.currentTime > start + 0.2, positionedTime), { timeout: 15_000 }).toBe(true);
    const playback = await player.evaluate((element: HTMLVideoElement) => ({ paused: element.paused, currentTime: element.currentTime, ready: element.readyState }));
    expect(playback.paused).toBe(false);
    expect(Math.abs(playback.currentTime - (target ?? 0))).toBeLessThan(2.5);
    expect(playback.currentTime).toBeGreaterThan(positionedTime + 0.2);
    expect(playback.ready).toBeGreaterThanOrEqual(2);
  }

  const mediaChecks = await page.evaluate(async (id) => {
    const head = await fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' });
    const range = await fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-15' }, cache: 'no-store' });
    return { headStatus: head.status, length: head.headers.get('content-length'), rangeStatus: range.status, contentRange: range.headers.get('content-range'), byteCount: (await range.arrayBuffer()).byteLength };
  }, videoId!);
  expect(mediaChecks.headStatus).toBe(200);
  expect(Number(mediaChecks.length)).toBe(persisted.byteCount);
  expect(mediaChecks.rangeStatus).toBe(206);
  expect(mediaChecks.byteCount).toBe(16);
  expect(mediaChecks.contentRange).toContain(`/${persisted.byteCount}`);

  const logoutPrimary = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logoutPrimary).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);
  const anonymous = await page.evaluate(async (id) => {
    const me = await fetch('/api/auth/me', { cache: 'no-store' });
    const content = await fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' });
    return [me.status, content.status];
  }, videoId!);
  expect(anonymous).toEqual([401, 401]);

  const entropy = randomBytes(6).toString('hex');
  const secondaryUser = `stage5-other-${entropy}`;
  await createAccount(page, secondaryUser, password!);
  const isolation = await page.evaluate(async (id) => {
    const responses = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
    ]);
    return responses.map((response) => response.status);
  }, videoId!);
  expect(isolation).toEqual([404, 404, 404, 404]);

  const logoutSecondary = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logoutSecondary).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await login(page, username!, password!);
  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 20_000 });
  const deleteResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除记录' }).click();
  const deleteResponse = await deleteResponsePromise;
  expect([202, 204]).toContain(deleteResponse.status());
  await expect(page).toHaveURL(/\/$/, { timeout: 60_000 });
  const afterDelete = await page.evaluate(async (id) => {
    const responses = await Promise.all([
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
    ]);
    return responses.map((response) => response.status);
  }, videoId!);
  expect(afterDelete.every((status) => [404, 410].includes(status))).toBe(true);

  await Promise.all(pendingRecords);
  expect(httpOnlySession.some(Boolean)).toBe(true);
  expect(requestRecords.every((request) => request.origin === null || request.origin === baseURL)).toBe(true);
  expect(requestRecords.filter((request) => ['POST', 'PATCH', 'PUT', 'DELETE'].includes(request.method)).every((request) => request.origin === baseURL && request.csrfPresent)).toBe(true);
  expect(requestRecords.some((request) => request.method === 'GET' && request.path.endsWith('/auth/me') && request.status === 200 && request.cookiePresent)).toBe(true);
});
