import { expect, test } from '@playwright/test';

const username = process.env.MVP_QUEUE_ACCEPTANCE_USERNAME;
const password = process.env.MVP_QUEUE_ACCEPTANCE_PASSWORD;
const videoId = process.env.MVP_QUEUE_ACCEPTANCE_VIDEO_ID;

test('shows durable execution-slot queue state without a recovery action', async ({ page }) => {
  test.skip(!username || !password || !videoId, 'Set the isolated Stage 4 queue fixture values.');

  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(username!);
  await page.getByLabel('密码').fill(password!);
  const loginResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith('/auth/login') && response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '登录', exact: true }).click();
  const loginResponse = await loginResponsePromise;
  if (!loginResponse.ok()) await page.getByLabel('密码').fill('');
  await expect(loginResponse.status()).toBe(200);
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });

  await page.goto(`/videos/${videoId}`);
  await expect(page.getByRole('heading', { name: '处理状态' })).toBeVisible();
  await expect(page.getByText('等待处理', { exact: true })).toBeVisible();
  await expect(page.getByText('分析执行位被其他任务占用', { exact: true })).toBeVisible();
  await expect(page.getByText('下次协调检查', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);
});
