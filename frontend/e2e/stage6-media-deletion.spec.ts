import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

test('stage 6 media deletion preserves notes, closes playback, survives API restart, then whole deletion removes the record', async ({ page, baseURL }) => {
  test.setTimeout(120_000);
  const username = process.env.VIDEOCLIP_STAGE6_USERNAME;
  const password = process.env.VIDEOCLIP_STAGE6_PASSWORD;
  const videoId = process.env.VIDEOCLIP_STAGE6_VIDEO_ID;
  const composeArgs = process.env.VIDEOCLIP_STAGE6_COMPOSE_ARGS;
  if (!username || !password || !videoId || !composeArgs) throw new Error('Missing isolated Stage 6 browser fixture configuration.');

  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.getByLabel('用户名').fill(username);
  await page.getByLabel('密码').fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible();
  await page.goto(`/videos/${videoId}`);

  await expect(page.getByText('合成测试引用保留').first()).toBeVisible();
  await expect(page.getByRole('heading', { name: '课程总结' })).toBeVisible();
  await expect(page.locator('video')).toHaveCount(1);
  await page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('仅删除原视频文件');
    expect(dialog.message()).toContain('保留视频记录');
    await dialog.accept();
  });
  await page.getByRole('button', { name: '删除原视频，保留笔记' }).click();
  await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /回看章节引用/ })).toHaveCount(0);
  await expect(page.locator('.transcript-time-static')).toHaveCount(1);
  await expect(page.getByText('合成测试引用保留').first()).toBeVisible();
  await expect(page.getByText('删除原视频后笔记继续保留')).toBeVisible();

  const mediaResponses = await page.evaluate(async (id) => {
    const [get, head, range] = await Promise.all([
      fetch(`/api/videos/${id}/content`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { method: 'HEAD', cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { headers: { Range: 'bytes=0-1' }, cache: 'no-store' }),
    ]);
    return [get.status, head.status, range.status];
  }, videoId);
  expect(mediaResponses).toEqual([410, 410, 410]);

  await page.reload();
  await expect(page.getByText('原视频已删除，笔记保留')).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);
  await expect(page.getByText('删除原视频后笔记继续保留')).toBeVisible();

  const compose = JSON.parse(composeArgs) as string[];
  execFileSync('docker', [...compose, 'restart', 'api'], { stdio: 'ignore' });
  const apiBase = new URL('/api/health', baseURL).toString();
  const healthDeadline = Date.now() + 30_000;
  let healthy = false;
  while (Date.now() < healthDeadline) {
    try {
      const response = await page.request.get(apiBase, { timeout: 2_000 });
      if (response.ok()) { healthy = true; break; }
    } catch {
      // The isolated API container is restarting.
    }
    await page.waitForTimeout(500);
  }
  expect(healthy).toBe(true);
  await page.reload();
  await expect(page.getByText('合成测试引用保留').first()).toBeVisible();
  await expect(page.getByText('删除原视频后笔记继续保留')).toBeVisible();
  await expect(page.locator('video')).toHaveCount(0);

  await page.once('dialog', async (dialog) => {
    expect(dialog.message()).toContain('删除整条记录');
    expect(dialog.message()).toContain('移除原视频、转写');
    await dialog.accept();
  });
  await page.getByRole('button', { name: '删除整条记录' }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible();
  const remainingStatus = await page.evaluate(async (id) => {
    const [video, transcript, summary, media] = await Promise.all([
      fetch(`/api/videos/${id}`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/transcript`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/summary`, { cache: 'no-store' }),
      fetch(`/api/videos/${id}/content`, { cache: 'no-store' }),
    ]);
    return [video.status, transcript.status, summary.status, media.status];
  }, videoId);
  expect(remainingStatus).toEqual([404, 404, 404, 404]);
});
