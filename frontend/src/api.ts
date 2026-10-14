import type {
  AnalysisTask,
  AnalysisEventPage,
  DeletionProgress,
  MediaDeletionProgress,
  Summary,
  Transcript,
  User,
  VideoDetail,
  VideoList,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, code: string | null) {
    super(`API request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

function responseCode(value: unknown): string | null {
  if (typeof value !== 'object' || value === null || !('detail' in value)) return null;
  const detail = value.detail;
  if (typeof detail === 'object' && detail !== null && 'code' in detail) {
    return typeof detail.code === 'string' ? detail.code : null;
  }
  return null;
}

async function readResponse(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) return undefined;
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

export async function request<T>(
  path: string,
  options: { method?: string; csrf?: string; body?: unknown; signal?: AbortSignal; headers?: HeadersInit } = {},
): Promise<T> {
  const result = await requestResult<T>(path, options);
  return result.data;
}

export async function requestResult<T>(
  path: string,
  options: { method?: string; csrf?: string; body?: unknown; signal?: AbortSignal; headers?: HeadersInit } = {},
): Promise<{ status: number; data: T }> {
  const headers = new Headers({ Accept: 'application/json' });
  new Headers(options.headers).forEach((value, key) => headers.set(key, value));
  // 登录凭据由 Cookie 携带；CSRF Token 另放请求头，供写接口校验。
  if (options.csrf) headers.set('X-CSRF-Token', options.csrf);
  if (options.body !== undefined) headers.set('Content-Type', 'application/json');

  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      // 同源请求由浏览器携带 Cookie，页面无需读取 HttpOnly 登录令牌。
      credentials: 'same-origin',
      cache: 'no-store',
      signal: options.signal,
    });
  } catch {
    throw new ApiError(0, 'NETWORK_UNAVAILABLE');
  }

  const data = await readResponse(response);
  if (!response.ok) throw new ApiError(response.status, responseCode(data));
  return { status: response.status, data: data as T };
}

export function getCsrfToken(): Promise<{ csrf_token: string }> {
  if (csrfTokenRequest) return csrfTokenRequest;
  // 接口返回可提交到请求头的 CSRF Token，不是 HttpOnly 登录令牌。
  const pending = request<{ csrf_token: string }>('/auth/csrf');
  csrfTokenRequest = pending;
  void pending.finally(() => {
    if (csrfTokenRequest === pending) csrfTokenRequest = null;
  }).catch(() => undefined);
  return pending;
}

let csrfTokenRequest: Promise<{ csrf_token: string }> | null = null;

export function getCurrentUser(): Promise<User> {
  // 刷新后凭已有 Cookie 查询用户，会话是否有效由服务端判断。
  return request('/auth/me');
}

export function listVideos(): Promise<VideoList> {
  return request('/videos');
}

export function getVideo(videoId: string): Promise<VideoDetail> {
  return request(`/videos/${encodeURIComponent(videoId)}`);
}

export function getTask(videoId: string, taskId: string): Promise<AnalysisTask> {
  return request(`/videos/${encodeURIComponent(videoId)}/tasks/${encodeURIComponent(taskId)}`);
}

export function getAnalysisEvents(
  videoId: string,
  options: { beforeId?: number; limit?: number } = {},
): Promise<AnalysisEventPage> {
  const query = new URLSearchParams();
  if (options.beforeId !== undefined) query.set('before_id', String(options.beforeId));
  if (options.limit !== undefined) query.set('limit', String(options.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : '';
  return request(`/videos/${encodeURIComponent(videoId)}/events${suffix}`);
}

export function getTranscript(videoId: string): Promise<Transcript> {
  return request(`/videos/${encodeURIComponent(videoId)}/transcript`);
}

export function getSummary(videoId: string): Promise<Summary> {
  return request(`/videos/${encodeURIComponent(videoId)}/summary`);
}

export function getDeletionProgress(videoId: string): Promise<DeletionProgress> {
  return request(`/videos/${encodeURIComponent(videoId)}/deletion`);
}

export function getMediaDeletionProgress(videoId: string): Promise<MediaDeletionProgress> {
  return request(`/videos/${encodeURIComponent(videoId)}/media-deletion`);
}

export function deleteVideoMedia(videoId: string, csrf: string): Promise<MediaDeletionProgress> {
  return request(`/videos/${encodeURIComponent(videoId)}/media`, { method: 'DELETE', csrf });
}

export function retryVideoMediaDeletion(videoId: string, csrf: string): Promise<MediaDeletionProgress> {
  return request(`/videos/${encodeURIComponent(videoId)}/media-deletion/retry`, { method: 'POST', csrf });
}
