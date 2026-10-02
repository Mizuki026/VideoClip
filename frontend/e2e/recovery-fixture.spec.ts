import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { resolve } from 'node:path';
import { expect, test } from '@playwright/test';

const sourceVideo = process.env.MVP_RECOVERY_FIXTURE_VIDEO;
const fixtureEnabled = process.env.MVP_STAGE5_RECOVERY_FIXTURE === '1';
const repoRoot = resolve(process.cwd(), '..');
const fixtureScript = resolve(repoRoot, 'scripts/stage2/stage5-recovery-fixture.py');
const python = process.env.MVP_STAGE2_PYTHON ?? resolve(repoRoot, 'data/mvp-stage-2/.venv/Scripts/python.exe');

function fixture(...arguments_: string[]): string {
  return execFileSync(python, [fixtureScript, ...arguments_], {
    cwd: repoRoot,
    env: { ...process.env, DB_HOST: '127.0.0.1', DB_PORT: '55443', PYTHONPATH: resolve(repoRoot, 'src') },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
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

test('failed cleanup and duplicate recovery remain independent and idempotent', async ({ page, baseURL }) => {
  test.skip(!fixtureEnabled || !sourceVideo, 'Set MVP_STAGE5_RECOVERY_FIXTURE=1 with a stopped idle Stage 2 worker and an authorized MP4 fixture.');
  expect(baseURL).toBe('http://127.0.0.1:5174');

  const entropy = randomBytes(6).toString('hex');
  const username = `stage5fixture-${entropy}`;
  const password = `VideoClip-${randomBytes(16).toString('hex')}!`;
  const forbiddenModelCalls: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST' && /\/(summarize|tasks\/[^/]+\/summarize)$/.test(new URL(request.url()).pathname)) {
      forbiddenModelCalls.push('summary-request');
    }
  });

  await createAccount(page, username, password);
  const uploadResponsePromise = page.waitForResponse((response) => response.url().endsWith('/api/videos') && response.request().method() === 'POST');
  await page.getByLabel('视频文件').setInputFiles(sourceVideo!);
  await page.getByRole('button', { name: '上传视频' }).click();
  expect((await uploadResponsePromise).status()).toBe(201);
  await expect(page).toHaveURL(/\/videos\/[0-9a-f-]{36}$/);
  const videoId = new URL(page.url()).pathname.split('/').at(-1)!;

  const taskResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}/tasks`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始分析' }).click();
  const taskResponse = await taskResponsePromise;
  expect(taskResponse.status()).toBe(202);
  const task = await taskResponse.json() as { id: string; current_attempt_id: string };
  expect(fixture('seed', '--video-id', videoId, '--task-id', task.id, '--attempt-id', task.current_attempt_id)).toBe('fixture=seeded');

  await page.reload();
  const detailRetry = page.getByRole('button', { name: '重试', exact: true });
  if (await detailRetry.count()) await detailRetry.click();
  await expect(page.getByRole('button', { name: '重试清理' })).toBeVisible();
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toBeVisible();
  await expect(page.getByText('队列未接受任务；可使用页面提供的恢复操作。').first()).toBeVisible();

  const cleanupResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}/cleanup`) && response.request().method() === 'POST');
  const taskRefreshPromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}/tasks/${task.id}`) && response.request().method() === 'GET', { timeout: 30_000 });
  await page.getByRole('button', { name: '重试清理' }).click();
  const [cleanupResponse, taskRefresh] = await Promise.all([cleanupResponsePromise, taskRefreshPromise]);
  expect(cleanupResponse.status()).toBe(200);
  expect((await cleanupResponse.json()).cleanup_state).toBe('cleaned');
  const refreshedTask = await taskRefresh.json() as { attempt: { cleanup_state: string } };
  expect(refreshedTask.attempt.cleanup_state).toBe('cleaned');
  await expect(page.locator('.cleanup-line')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toBeVisible();

  const recoveryResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}/tasks/${task.id}/recover`) && response.request().method() === 'POST');
  await page.getByRole('button', { name: '手动恢复分析' }).click();
  const recoveryResponse = await recoveryResponsePromise;
  expect(recoveryResponse.status()).toBe(202);
  const recovered = await recoveryResponse.json() as { current_attempt_id: string };
  expect(recovered.current_attempt_id).not.toBe(task.current_attempt_id);

  const duplicateRecoveries = await page.evaluate(async ({ videoId: id, taskId, parentAttemptId }) => {
    const token = (await (await fetch('/api/auth/csrf', { cache: 'no-store' })).json() as { csrf_token: string }).csrf_token;
    const responses = await Promise.all([1, 2].map(() => fetch(`/api/videos/${id}/tasks/${taskId}/recover`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
      body: JSON.stringify({ attempt_id: parentAttemptId }),
    })));
    const values = await Promise.all(responses.map(async (response) => ({ status: response.status, body: await response.json() as { current_attempt_id: string } })));
    return values;
  }, { videoId, taskId: task.id, parentAttemptId: task.current_attempt_id });
  expect(duplicateRecoveries.map((result) => result.status)).toEqual([202, 202]);
  expect(duplicateRecoveries.map((result) => result.body.current_attempt_id)).toEqual([recovered.current_attempt_id, recovered.current_attempt_id]);
  expect(fixture('verify', '--task-id', task.id, '--parent-attempt-id', task.current_attempt_id, '--current-attempt-id', recovered.current_attempt_id))
    .toBe('attempts=2; transcript_segments=0; summary_requests=0; executions=0');
  expect(forbiddenModelCalls).toEqual([]);
  await expect(page.getByRole('button', { name: '手动恢复分析' })).toHaveCount(0);

  expect(fixture('prepare-delete', '--video-id', videoId, '--task-id', task.id, '--current-attempt-id', recovered.current_attempt_id))
    .toBe('fixture_delete_ready=1');
  const deleteResponsePromise = page.waitForResponse((response) => response.url().endsWith(`/api/videos/${videoId}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: '删除记录' }).click();
  expect((await deleteResponsePromise).status()).toBe(204);
  await expect(page).toHaveURL(/\/$/);
  const logout = page.waitForResponse((response) => response.url().endsWith('/api/auth/logout'));
  await page.getByRole('button', { name: '退出登录' }).click();
  expect((await logout).status()).toBe(204);
  expect(fixture('cleanup-account', '--username', username)).toBe('empty_generated_account_removed=1');
});
