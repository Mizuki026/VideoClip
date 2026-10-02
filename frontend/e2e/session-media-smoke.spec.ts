import { randomBytes } from 'node:crypto';
import { expect, test } from '@playwright/test';

const sourceVideo = process.env.MVP_SESSION_MEDIA_VIDEO;

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

test('same-origin CSRF rejection, expired session, and raw video ranges', async ({ page, baseURL }) => {
  test.skip(!sourceVideo, 'Set MVP_SESSION_MEDIA_VIDEO to a valid authorized MP4 fixture.');
  expect(baseURL).toBe('http://127.0.0.1:5174');
  await page.setViewportSize({ width: 390, height: 844 });

  const entropy = randomBytes(6).toString('hex');
  const username = `stage5-${entropy}`;
  const password = `VideoClip-${randomBytes(16).toString('hex')}!`;
  const httpOnlyCookies: boolean[] = [];
  page.on('response', async (response) => {
    if (!response.url().endsWith('/api/auth/login')) return;
    const cookie = (await response.allHeaders())['set-cookie'] ?? '';
    httpOnlyCookies.push(/videoclip_session=[^;]+;[^\r\n]*httponly/i.test(cookie));
  });

  await createAccount(page, username, password);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
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
  const requestHeaders = await uploadResponse.request().allHeaders();
  expect(requestHeaders['content-type'] ?? '').not.toContain('multipart/form-data');
  expect(requestHeaders['x-video-filename']).toBeTruthy();
  expect(requestHeaders.origin).toBe(baseURL);
  await expect(page).toHaveURL(/\/videos\/[0-9a-f-]{36}$/);
  const videoId = new URL(page.url()).pathname.split('/').at(-1)!;
  const detail = await page.evaluate(async (id) => {
    const response = await fetch(`/api/videos/${id}`, { cache: 'no-store' });
    return { status: response.status, video: await response.json() as { byte_count: number; display_name: string } };
  }, videoId);
  expect(detail.status).toBe(200);
  expect(detail.video.byte_count).toBeGreaterThan(0);
  await expect(page.getByRole('heading', { name: detail.video.display_name })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);

  const mediaChecks = await page.evaluate(async ({ id, byteCount }) => {
    const head = await fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' });
    const range = await fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-31' }, cache: 'no-store' });
    const rangeBytes = (await range.arrayBuffer()).byteLength;
    const invalid = await fetch(`/api/videos/${id}/content`, { headers: { Range: `bytes=${byteCount}-` }, cache: 'no-store' });
    return {
      head: { status: head.status, length: head.headers.get('content-length') },
      range: { status: range.status, length: rangeBytes, contentRange: range.headers.get('content-range') },
      invalidRange: { status: invalid.status, contentRange: invalid.headers.get('content-range') },
    };
  }, { id: videoId, byteCount: detail.video.byte_count });
  expect(mediaChecks.head).toEqual({ status: 200, length: String(detail.video.byte_count) });
  expect(mediaChecks.range.status).toBe(206);
  expect(mediaChecks.range.length).toBe(32);
  expect(mediaChecks.range.contentRange).toBe(`bytes 0-31/${detail.video.byte_count}`);
  expect(mediaChecks.invalidRange).toEqual({ status: 416, contentRange: `bytes */${detail.video.byte_count}` });

  await page.context().clearCookies();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText('登录状态已失效，请重新登录。')).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);

  await loginAccount(page, username, password);
  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: detail.video.display_name })).toBeVisible();
  const deleteResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除记录' }).click();
  expect((await deleteResponsePromise).status()).toBe(204);
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByText(detail.video.display_name)).toHaveCount(0);
  const logout = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logout).status()).toBe(204);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  const rejectedAfterLogout = await page.evaluate(async (id) => {
    const [me, content] = await Promise.all([
      fetch('/api/auth/me', { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-0' }, cache: 'no-store' }),
    ]);
    return [me.status, content.status];
  }, videoId);
  expect(rejectedAfterLogout).toEqual([401, 401]);
  expect(httpOnlyCookies.some(Boolean)).toBe(true);
});
