import { randomBytes } from 'node:crypto';
import { open, stat, unlink, writeFile } from 'node:fs/promises';
import { expect, test, type Page, type Route } from '@playwright/test';

const sourceVideo = process.env.MVP_RESUMABLE_VIDEO;

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
}

function deferred(): Deferred {
  let resolve: () => void = () => undefined;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function registerAndLogin(page: Page, username: string, password: string): Promise<void> {
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

async function apiStatus(page: Page, uploadId: string): Promise<{
  state: string;
  confirmed_offset: number;
  video_id: string | null;
}> {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/uploads/${id}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`upload status failed: ${response.status}`);
    return await response.json() as { state: string; confirmed_offset: number; video_id: string | null };
  }, uploadId);
}

test('real browser resumes the authorized long video from server-confirmed chunks', async ({ page }, testInfo) => {
  test.setTimeout(90 * 60 * 1000);
  test.skip(!sourceVideo, 'Set MVP_RESUMABLE_VIDEO to the authorized long MP4 fixture.');
  const source = sourceVideo!;
  const sourceStat = await stat(source);
  expect(sourceStat.size).toBeGreaterThan(8 * 1024 * 1024);
  const sourceName = source.split(/[\\/]/).at(-1)!;
  const entropy = randomBytes(6).toString('hex');
  const username = `stage2-upload-${entropy}`;
  const password = `VideoClip-${randomBytes(18).toString('hex')}!`;
  const uploadIds = new Set<string>();
  const chunkRequests: Array<{ uploadId: string; index: number }> = [];
  const originalCreateKey = { value: '' };
  const originalGate = deferred();
  const originalHeld = deferred();
  const wrongGate = deferred();
  const wrongHeld = deferred();
  const heldRoutes = new Set<string>();
  let originalUploadId = '';
  let wrongUploadId: string;
  let wrongMode = false;
  let videoId = '';
  let authenticated = false;

  await page.route(/\/api\/uploads\/[^/]+\/chunks\/\d+$/, async (route: Route) => {
    const match = new URL(route.request().url()).pathname.match(/\/uploads\/([^/]+)\/chunks\/(\d+)$/);
    if (!match) return route.continue();
    const [, uploadId, rawIndex] = match;
    const index = Number(rawIndex);
    chunkRequests.push({ uploadId: uploadId!, index });
    const key = `${uploadId}:${index}`;
    if (uploadId === originalUploadId && index === 1 && !heldRoutes.has(`original:${key}`)) {
      heldRoutes.add(`original:${key}`);
      originalHeld.resolve();
      await originalGate.promise;
      try { await route.abort(); } catch { /* The XHR may already have aborted with the pause signal. */ }
      return;
    }
    if (wrongMode && uploadId !== originalUploadId && index === 0 && !heldRoutes.has(`wrong:${key}`)) {
      heldRoutes.add(`wrong:${key}`);
      wrongHeld.resolve();
      await wrongGate.promise;
      try { await route.abort(); } catch { /* The XHR may already have aborted with the pause signal. */ }
      return;
    }
    await route.continue();
  });

  try {
    await registerAndLogin(page, username, password);
    authenticated = true;

    const initialCreatePromise = page.waitForResponse((response) =>
      response.url().endsWith('/api/uploads') && response.request().method() === 'POST',
      { timeout: 20 * 60 * 1000 },
    );
    await page.locator('#video-file').setInputFiles(source);
    await page.getByRole('button', { name: /上传 \/ 继续上传/ }).click();
    const initialCreate = await initialCreatePromise;
    expect(initialCreate.status()).toBe(201);
    const initialCreateBody = await initialCreate.json() as { upload_id: string };
    originalUploadId = initialCreateBody.upload_id;
    uploadIds.add(originalUploadId);
    originalCreateKey.value = (await initialCreate.request().allHeaders())['idempotency-key'] ?? '';
    expect(originalCreateKey.value).toBeTruthy();
    await originalHeld.promise;
    await expect(page.getByText(/服务端已确认 8\.0 MiB/)).toBeVisible({ timeout: 60_000 });
    const interrupted = await apiStatus(page, originalUploadId);
    expect(interrupted).toMatchObject({ state: 'receiving', confirmed_offset: 8_388_608 });
    await page.getByRole('button', { name: '暂停上传' }).click();
    originalGate.resolve();
    await expect(page.getByText(/上传已暂停/)).toBeVisible({ timeout: 30_000 });
    expect(await apiStatus(page, originalUploadId)).toMatchObject({ confirmed_offset: 8_388_608 });

    await page.reload();
    await expect(page.getByRole('heading', { name: '课程视频', exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/此账户有 1 个临时上传/)).toBeVisible();

    const wrongPath = testInfo.outputPath(sourceName);
    const originalHandle = await open(source, 'r');
    const firstByte = Buffer.alloc(1);
    await originalHandle.read(firstByte, 0, 1, 0);
    await originalHandle.close();
    const wrongHandle = await open(wrongPath, 'w');
    await wrongHandle.truncate(sourceStat.size);
    await wrongHandle.write(Buffer.from([firstByte[0]! ^ 0xff]), 0, 1, 0);
    await wrongHandle.close();
    expect((await stat(wrongPath)).size).toBe(sourceStat.size);

    wrongMode = true;
    const wrongCreatePromise = page.waitForResponse((response) =>
      response.url().endsWith('/api/uploads') && response.request().method() === 'POST',
      { timeout: 20 * 60 * 1000 },
    );
    await page.locator('#video-file').setInputFiles(wrongPath);
    await page.getByRole('button', { name: /上传 \/ 继续上传/ }).click();
    const wrongCreate = await wrongCreatePromise;
    expect(wrongCreate.status()).toBe(201);
    const wrongBody = await wrongCreate.json() as { upload_id: string };
    wrongUploadId = wrongBody.upload_id;
    uploadIds.add(wrongUploadId);
    expect(wrongUploadId).not.toBe(originalUploadId);
    expect((await wrongCreate.request().allHeaders())['idempotency-key']).not.toBe(originalCreateKey.value);
    await expect(page.getByText(/同名同大小的旧上传内容不同/)).toBeVisible({ timeout: 60_000 });
    await wrongHeld.promise;
    expect(await apiStatus(page, wrongUploadId)).toMatchObject({ state: 'receiving', confirmed_offset: 0 });
    await page.getByRole('button', { name: '暂停上传' }).click();
    wrongGate.resolve();
    await expect(page.getByText(/上传已暂停/)).toBeVisible({ timeout: 30_000 });

    const wrongRecovery = page.locator('.recovery-item').filter({ hasText: sourceName }).nth(1);
    await wrongRecovery.getByRole('button', { name: '放弃临时上传' }).click();
    await expect(page.getByText('临时上传已清理。')).toBeVisible({ timeout: 30_000 });
    const abandonedStatus = await page.evaluate(async (id) =>
      (await fetch(`/api/uploads/${id}`, { cache: 'no-store' })).status,
    wrongUploadId);
    expect(abandonedStatus).toBe(404);

    const resumedCreatePromise = page.waitForResponse((response) =>
      response.url().endsWith('/api/uploads') && response.request().method() === 'POST',
      { timeout: 20 * 60 * 1000 },
    );
    await page.locator('#video-file').setInputFiles(source);
    await page.getByRole('button', { name: /上传 \/ 继续上传/ }).click();
    const resumedCreate = await resumedCreatePromise;
    expect(resumedCreate.status()).toBe(200);
    const resumedBody = await resumedCreate.json() as { upload_id: string; replayed: boolean };
    expect(resumedBody).toEqual(expect.objectContaining({ upload_id: originalUploadId, replayed: true }));

    await expect(page).toHaveURL(/\/videos\/[0-9a-f-]{36}$/, { timeout: 80 * 60 * 1000 });
    videoId = new URL(page.url()).pathname.split('/').at(-1)!;
    const videoDetails = await page.evaluate(async (id) => {
      const [detailResponse, listResponse] = await Promise.all([
        fetch(`/api/videos/${id}`, { cache: 'no-store' }),
        fetch('/api/videos', { cache: 'no-store' }),
      ]);
      const detail = await detailResponse.json() as { byte_count: number; display_name: string };
      const list = await listResponse.json() as { items: Array<{ id: string }> };
      return {
        detailStatus: detailResponse.status,
        detail,
        matchingVideos: list.items.filter((item) => item.id === id).length,
        videoCount: list.items.length,
      };
    }, videoId);
    expect(videoDetails.detailStatus).toBe(200);
    expect(videoDetails.detail.byte_count).toBe(sourceStat.size);
    expect(videoDetails.detail.display_name).toBe(sourceName);
    expect(videoDetails.matchingVideos).toBe(1);
    expect(videoDetails.videoCount).toBe(1);

    const player = page.getByLabel('课程视频播放器');
    await expect(player).toBeVisible();
    await page.waitForFunction(() => {
      const video = document.querySelector<HTMLVideoElement>('video[aria-label="课程视频播放器"]');
      return Boolean(video && video.readyState >= HTMLMediaElement.HAVE_METADATA);
    }, { timeout: 120_000 });
    const playback = await player.evaluate(async (video: HTMLVideoElement) => {
      const startingTime = video.currentTime;
      await video.play();
      await new Promise((resolve) => window.setTimeout(resolve, 1_000));
      video.pause();
      return { readyState: video.readyState, currentTime: video.currentTime, startingTime };
    });
    expect(playback.readyState).toBeGreaterThanOrEqual(1);
    expect(playback.currentTime).toBeGreaterThan(playback.startingTime);

    const boundaryOffsets = [0, 8_388_608, sourceStat.size - 64];
    for (const offset of boundaryOffsets) {
      const expected = Buffer.alloc(32);
      const handle = await open(source, 'r');
      await handle.read(expected, 0, expected.length, offset);
      await handle.close();
      const received = await page.evaluate(async ({ id, start }) => {
        let lastError: unknown;
        for (let attempt = 0; attempt < 4; attempt += 1) {
          try {
            const response = await fetch(`/api/videos/${id}/content`, {
              headers: { Range: `bytes=${start}-${start + 31}` },
              cache: 'no-store',
              signal: AbortSignal.timeout(15_000),
            });
            const bytes = Array.from(new Uint8Array(await response.arrayBuffer()));
            if (response.status < 500 || attempt === 3) return { status: response.status, bytes };
            lastError = new Error(`transient media response: ${response.status}`);
          } catch (error) {
            if (attempt === 3) throw error;
            lastError = error;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 250 * (attempt + 1)));
        }
        throw lastError instanceof Error ? lastError : new Error('media range request failed');
      }, { id: videoId, start: offset });
      expect(received.status).toBe(206);
      expect(Buffer.from(received.bytes)).toEqual(expected);
    }
    expect(chunkRequests.filter((item) => item.uploadId === originalUploadId && item.index === 0)).toHaveLength(1);
    expect(chunkRequests.some((item) => item.uploadId === originalUploadId && item.index === 1)).toBe(true);

    const status = await apiStatus(page, originalUploadId);
    expect(status.state).toBe('complete');
    expect(status.video_id).toBe(videoId);
    await page.getByRole('button', { name: '删除记录' }).click();
    await expect(page.locator('video')).toHaveCount(0, { timeout: 30_000 });
    await expect(page).toHaveURL(/\/$/, { timeout: 30_000 });
    await expect(page.getByRole('heading', { name: '这里还没有课程视频' })).toBeVisible();
    videoId = '';
    await writeFile(testInfo.outputPath('test-account.txt'), username, 'utf8');
  } finally {
    originalGate.resolve();
    wrongGate.resolve();
    try { await unlink(testInfo.outputPath(sourceName)); } catch { /* No wrong-file fixture was created. */ }
    await page.unroute(/\/api\/uploads\/[^/]+\/chunks\/\d+$/).catch(() => undefined);
    if (authenticated) {
      try {
        const csrf = await page.evaluate(async () => {
          const response = await fetch('/api/auth/csrf', { cache: 'no-store' });
          return response.ok ? (await response.json() as { csrf_token: string }).csrf_token : '';
        });
        if (videoId) {
          await page.evaluate(async ({ id, token }) => fetch(`/api/videos/${id}`, {
            method: 'DELETE', headers: { 'X-CSRF-Token': token },
          }), { id: videoId, token: csrf });
        }
        for (const uploadId of uploadIds) {
          try {
            const state = await apiStatus(page, uploadId);
            if (state.state !== 'complete' && state.state !== 'abandoned') {
              await page.evaluate(async ({ id, token }) => fetch(`/api/uploads/${id}`, {
                method: 'DELETE', headers: { 'X-CSRF-Token': token },
              }), { id: uploadId, token: csrf });
            }
          } catch { /* The server-side session may already have been removed. */ }
        }
      } catch { /* Preserve failed acceptance data for inspection. */ }
    }
  }
});
