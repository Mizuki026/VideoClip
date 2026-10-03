import { bytesToHex } from '@noble/hashes/utils.js';
import { sha256 } from '@noble/hashes/sha2.js';

export interface FileHashes {
  sha256: string;
  chunkDigests: string[];
}

export async function calculateFileHashes(
  file: Blob,
  chunkSize: number,
  onProgress: (processed: number, total: number) => void = () => undefined,
): Promise<FileHashes> {
  if (!Number.isSafeInteger(chunkSize) || chunkSize <= 0) throw new Error('INVALID_CHUNK_SIZE');
  const complete = sha256.create();
  const chunkDigests: string[] = [];
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const end = Math.min(offset + chunkSize, file.size);
    const bytes = new Uint8Array(await file.slice(offset, end).arrayBuffer());
    complete.update(bytes);
    chunkDigests.push(bytesToHex(sha256(bytes)));
    onProgress(end, file.size);
  }
  return { sha256: bytesToHex(complete.digest()), chunkDigests };
}
