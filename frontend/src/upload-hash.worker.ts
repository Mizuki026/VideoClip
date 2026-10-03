import { calculateFileHashes, type FileHashes } from './file-hashes';

type HashWorkerRequest = { file: File; chunkSize: number };
type HashWorkerResponse =
  | { type: 'progress'; processed: number; total: number }
  | { type: 'complete'; hashes: FileHashes }
  | { type: 'error'; message: string };

const workerScope = self as unknown as {
  addEventListener: (type: 'message', listener: (event: MessageEvent<HashWorkerRequest>) => void) => void;
  postMessage: (message: HashWorkerResponse) => void;
};
workerScope.addEventListener('message', (event: MessageEvent<HashWorkerRequest>) => {
  void calculateFileHashes(event.data.file, event.data.chunkSize, (processed, total) => {
    const response: HashWorkerResponse = { type: 'progress', processed, total };
    workerScope.postMessage(response);
  }).then((hashes) => {
    const response: HashWorkerResponse = { type: 'complete', hashes };
    workerScope.postMessage(response);
  }).catch((error: unknown) => {
    const response: HashWorkerResponse = {
      type: 'error',
      message: error instanceof Error ? error.message : 'HASH_FAILED',
    };
    workerScope.postMessage(response);
  });
});
