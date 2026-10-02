import { expect, test } from '@playwright/test';

test('same-origin browser entry and /api proxy are available', async ({ page, baseURL }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  const checks = await page.evaluate(async () => {
    const health = await fetch('/api/health', { cache: 'no-store' });
    const csrf = await fetch('/api/auth/csrf', { cache: 'no-store' });
    return {
      origin: window.location.origin,
      healthStatus: health.status,
      healthBody: health.ok ? (await health.json() as { status?: string }).status : null,
      csrfStatus: csrf.status,
    };
  });
  expect(checks.origin).toBe(baseURL);
  expect(checks.healthStatus).toBe(200);
  expect(checks.healthBody).toBe('ok');
  expect(checks.csrfStatus).toBe(200);
});
