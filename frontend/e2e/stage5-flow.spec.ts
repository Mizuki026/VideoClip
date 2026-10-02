import { randomBytes } from 'node:crypto';
import { expect, test } from '@playwright/test';

const sourceVideo = process.env.MVP_ACCEPTANCE_VIDEO;
const uuidPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/gi;

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
  const primaryUser = `stage5-${entropy}`;
  const secondaryUser = `stage5-other-${entropy}`;
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
    .toMatch(/^(awaiting_summary:waiting:stopped|succeeded:succeeded:stopped)$/);
  const checkpoint = await page.evaluate(async ({ id, taskId }) => {
    const response = await fetch(`/api/videos/${id}/tasks/${taskId}`, { cache: 'no-store' });
    return { status: response.status, body: await response.json() };
  }, { id: videoId, taskId: task.id }) as { status: number; body: { status: string; execution_state: string; execution_stop_confirmed: boolean; completed_segments: number; expected_segments: number } };
  expect(checkpoint.status).toBe(200);
  expect(checkpoint.body.execution_stop_confirmed).toBe(true);
  expect(['awaiting_summary', 'succeeded']).toContain(checkpoint.body.status);
  expect(checkpoint.body.completed_segments).toBe(checkpoint.body.expected_segments);
  await expect(page.locator('.transcript-row').first()).toBeVisible();
  if (checkpoint.body.status === 'awaiting_summary') {
    await page.getByRole('button', { name: '继续生成总结' }).click();
  }
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 1_500_000 });
  await page.reload();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible({ timeout: 30_000 });
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
  await expect(page.getByText('登录状态已失效，请重新登录。')).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);
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
