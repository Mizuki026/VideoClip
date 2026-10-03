import { ApiError, requestResult } from './api';
import type { FileHashes } from './file-hashes';
import type { VideoDetail } from './types';

export const DEFAULT_UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024;
const STORAGE_PREFIX = 'videoclip.upload-recovery.v1:';
const MAX_RETRIES = 3;

export type UploadPhase = 'hashing' | 'creating' | 'checking' | 'transferring' | 'completing';

export interface UploadRecovery {
  userId: string;
  fileName: string;
  totalBytes: number;
  sha256: string;
  idempotencyKey: string;
  uploadId: string | null;
  createdAt: string;
}

export interface CreateUploadResponse {
  upload_id: string;
  state: string;
  chunk_size: number;
  total_bytes: number;
  confirmed_offset: number;
  expires_at: string;
  replayed: boolean;
}

export interface UploadChunkSummary {
  chunk_index: number;
  byte_offset: number;
  byte_count: number;
  sha256: string;
}

export interface UploadStatus {
  upload_id: string;
  state: string;
  chunk_size: number;
  total_bytes: number;
  confirmed_offset: number;
  next_chunk_index: number;
  expires_at: string;
  chunks: UploadChunkSummary[];
  error_code: string | null;
  video_id: string | null;
}

export interface UploadChunkResponse {
  upload_id: string;
  state: string;
  chunk_index: number;
  byte_offset: number;
  byte_count: number;
  sha256: string;
  confirmed_offset: number;
  expires_at: string;
  replayed: boolean;
}

export interface CompletedUploadResponse {
  video: VideoDetail;
}

export interface UploadProgress {
  phase: UploadPhase;
  processed: number;
  total: number;
  confirmed: number;
  requestSent: number;
  requestTotal: number;
}

export class UploadClientError extends Error {
  readonly code: string;

  constructor(code: string, message = code) {
    super(message);
    this.name = 'UploadClientError';
    this.code = code;
  }
}

export class UploadPausedError extends Error {
  constructor() {
    super('UPLOAD_PAUSED');
    this.name = 'UploadPausedError';
  }
}

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>;
type HashFunction = (
  file: File,
  chunkSize: number,
  signal: AbortSignal,
  onProgress: (processed: number, total: number) => void,
) => Promise<FileHashes>;

export interface UploadDependencies {
  storage: StorageLike;
  hashFile: HashFunction;
  create: (recovery: UploadRecovery, csrf: string, signal: AbortSignal) => Promise<CreateUploadResponse>;
  status: (uploadId: string, signal: AbortSignal) => Promise<UploadStatus>;
  putChunk: (
    uploadId: string,
    chunkIndex: number,
    chunk: Blob,
    digest: string,
    csrf: string,
    signal: AbortSignal,
    onProgress: (loaded: number, total: number) => void,
  ) => Promise<UploadChunkResponse>;
  complete: (uploadId: string, csrf: string, signal: AbortSignal) => Promise<CompletedUploadResponse>;
  abandon: (uploadId: string, csrf: string, signal: AbortSignal) => Promise<number>;
  wait: (milliseconds: number, signal: AbortSignal) => Promise<void>;
  newIdempotencyKey: () => string;
  now: () => string;
}

function storageFor(): StorageLike {
  return window.localStorage;
}

function recoveryStorageKey(userId: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(userId)}`;
}

function isRecovery(value: unknown, userId: string): value is UploadRecovery {
  if (typeof value !== 'object' || value === null) return false;
  const item = value as Partial<UploadRecovery>;
  return item.userId === userId
    && typeof item.fileName === 'string'
    && Number.isSafeInteger(item.totalBytes)
    && typeof item.sha256 === 'string'
    && /^[0-9a-f]{64}$/.test(item.sha256)
    && typeof item.idempotencyKey === 'string'
    && (typeof item.uploadId === 'string' || item.uploadId === null)
    && typeof item.createdAt === 'string';
}

export function getUploadRecoveries(userId: string, storage: StorageLike = storageFor()): UploadRecovery[] {
  try {
    const parsed: unknown = JSON.parse(storage.getItem(recoveryStorageKey(userId)) ?? '[]');
    return Array.isArray(parsed) ? parsed.filter((item): item is UploadRecovery => isRecovery(item, userId)) : [];
  } catch {
    return [];
  }
}

function saveRecoveries(userId: string, items: UploadRecovery[], storage: StorageLike): void {
  try {
    storage.setItem(recoveryStorageKey(userId), JSON.stringify(items));
  } catch {
    throw new UploadClientError('UPLOAD_RECOVERY_STORAGE_FAILED', '无法安全保存上传恢复信息；尚未发送上传请求。');
  }
}

function saveRecovery(item: UploadRecovery, storage: StorageLike): void {
  const items = getUploadRecoveries(item.userId, storage).filter((entry) => entry.idempotencyKey !== item.idempotencyKey);
  items.push(item);
  saveRecoveries(item.userId, items, storage);
}

function removeRecovery(item: UploadRecovery, storage: StorageLike): void {
  saveRecoveries(
    item.userId,
    getUploadRecoveries(item.userId, storage).filter((entry) => entry.idempotencyKey !== item.idempotencyKey),
    storage,
  );
}

function newKey(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
}

function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) throw new UploadPausedError();
}

function wait(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    throwIfAborted(signal);
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      window.clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      reject(new UploadPausedError());
    };
    signal.addEventListener('abort', abort, { once: true });
  });
}

function isTransient(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return error.status === 0
    || error.status === 502
    || error.status === 503
    || error.status === 504
    || error.code === 'UPLOAD_BUSY'
    || error.code === 'UPLOAD_CONFIRMATION_PENDING'
    || error.code === 'UPLOAD_FINALIZATION_PENDING'
    || error.code === 'UPLOAD_MEDIA_VALIDATION_FAILED';
}

async function retryBounded<T>(
  action: () => Promise<T>,
  signal: AbortSignal,
  dependencies: UploadDependencies,
  retryWhen = isTransient,
): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    throwIfAborted(signal);
    try {
      return await action();
    } catch (error) {
      if (signal.aborted) throw new UploadPausedError();
      if (attempt >= MAX_RETRIES || !retryWhen(error)) throw error;
      await dependencies.wait(250 * (2 ** attempt), signal);
    }
  }
}

function report(
  callback: (progress: UploadProgress) => void,
  update: Partial<UploadProgress> & Pick<UploadProgress, 'phase' | 'total'>,
): void {
  callback({ processed: 0, confirmed: 0, requestSent: 0, requestTotal: 0, ...update });
}

async function readStatus(
  uploadId: string,
  signal: AbortSignal,
  dependencies: UploadDependencies,
): Promise<UploadStatus> {
  return retryBounded(() => dependencies.status(uploadId, signal), signal, dependencies);
}

function validatePrefix(status: UploadStatus, hashes: FileHashes, total: number): void {
  if (!Number.isSafeInteger(status.chunk_size) || status.chunk_size <= 0 || status.total_bytes !== total) {
    throw new UploadClientError('UPLOAD_PROTOCOL_MISMATCH', '服务端上传规格与当前文件不匹配。');
  }
  const expectedChunks = status.confirmed_offset === 0 ? 0 : Math.ceil(status.confirmed_offset / status.chunk_size);
  if (status.chunks.length !== expectedChunks || status.next_chunk_index !== expectedChunks) {
    throw new UploadClientError('UPLOAD_STORAGE_CORRUPT', '服务端分片清单与确认偏移不一致。');
  }
  for (const chunk of status.chunks) {
    const expectedOffset = chunk.chunk_index * status.chunk_size;
    const expectedLength = Math.min(status.chunk_size, total - expectedOffset);
    if (chunk.chunk_index < 0
      || chunk.byte_offset !== expectedOffset
      || chunk.byte_count !== expectedLength
      || hashes.chunkDigests[chunk.chunk_index] !== chunk.sha256) {
      throw new UploadClientError('UPLOAD_STORAGE_CORRUPT', '已确认分片与重新选择的文件不一致。');
    }
  }
  const expectedOffset = Math.min(status.next_chunk_index * status.chunk_size, total);
  if (status.confirmed_offset !== expectedOffset) {
    throw new UploadClientError('UPLOAD_OFFSET_CONFLICT', '服务端确认偏移不在有效分片边界。');
  }
}

function statusError(status: UploadStatus): UploadClientError | null {
  if (status.state === 'receiving' || status.state === 'validating' || status.state === 'promoting' || status.state === 'complete') {
    return null;
  }
  const code = status.error_code ?? (status.state === 'expired' ? 'UPLOAD_EXPIRED' : `UPLOAD_${status.state.toUpperCase()}`);
  return new UploadClientError(code);
}

function uploadPath(uploadId: string): string {
  return `/uploads/${encodeURIComponent(uploadId)}`;
}

function errorCodeFromBody(value: unknown): string | null {
  if (typeof value !== 'object' || value === null || !('detail' in value)) return null;
  const detail = value.detail;
  if (typeof detail !== 'object' || detail === null || !('code' in detail)) return null;
  return typeof detail.code === 'string' ? detail.code : null;
}

async function hashInWorker(
  file: File,
  chunkSize: number,
  signal: AbortSignal,
  onProgress: (processed: number, total: number) => void,
): Promise<FileHashes> {
  return new Promise((resolve, reject) => {
    throwIfAborted(signal);
    const worker = new Worker(new URL('./upload-hash.worker.ts', import.meta.url), { type: 'module' });
    const cleanup = () => {
      signal.removeEventListener('abort', abort);
      worker.terminate();
    };
    const abort = () => {
      cleanup();
      reject(new UploadPausedError());
    };
    signal.addEventListener('abort', abort, { once: true });
    worker.addEventListener('message', (event: MessageEvent<
      | { type: 'progress'; processed: number; total: number }
      | { type: 'complete'; hashes: FileHashes }
      | { type: 'error'; message: string }
    >) => {
      if (event.data.type === 'progress') onProgress(event.data.processed, event.data.total);
      else if (event.data.type === 'complete') {
        cleanup();
        resolve(event.data.hashes);
      } else {
        cleanup();
        reject(new UploadClientError('UPLOAD_HASH_FAILED', event.data.message));
      }
    });
    worker.addEventListener('error', () => {
      cleanup();
      reject(new UploadClientError('UPLOAD_HASH_FAILED', '无法计算文件摘要。'));
    }, { once: true });
    worker.postMessage({ file, chunkSize });
  });
}

function defaultDependencies(): UploadDependencies {
  return {
    storage: storageFor(),
    hashFile: (file, chunkSize, signal, progress) => hashInWorker(file, chunkSize, signal, progress),
    create: async (recovery, csrf, signal) => {
      const result = await requestResult<CreateUploadResponse>('/uploads', {
        method: 'POST',
        csrf,
        signal,
        headers: { 'Idempotency-Key': recovery.idempotencyKey },
        body: { file_name: recovery.fileName, total_bytes: recovery.totalBytes, sha256: recovery.sha256 },
      });
      return result.data;
    },
    status: async (uploadId, signal) => requestResult<UploadStatus>(uploadPath(uploadId), { signal }).then((result) => result.data),
    putChunk: (uploadId, chunkIndex, chunk, digest, csrf, signal, progress) => new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const cleanup = () => signal.removeEventListener('abort', abort);
      const abort = () => xhr.abort();
      xhr.open('PUT', `/api${uploadPath(uploadId)}/chunks/${chunkIndex}`);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-CSRF-Token', csrf);
      xhr.setRequestHeader('X-Chunk-SHA256', digest);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.addEventListener('progress', (event) => progress(event.loaded, event.lengthComputable ? event.total : chunk.size));
      xhr.addEventListener('load', () => {
        cleanup();
        const body: unknown = (() => {
          try { return JSON.parse(xhr.responseText) as unknown; } catch { return null; }
        })();
        if (xhr.status >= 200 && xhr.status < 300 && typeof body === 'object' && body !== null && 'confirmed_offset' in body) {
          resolve(body as UploadChunkResponse);
        } else reject(new ApiError(xhr.status, errorCodeFromBody(body)));
      }, { once: true });
      xhr.addEventListener('error', () => { cleanup(); reject(new ApiError(0, 'NETWORK_UNAVAILABLE')); }, { once: true });
      xhr.addEventListener('abort', () => { cleanup(); reject(signal.aborted ? new UploadPausedError() : new ApiError(0, 'NETWORK_UNAVAILABLE')); }, { once: true });
      signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) abort();
      else xhr.send(chunk);
    }),
    complete: async (uploadId, csrf, signal) => requestResult<CompletedUploadResponse>(`${uploadPath(uploadId)}/complete`, {
      method: 'POST', csrf, signal,
    }).then((result) => result.data),
    abandon: async (uploadId, csrf, signal) => requestResult<unknown>(uploadPath(uploadId), {
      method: 'DELETE', csrf, signal,
    }).then((result) => result.status),
    wait,
    newIdempotencyKey: newKey,
    now: () => new Date().toISOString(),
  };
}

export async function runResumableUpload(
  userId: string,
  file: File,
  csrf: string,
  signal: AbortSignal,
  onProgress: (progress: UploadProgress) => void,
  onRecoveriesChanged: () => void = () => undefined,
  onMismatch: () => void = () => undefined,
  overrides: Partial<UploadDependencies> = {},
): Promise<VideoDetail> {
  const dependencies = { ...defaultDependencies(), ...overrides };
  report(onProgress, { phase: 'hashing', total: file.size, processed: 0 });
  let hashes = await dependencies.hashFile(file, DEFAULT_UPLOAD_CHUNK_SIZE, signal, (processed, total) => {
    report(onProgress, { phase: 'hashing', total, processed });
  });
  throwIfAborted(signal);

  const existing = getUploadRecoveries(userId, dependencies.storage);
  const matching = existing
    .filter((entry) => entry.totalBytes === file.size && entry.sha256 === hashes.sha256)
    .sort((left, right) => right.createdAt.localeCompare(left.createdAt))[0];
  const sameNameAndSizeDifferentContent = existing.some((entry) =>
    entry.fileName === file.name && entry.totalBytes === file.size && entry.sha256 !== hashes.sha256,
  );
  const recovery: UploadRecovery = matching ?? {
    userId,
    fileName: file.name,
    totalBytes: file.size,
    sha256: hashes.sha256,
    idempotencyKey: dependencies.newIdempotencyKey(),
    uploadId: null,
    createdAt: dependencies.now(),
  };
  // Persist before the first request so a lost create response can be replayed with the same key.
  saveRecovery(recovery, dependencies.storage);
  onRecoveriesChanged();

  if (sameNameAndSizeDifferentContent && !matching) {
    onMismatch();
  }

  report(onProgress, { phase: 'creating', total: file.size, processed: file.size });
  const created = await retryBounded(
    () => dependencies.create(recovery, csrf, signal), signal, dependencies,
  );
  if (created.total_bytes !== file.size || created.chunk_size <= 0) {
    throw new UploadClientError('UPLOAD_PROTOCOL_MISMATCH', '服务端返回的文件长度或分片规格无效。');
  }
  const identified: UploadRecovery = { ...recovery, uploadId: created.upload_id };
  saveRecovery(identified, dependencies.storage);
  onRecoveriesChanged();

  if (created.chunk_size !== DEFAULT_UPLOAD_CHUNK_SIZE) {
    hashes = await dependencies.hashFile(file, created.chunk_size, signal, (processed, total) => {
      report(onProgress, { phase: 'hashing', total, processed });
    });
    if (hashes.sha256 !== identified.sha256) {
      throw new UploadClientError('UPLOAD_FILE_CHANGED', '文件在摘要计算后发生变化，请重新选择文件。');
    }
  }

  report(onProgress, { phase: 'checking', total: file.size, processed: file.size });
  let status = await readStatus(created.upload_id, signal, dependencies);
  if (status.total_bytes !== file.size || status.chunk_size !== created.chunk_size) {
    throw new UploadClientError('UPLOAD_PROTOCOL_MISMATCH', '服务端会话规格与创建响应不一致。');
  }
  validatePrefix(status, hashes, file.size);
  const currentStatusError = statusError(status);
  if (currentStatusError) throw currentStatusError;

  let confirmed = status.state === 'complete' ? file.size : status.confirmed_offset;
  let nextIndex = status.next_chunk_index;
  while (status.state === 'receiving' && confirmed < file.size) {
    throwIfAborted(signal);
    const offset = confirmed;
    const index = nextIndex;
    const length = Math.min(status.chunk_size, file.size - offset);
    const chunk = file.slice(offset, offset + length);
    const digest = hashes.chunkDigests[index];
    if (!digest) throw new UploadClientError('UPLOAD_HASH_FAILED', '无法取得当前分片摘要。');

    let sent: UploadChunkResponse | null = null;
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt += 1) {
      report(onProgress, {
        phase: 'transferring', total: file.size, processed: file.size,
        confirmed: offset, requestSent: 0, requestTotal: length,
      });
      try {
        sent = await dependencies.putChunk(created.upload_id, index, chunk, digest, csrf, signal, (requestSent, requestTotal) => {
          report(onProgress, {
            phase: 'transferring', total: file.size, processed: file.size,
            confirmed: offset, requestSent, requestTotal,
          });
        });
        if (sent.confirmed_offset !== offset + length || sent.chunk_index !== index) {
          throw new UploadClientError('UPLOAD_OFFSET_CONFLICT', '服务端分片响应与当前确认位置不一致。');
        }
        break;
      } catch (error) {
        if (signal.aborted) throw new UploadPausedError();
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) throw error;
        status = await readStatus(created.upload_id, signal, dependencies);
        const recoveredError = statusError(status);
        if (recoveredError) throw recoveredError;
        validatePrefix(status, hashes, file.size);
        if (status.confirmed_offset === offset + length) {
          sent = { confirmed_offset: status.confirmed_offset, chunk_index: index } as UploadChunkResponse;
          break;
        }
        if (status.confirmed_offset !== offset) {
          throw new UploadClientError('UPLOAD_OFFSET_CONFLICT', '服务端确认偏移发生冲突；未根据本地发送进度继续。');
        }
        if (attempt >= MAX_RETRIES || !isTransient(error)) throw error;
        await dependencies.wait(250 * (2 ** attempt), signal);
      }
    }
    if (!sent) throw new UploadClientError('UPLOAD_CONFIRMATION_PENDING', '分片确认结果仍不确定，请稍后重新选择原文件续传。');
    confirmed = sent.confirmed_offset;
    nextIndex = sent.chunk_index + 1;
    status = {
      ...status,
      confirmed_offset: confirmed,
      next_chunk_index: nextIndex,
      state: 'receiving',
    };
    report(onProgress, {
      phase: 'transferring', total: file.size, processed: file.size,
      confirmed, requestSent: 0, requestTotal: 0,
    });
  }

  if (confirmed !== file.size) {
    status = await readStatus(created.upload_id, signal, dependencies);
    validatePrefix(status, hashes, file.size);
    if (status.state !== 'receiving' || status.confirmed_offset !== file.size) {
      const stateError = statusError(status);
      if (stateError) throw stateError;
      throw new UploadClientError('UPLOAD_OFFSET_CONFLICT', '服务端尚未确认完整文件。');
    }
  }

  report(onProgress, { phase: 'completing', total: file.size, processed: file.size, confirmed: file.size });
  let lastError: unknown = null;
  for (let attempt = 0; attempt <= MAX_RETRIES; attempt += 1) {
    try {
      const completed = await dependencies.complete(created.upload_id, csrf, signal);
      removeRecovery(identified, dependencies.storage);
      onRecoveriesChanged();
      return completed.video;
    } catch (error) {
      if (signal.aborted) throw new UploadPausedError();
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) throw error;
      lastError = error;
      if (!isTransient(error)) throw error;
      status = await readStatus(created.upload_id, signal, dependencies);
      validatePrefix(status, hashes, file.size);
      const recoveredError = statusError(status);
      if (recoveredError) throw recoveredError;
      if (status.state === 'complete' && attempt < MAX_RETRIES) {
        await dependencies.wait(250 * (2 ** attempt), signal);
        continue;
      }
      if ((status.state === 'validating' || status.state === 'promoting') && attempt < MAX_RETRIES) {
        await dependencies.wait(250 * (2 ** attempt), signal);
        continue;
      }
      if (status.state === 'receiving' && status.confirmed_offset === file.size && attempt < MAX_RETRIES) {
        await dependencies.wait(250 * (2 ** attempt), signal);
        continue;
      }
      throw error;
    }
  }
  throw lastError ?? new UploadClientError('UPLOAD_FINALIZATION_PENDING');
}

export async function abandonUploadRecovery(
  userId: string,
  idempotencyKey: string,
  csrf: string,
  signal: AbortSignal,
  onRecoveriesChanged: () => void = () => undefined,
  overrides: Partial<UploadDependencies> = {},
): Promise<'abandoned' | 'pending'> {
  const dependencies = { ...defaultDependencies(), ...overrides };
  const recovery = getUploadRecoveries(userId, dependencies.storage).find((item) => item.idempotencyKey === idempotencyKey);
  if (!recovery) return 'abandoned';
  const created = recovery.uploadId ? null : await retryBounded(
    () => dependencies.create(recovery, csrf, signal), signal, dependencies,
  );
  const uploadId = recovery.uploadId ?? created?.upload_id;
  if (!uploadId) throw new UploadClientError('UPLOAD_NOT_FOUND', '尚未找到待放弃的上传会话。');
  if (!recovery.uploadId) {
    saveRecovery({ ...recovery, uploadId }, dependencies.storage);
    onRecoveriesChanged();
  }
  const responseStatus = await dependencies.abandon(uploadId, csrf, signal);
  if (responseStatus === 202) return 'pending';
  if (responseStatus === 204) {
    removeRecovery(recovery, dependencies.storage);
    onRecoveriesChanged();
    return 'abandoned';
  }
  throw new UploadClientError('UPLOAD_CLEANUP_PENDING', '服务端尚未确认临时文件清理完成。');
}

export function uploadErrorMessage(error: unknown): string {
  if (error instanceof UploadPausedError) return '上传已暂停。会话已保留；重新选择原文件后可按服务端确认偏移续传。';
  if (error instanceof UploadClientError) {
    const messages: Record<string, string> = {
      UPLOAD_EXPIRED: '临时上传已过期。请重新开始上传。',
      UPLOAD_ABANDONED: '该临时上传已放弃。请重新开始上传。',
      UPLOAD_STORAGE_CORRUPT: '服务端临时分片校验失败。该会话已停止续传，请联系维护者。',
      UPLOAD_INVALID_MEDIA: '服务器未能验证视频格式、音轨或时间轴。该会话可放弃后重新上传。',
      UPLOAD_DIGEST_MISMATCH: '文件摘要校验失败。该会话不能继续，请放弃后重新上传。',
      UPLOAD_ALREADY_COMPLETED: '视频已经登记，不能通过放弃操作删除。',
      UPLOAD_CLEANUP_PENDING: '已记录放弃请求，临时文件清理仍在等待重试。',
      UPLOAD_RECOVERY_STORAGE_FAILED: '浏览器无法保存恢复信息，未继续发送上传请求。',
      UPLOAD_PROTOCOL_MISMATCH: '服务端返回了不一致的上传规格，请稍后重试。',
      UPLOAD_OFFSET_CONFLICT: '服务端确认偏移存在冲突。已停止上传，请重新查询后续传。',
      UPLOAD_CONFIRMATION_PENDING: '分片确认结果尚未确定。重新选择文件后会先查询服务端状态。',
      UPLOAD_FINALIZATION_PENDING: '文件已传完，服务端登记仍在处理中；可重新选择文件查询并完成。',
      UPLOAD_FILE_CHANGED: '文件在摘要计算期间发生变化，请重新选择文件。',
      UPLOAD_HASH_FAILED: '无法完成文件摘要计算。',
      UPLOAD_BUSY: '上传会话正被另一个请求使用。请稍后重试。',
      UPLOAD_NOT_FOUND: '当前账户下找不到该临时上传。',
      UPLOAD_IDEMPOTENCY_CONFLICT: '恢复信息与服务端记录冲突，请放弃旧临时上传后重新开始。',
      UPLOAD_CHUNK_CONFLICT: '分片与服务端已确认内容冲突，未继续上传。',
      UPLOAD_OUT_OF_ORDER: '服务端偏移与当前分片顺序冲突，已停止并保留恢复信息。',
      UPLOAD_INCOMPLETE: '服务端尚未确认全部分片。重新选择文件后可续传。',
      UPLOAD_INVALID_STATE: '该会话状态不允许继续上传。',
      UPLOAD_MEDIA_VALIDATION_FAILED: '媒体检查服务暂时不可用；上传会话已保留，可稍后重试。',
      UPLOAD_STORAGE_FAILED: '服务端存储失败。会话已保留，可检查磁盘后重试。',
    };
    return messages[error.code] ?? `上传未完成（${error.code}）。恢复信息已保留。`;
  }
  if (error instanceof ApiError) {
    if (error.status === 0) return '网络中断。会话和幂等键已保留；重新选择文件后先查询服务端偏移。';
    if (error.status === 401) return '登录状态已失效，请重新登录。';
    if (error.status === 403) return '安全校验失败，请刷新页面后重试。';
    if (error.code) return uploadErrorMessage(new UploadClientError(error.code));
    if (error.status >= 500) return '服务端暂时不可用。上传会话已保留，可稍后重试。';
    return `服务器拒绝了上传请求（HTTP ${error.status}）。`;
  }
  return error instanceof Error ? error.message : '上传失败。恢复信息已保留。';
}
