import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './api';
import { calculateFileHashes } from './file-hashes';
import {
  abandonUploadRecovery,
  getUploadRecoveries,
  runResumableUpload,
  UploadPausedError,
  type CreateUploadResponse,
  type UploadDependencies,
  type UploadRecovery,
  type UploadStatus,
} from './resumableUpload';
import type { VideoDetail } from './types';

class MemoryStorage {
  private values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
}

const storage = new MemoryStorage();
const video = { id: 'video-1' } as VideoDetail;

function file(content: string, name = 'course.mp4'): File {
  return new File([content], name, { type: 'video/mp4' });
}

function recovery(userId: string, overrides: Partial<UploadRecovery> = {}): UploadRecovery {
  return {
    userId,
    fileName: 'course.mp4',
    totalBytes: 3,
    sha256: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    idempotencyKey: `idem-${userId}`,
    uploadId: 'upload-1',
    createdAt: '2026-10-03T00:00:00.000Z',
    ...overrides,
  };
}

function createResponse(overrides: Partial<CreateUploadResponse> = {}): CreateUploadResponse {
  return {
    upload_id: 'upload-1', state: 'receiving', chunk_size: 2, total_bytes: 3,
    confirmed_offset: 0, expires_at: '2026-10-04T00:00:00Z', replayed: false,
    ...overrides,
  };
}

function status(overrides: Partial<UploadStatus> = {}): UploadStatus {
  return {
    upload_id: 'upload-1', state: 'receiving', chunk_size: 2, total_bytes: 3,
    confirmed_offset: 0, next_chunk_index: 0, expires_at: '2026-10-04T00:00:00Z',
    chunks: [], error_code: null, video_id: null,
    ...overrides,
  };
}

function dependencies(overrides: Partial<UploadDependencies> = {}): UploadDependencies {
  return {
    storage,
    hashFile: (source, chunkSize, _signal, progress) => calculateFileHashes(source, chunkSize, progress),
    create: vi.fn(async () => createResponse()),
    status: vi.fn(async () => status()),
    putChunk: vi.fn(async (_id, index, chunk, digest) => ({
      upload_id: 'upload-1', state: 'receiving', chunk_index: index,
      byte_offset: index * 2, byte_count: chunk.size, sha256: digest,
      confirmed_offset: index * 2 + chunk.size, expires_at: '2026-10-04T00:00:00Z', replayed: false,
    })),
    complete: vi.fn(async () => ({ video })),
    abandon: vi.fn(async () => 204),
    wait: async () => undefined,
    newIdempotencyKey: () => 'new-idem',
    now: () => '2026-10-03T00:00:00.000Z',
    ...overrides,
  };
}

describe('resumable browser upload', () => {
  beforeEach(() => {
    storage.setItem('videoclip.upload-recovery.v1:user-a', '[]');
    storage.setItem('videoclip.upload-recovery.v1:user-b', '[]');
  });

  it('computes the complete and per-chunk SHA-256 incrementally at exact boundaries', async () => {
    const progress: number[] = [];
    const hashes = await calculateFileHashes(file('abc'), 2, (processed) => progress.push(processed));
    expect(hashes.sha256).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
    expect(hashes.chunkDigests).toEqual([
      'fb8e20fc2e4c3f248c60c39bd652f3c1347298bb977b8b4d5903b85055620603',
      '2e7d2c03a9507ae265ecf5b5356885a53393a2029d241394997265a1a25aefc6',
    ]);
    expect(progress).toEqual([2, 3]);
  });

  it('persists before create and reuses the same idempotency key after a lost response', async () => {
    let calls = 0;
    const deps = dependencies({
      create: vi.fn(async (item) => {
        expect(getUploadRecoveries('user-a', storage)[0]?.idempotencyKey).toBe(item.idempotencyKey);
        calls += 1;
        if (calls === 1) throw new ApiError(0, 'NETWORK_UNAVAILABLE');
        return createResponse({ replayed: true });
      }),
    });
    const result = await runResumableUpload('user-a', file('abc'), 'csrf', new AbortController().signal, () => undefined, () => undefined, () => undefined, deps);
    expect(result).toBe(video);
    expect(deps.create).toHaveBeenCalledTimes(2);
    expect((deps.create as ReturnType<typeof vi.fn>).mock.calls[0]?.[0].idempotencyKey)
      .toBe((deps.create as ReturnType<typeof vi.fn>).mock.calls[1]?.[0].idempotencyKey);
    expect(getUploadRecoveries('user-a', storage)).toHaveLength(0);
  });

  it('resumes from the server confirmed offset and reconciles an uncertain chunk response', async () => {
    const hashes = await calculateFileHashes(file('abc'), 2);
    storage.setItem('videoclip.upload-recovery.v1:user-a', JSON.stringify([
      recovery('user-a', { sha256: hashes.sha256 }),
    ]));
    let reads = 0;
    const deps = dependencies({
      status: vi.fn(async () => {
        reads += 1;
        return reads === 1
          ? status({
            confirmed_offset: 2,
            next_chunk_index: 1,
            chunks: [{ chunk_index: 0, byte_offset: 0, byte_count: 2, sha256: hashes.chunkDigests[0]! }],
          })
          : status({
            confirmed_offset: 3,
            next_chunk_index: 2,
            chunks: [
              { chunk_index: 0, byte_offset: 0, byte_count: 2, sha256: hashes.chunkDigests[0]! },
              { chunk_index: 1, byte_offset: 2, byte_count: 1, sha256: hashes.chunkDigests[1]! },
            ],
          });
      }),
      putChunk: vi.fn(async () => { throw new ApiError(0, 'NETWORK_UNAVAILABLE'); }),
    });
    await runResumableUpload('user-a', file('abc'), 'csrf', new AbortController().signal, () => undefined, () => undefined, () => undefined, deps);
    expect(deps.putChunk).toHaveBeenCalledTimes(1);
    expect(deps.putChunk).toHaveBeenCalledWith('upload-1', 1, expect.any(Blob), hashes.chunkDigests[1], 'csrf', expect.any(AbortSignal), expect.any(Function));
    expect(reads).toBe(2);
  });

  it('does not resume a same-name, same-size file with different content', async () => {
    storage.setItem('videoclip.upload-recovery.v1:user-a', JSON.stringify([
      recovery('user-a', { uploadId: 'old-upload' }),
    ]));
    const deps = dependencies({ create: vi.fn(async () => createResponse({ upload_id: 'new-upload' })) });
    const mismatch = vi.fn();
    await runResumableUpload('user-a', file('xyz'), 'csrf', new AbortController().signal, () => undefined, () => undefined, mismatch, deps);
    expect(mismatch).toHaveBeenCalledOnce();
    expect(deps.create).toHaveBeenCalledWith(expect.objectContaining({ idempotencyKey: 'new-idem' }), 'csrf', expect.any(AbortSignal));
    expect(deps.status).toHaveBeenCalledWith('new-upload', expect.any(AbortSignal));
    expect(deps.status).not.toHaveBeenCalledWith('old-upload', expect.anything());
  });

  it('isolates stored recovery records by authenticated user', async () => {
    storage.setItem('videoclip.upload-recovery.v1:user-a', JSON.stringify([recovery('user-a')]));
    const deps = dependencies({
      create: vi.fn(async (item) => createResponse({ upload_id: `for-${item.userId}` })),
    });
    await runResumableUpload('user-b', file('abc'), 'csrf', new AbortController().signal, () => undefined, () => undefined, () => undefined, deps);
    expect(deps.create).toHaveBeenCalledWith(expect.objectContaining({ userId: 'user-b', idempotencyKey: 'new-idem' }), 'csrf', expect.any(AbortSignal));
    expect(getUploadRecoveries('user-a', storage)).toHaveLength(1);
    expect(getUploadRecoveries('user-b', storage)).toHaveLength(0);
  });

  it('keeps pause separate from abandon, retains HTTP 202 cleanup intent, and clears only after 204', async () => {
    const item = recovery('user-a');
    storage.setItem('videoclip.upload-recovery.v1:user-a', JSON.stringify([item]));
    const controller = new AbortController();
    controller.abort();
    const deps = dependencies({ hashFile: async () => { throw new UploadPausedError(); } });
    await expect(runResumableUpload('user-a', file('abc'), 'csrf', controller.signal, () => undefined, () => undefined, () => undefined, deps))
      .rejects.toBeInstanceOf(UploadPausedError);
    expect(deps.abandon).not.toHaveBeenCalled();
    expect(getUploadRecoveries('user-a', storage)).toHaveLength(1);

    deps.abandon = vi.fn(async () => 202);
    await expect(abandonUploadRecovery('user-a', item.idempotencyKey, 'csrf', new AbortController().signal, () => undefined, deps))
      .resolves.toBe('pending');
    expect(getUploadRecoveries('user-a', storage)).toHaveLength(1);
    deps.abandon = vi.fn(async () => 204);
    await expect(abandonUploadRecovery('user-a', item.idempotencyKey, 'csrf', new AbortController().signal, () => undefined, deps))
      .resolves.toBe('abandoned');
    expect(getUploadRecoveries('user-a', storage)).toHaveLength(0);
  });

  it('repeats completion after a lost response and returns the one server video', async () => {
    let completions = 0;
    const hashes = await calculateFileHashes(file('abc'), 2);
    const deps = dependencies({
      status: vi.fn(async () => status({
        state: 'complete', confirmed_offset: 3, next_chunk_index: 2, video_id: video.id,
        chunks: [
          { chunk_index: 0, byte_offset: 0, byte_count: 2, sha256: hashes.chunkDigests[0]! },
          { chunk_index: 1, byte_offset: 2, byte_count: 1, sha256: hashes.chunkDigests[1]! },
        ],
      })),
      complete: vi.fn(async () => {
        completions += 1;
        if (completions === 1) throw new ApiError(0, 'NETWORK_UNAVAILABLE');
        return { video };
      }),
    });
    const result = await runResumableUpload('user-a', file('abc'), 'csrf', new AbortController().signal, () => undefined, () => undefined, () => undefined, deps);
    expect(result).toBe(video);
    expect(deps.complete).toHaveBeenCalledTimes(2);
    expect(getUploadRecoveries('user-a', storage)).toHaveLength(0);
  });
});
