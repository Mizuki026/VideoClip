import { createHash } from 'node:crypto';
import { expect, test } from '@playwright/test';

const username = process.env.MVP_STAGE5_USERNAME;
const password = process.env.MVP_STAGE5_PASSWORD;
const videoId = process.env.MVP_STAGE5_WAITING_VIDEO_ID;
const recoveredVideoId = process.env.MVP_STAGE5_RECOVERED_VIDEO_ID;
const permanentFailureVideoId = process.env.MVP_STAGE5_PERMANENT_VIDEO_ID;
const stopConfirmationVideoId = process.env.MVP_STAGE5_STOP_CONFIRMATION_VIDEO_ID;
const exhaustedVideoId = process.env.MVP_STAGE5_EXHAUSTED_VIDEO_ID;

async function login(page: import('@playwright/test').Page) {
  const csrfResponses: Array<Promise<{ bodyHash: string | null; cookieHash: string | null }>> = [];
  page.on('response', (response) => {
    if (!response.url().endsWith('/api/auth/csrf')) return;
    csrfResponses.push((async () => {
      const value = await response.json() as { csrf_token?: string };
      const headers = await response.allHeaders();
      const setCookie = headers['set-cookie']?.split(';', 1)[0];
      const cookieValue = setCookie?.startsWith('videoclip_csrf=')
        ? setCookie.slice('videoclip_csrf='.length)
        : null;
      const hash = (token: string | null | undefined) => token
        ? createHash('sha256').update(token).digest('hex').slice(0, 12)
        : null;
      return { bodyHash: hash(value.csrf_token), cookieHash: hash(cookieValue) };
    })());
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(username!);
  await page.getByLabel('密码').fill(password!);
  const loginRequestPromise = page.waitForRequest((request) =>
    request.url().endsWith('/auth/login') && request.method() === 'POST',
  );
  const loginResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith('/auth/login') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '登录', exact: true }).click();
  const loginRequest = await loginRequestPromise;
  const headers = await loginRequest.allHeaders();
  const csrfCookies = headers.cookie?.split(';').map((item) => item.trim())
    .filter((item) => item.startsWith('videoclip_csrf='))
    .map((item) => item.slice('videoclip_csrf='.length)) ?? [];
  const csrfHeader = headers['x-csrf-token'];
  await page.locator('#password').evaluate((input) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    setter?.call(input, '');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const loginResponse = await loginResponsePromise;
  if (loginResponse.status() !== 200) {
    const csrfObservations = await Promise.allSettled(csrfResponses);
    const hash = (token: string | null | undefined) => token
      ? createHash('sha256').update(token).digest('hex').slice(0, 12)
      : null;
    throw new Error(`isolated login failed; csrf diagnostics=${JSON.stringify({
      origin: headers.origin ?? null,
      expectedOrigin: process.env.MVP_STAGE5_EXPECTED_ORIGIN ?? null,
      originMatches: headers.origin === process.env.MVP_STAGE5_EXPECTED_ORIGIN,
      headerPresent: Boolean(csrfHeader),
      cookieCount: csrfCookies.length,
      cookieMatchesHeader: csrfCookies.some((cookie) => csrfHeader && csrfHeader === cookie),
      headerHash: hash(csrfHeader),
      cookieHashes: csrfCookies.map(hash),
      csrfResponses: csrfObservations.map((result) => result.status === 'fulfilled' ? result.value : null),
    })}; HTTP ${loginResponse.status()}`);
  }
  expect(loginResponse.status()).toBe(200);
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
}

test('Stage 5 displays a persisted model wait and independent budgets', async ({ page }) => {
  test.skip(!username || !password || !videoId, 'Set isolated Stage 5 waiting-task values.');

  await login(page);
  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: '处理状态' })).toBeVisible();
  await expect(page.getByText('等待模型冷却', { exact: true })).toBeVisible();
  await expect(page.getByText('等待总结模型冷却', { exact: true })).toBeVisible();
  await expect(page.getByText('预计下次检查', { exact: true })).toBeVisible();
  await expect(page.getByText('累计模型等待', { exact: true })).toBeVisible();
  await expect(page.getByText('执行失败自动恢复', { exact: true })).toBeVisible();
  await expect(page.getByText('总结请求预算', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);
});

test('Stage 5 displays automatic recovery budgets and blocks a permanent retry', async ({ page }) => {
  test.skip(
    !username || !password || !recoveredVideoId || !permanentFailureVideoId,
    'Set isolated Stage 5 recovered and permanent-failure task values.',
  );

  await login(page);
  await page.goto(`/videos/${recoveredVideoId}`);
  await expect(page.getByRole('heading', { name: '处理状态' })).toBeVisible();
  await expect(page.locator('.status-row').filter({ hasText: '执行失败自动恢复' })).toContainText('1 / 2');
  await expect(page.locator('.status-row').filter({ hasText: '总结请求预算' })).toContainText('2 / 3');
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);

  await page.goto(`/videos/${permanentFailureVideoId}`);
  await expect(page.getByRole('heading', { name: '处理状态' })).toBeVisible();
  await expect(page.getByText('总结模型鉴权失败。请核对模型账户和密钥配置后再恢复。')).toBeVisible();
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);
});

test('Stage 5 explains the unconfirmed stop gate', async ({ page }) => {
  test.skip(
    !username || !password || !stopConfirmationVideoId,
    'Set isolated Stage 5 active-execution values.',
  );

  await login(page);
  await page.goto(`/videos/${stopConfirmationVideoId}`);
  await expect(page.getByRole('heading', { name: '处理状态' })).toBeVisible();
  await expect(page.getByText('停止尚未确认', { exact: true })).toBeVisible();
  await expect(page.getByText(/旧 Worker 尚未确认停止/)).toBeVisible();
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);
});

test('Stage 5 displays both exhausted budgets and preserves the transcript', async ({ page }) => {
  test.skip(
    !username || !password || !exhaustedVideoId,
    'Set an isolated Stage 5 exhausted-task value.',
  );

  await login(page);
  await page.goto(`/videos/${exhaustedVideoId}`);
  await expect(page.getByRole('heading', { name: '处理状态' })).toBeVisible();
  await expect(page.locator('.status-row').filter({ hasText: '执行失败自动恢复' })).toContainText('2 / 2');
  await expect(page.locator('.status-row').filter({ hasText: '总结请求预算' })).toContainText('3 / 3（已耗尽）');
  await expect(page.getByText('自动恢复次数已耗尽；可用总结或补投预算仍分别按各自计数。')).toBeVisible();
  await expect(page.getByText('总结请求已耗尽；已验证转写会保留。')).toBeVisible();
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);
});
