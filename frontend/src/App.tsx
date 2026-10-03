import { createContext, FormEvent, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import {
  ApiError,
  getCsrfToken,
  getCurrentUser,
  getDeletionProgress,
  getSummary,
  getTask,
  getTranscript,
  getVideo,
  listVideos,
  request,
  requestResult,
} from './api';
import {
  abandonUploadRecovery,
  getUploadRecoveries,
  runResumableUpload,
  type UploadProgress,
  type UploadRecovery,
  uploadErrorMessage,
} from './resumableUpload';
import {
  errorMessage,
  formatBytes,
  formatDate,
  formatDuration,
  shouldPollTask,
  stageLabel,
  taskStatusLabel,
} from './model';
import type {
  AnalysisTask,
  DeletionProgress,
  Summary,
  SummaryItem,
  Transcript,
  User,
  VideoDetail,
  VideoSummary,
} from './types';

interface SessionState {
  user: User | null;
  csrf: string;
  message: string | null;
}

function apiMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return '请求失败，请重试。';
  if (error.status === 0) return '无法连接本地服务。请确认 Stage 2 后端和网页服务正在运行。';
  if (error.status === 401) return '登录状态已失效，请重新登录。';
  if (error.status === 403) return '安全校验失败，请刷新页面后重试。';
  if (error.status === 413) return '请求体超过服务端允许的范围。';
  if (error.status === 408) return '请求超时，请检查服务状态后重试。';
  if (error.status === 409 && error.code) return errorMessage(error.code) ?? `操作暂不可用（${error.code}）。`;
  if (error.code) return errorMessage(error.code) ?? `服务端返回状态：${error.code}`;
  if (error.status >= 500) return '服务端暂时不可用，请稍后重试。';
  return `请求未完成（HTTP ${error.status}）。`;
}

function isSessionExpired(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

function Brand() {
  return (
    <a className="brand" href="/" aria-label="课迹首页">
      <span className="brand-mark" aria-hidden="true">迹</span>
      <span>
        <strong>课迹</strong>
        <small>视频学习工作台</small>
      </span>
    </a>
  );
}

function Header({ username, onLogout }: { username?: string; onLogout?: () => void }) {
  return (
    <header className="topbar">
      <div className="topbar-inner">
        <Brand />
        {username ? (
          <div className="account-menu">
            <span className="account-name"><span className="account-dot" />{username}</span>
            <button className="button button-quiet button-small" type="button" onClick={onLogout}>退出登录</button>
          </div>
        ) : <span className="local-badge">本地运行</span>}
      </div>
    </header>
  );
}

function AuthPage({
  csrf,
  notice,
  onAuthenticated,
  onCsrf,
}: {
  csrf: string;
  notice: string | null;
  onAuthenticated: (user: User, csrf: string) => void;
  onCsrf: (token: string) => void;
}) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [working, setWorking] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(notice);

  useEffect(() => setFeedback(notice), [notice]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setWorking(true);
    setFeedback(null);
    try {
      if (mode === 'register') {
        await request<User>('/auth/register', {
          method: 'POST', csrf, body: { username, password },
        });
        setMode('login');
        setFeedback('账户已创建，请登录继续。');
        return;
      }
      await request<User>('/auth/login', {
        method: 'POST', csrf, body: { username, password },
      });
      const csrfResult = await getCsrfToken();
      const user = await getCurrentUser();
      onCsrf(csrfResult.csrf_token);
      onAuthenticated(user, csrfResult.csrf_token);
    } catch (error) {
      if (isSessionExpired(error)) {
        setFeedback('登录状态已失效，请重新登录。');
      } else if (error instanceof ApiError && error.status === 409) {
        setFeedback('该用户名已被使用，请直接登录或换一个用户名。');
      } else if (error instanceof ApiError && error.status === 422) {
        setFeedback('请检查用户名和密码格式。密码至少 12 个字符。');
      } else {
        setFeedback(apiMessage(error));
      }
    } finally {
      setWorking(false);
    }
  }

  return (
    <div className="auth-shell">
      <Header />
      <main className="auth-layout">
        <section className="auth-intro">
          <span className="eyebrow"><span className="eyebrow-dot" />为学习留下可回看的线索</span>
          <h1>从一段视频，<br /><em>找到讲解的脉络。</em></h1>
          <p>转写、章节与原文引用汇在一处。点开引用，回到视频对应片段核对上下文。</p>
          <div className="auth-points" aria-label="产品能力">
            <div><span className="point-icon">01</span><span><strong>原文可追溯</strong><small>总结附带对应转写片段</small></span></div>
            <div><span className="point-icon">02</span><span><strong>回到视频核对</strong><small>引用定位到原视频时间轴</small></span></div>
            <div><span className="point-icon">03</span><span><strong>仅本地使用</strong><small>账号内容由服务端隔离</small></span></div>
          </div>
        </section>
        <section className="auth-card" aria-labelledby="auth-title">
          <div className="card-kicker">YOUR STUDY SPACE</div>
          <h2 id="auth-title">{mode === 'login' ? '欢迎回来' : '创建你的账户'}</h2>
          <p className="muted">{mode === 'login' ? '登录后继续整理课程视频。' : '创建账户，开始整理你的视频笔记。'}</p>
          <form className="form-stack" onSubmit={submit}>
            <label className="field-label" htmlFor="username">用户名</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              minLength={3}
              maxLength={32}
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="输入用户名"
              disabled={working}
            />
            <label className="field-label" htmlFor="password">密码</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              minLength={mode === 'login' ? 1 : 12}
              maxLength={128}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={mode === 'login' ? '输入密码' : '至少 12 个字符'}
              disabled={working}
            />
            {feedback && <div className="notice" role="status" aria-live="polite">{feedback}</div>}
            <button className="button button-primary button-wide" type="submit" disabled={working || !csrf}>
              {working ? '请稍候…' : mode === 'login' ? '登录' : '创建账户'}
              {!working && <span aria-hidden="true">↗</span>}
            </button>
          </form>
          <div className="auth-switch">
            {mode === 'login' ? '还没有账户？' : '已经有账户？'}
            <button
              type="button"
              onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setFeedback(null); }}
              disabled={working}
            >
              {mode === 'login' ? '创建账户' : '返回登录'}
            </button>
          </div>
          <p className="privacy-note"><span aria-hidden="true">◈</span> 会话由服务端安全 Cookie 管理</p>
        </section>
      </main>
      <footer className="auth-footer">课迹 · 课程视频转写与可追溯总结</footer>
    </div>
  );
}

function UploadPanel({ csrf, userId, onUploaded, onExpired }: {
  csrf: string;
  userId: string;
  onUploaded: (video: VideoDetail) => void;
  onExpired: () => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [working, setWorking] = useState(false);
  const [progress, setProgress] = useState<UploadProgress | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [recoveries, setRecoveries] = useState<UploadRecovery[]>(() => getUploadRecoveries(userId));
  const [abandoning, setAbandoning] = useState<string | null>(null);

  useEffect(() => {
    setRecoveries(getUploadRecoveries(userId));
    return () => uploadAbortRef.current?.abort();
  }, [userId]);

  function cancel() {
    uploadAbortRef.current?.abort();
    setFeedback('正在暂停当前请求；临时会话会保留，重新选择原文件后可续传。');
  }

  async function upload() {
    if (!file || !csrf || working) return;
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    setWorking(true);
    setFeedback(null);
    setProgress({ phase: 'hashing', processed: 0, total: file.size, confirmed: 0, requestSent: 0, requestTotal: 0 });
    try {
      const video = await runResumableUpload(
        userId,
        file,
        csrf,
        controller.signal,
        setProgress,
        () => setRecoveries(getUploadRecoveries(userId)),
        () => setFeedback('所选文件与同名同大小的旧上传内容不同，不会接续旧会话；本次将作为新的上传。'),
      );
      setFile(null);
      if (fileInput.current) fileInput.current.value = '';
      setFeedback('服务端已完成媒体校验并登记视频。');
      onUploaded(video);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      else setFeedback(uploadErrorMessage(error));
    } finally {
      if (uploadAbortRef.current === controller) uploadAbortRef.current = null;
      setWorking(false);
      setRecoveries(getUploadRecoveries(userId));
    }
  }

  async function abandon(recovery: UploadRecovery) {
    if (working || abandoning) return;
    const controller = new AbortController();
    setAbandoning(recovery.idempotencyKey);
    setFeedback(null);
    try {
      const result = await abandonUploadRecovery(
        userId,
        recovery.idempotencyKey,
        csrf,
        controller.signal,
        () => setRecoveries(getUploadRecoveries(userId)),
      );
      setFeedback(result === 'pending'
        ? '放弃请求已受理，服务端仍待完成临时文件清理；该记录会保留以便安全重试。'
        : '临时上传已清理。');
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      else setFeedback(uploadErrorMessage(error));
    } finally {
      setAbandoning(null);
      setRecoveries(getUploadRecoveries(userId));
    }
  }

  const phaseLabels: Record<UploadProgress['phase'], string> = {
    hashing: '正在计算文件摘要',
    creating: '正在准备上传会话',
    checking: '正在查询服务端确认偏移',
    transferring: '正在传输当前分片',
    completing: '分片已确认，服务端正在校验并登记视频',
  };
  const confirmedPercent = progress && progress.total > 0
    ? Math.min(100, Math.round((progress.confirmed / progress.total) * 100))
    : 0;

  return (
    <section className="upload-panel" aria-labelledby="upload-title">
      <div className="upload-copy">
        <span className="section-label">ADD A VIDEO</span>
        <h2 id="upload-title">把一堂课放进来</h2>
        <p>使用原始文件上传。可播放格式为 MP4、H.264 视频与 AAC 音轨，时长不超过 20 分钟。</p>
        <p className="subtle-note">文件大小由磁盘和服务端写入能力决定，本页面不设字节数门槛。中断后重新选择相同文件，会先核对摘要并按服务端确认偏移续传。</p>
      </div>
      <div className="upload-actions">
        <input
          ref={fileInput}
          className="visually-hidden"
          id="video-file"
          type="file"
          accept="video/mp4,.mp4"
          disabled={working || abandoning !== null}
          onChange={(event) => { setFile(event.target.files?.[0] ?? null); setFeedback(null); }}
        />
        <label className="button button-outline choose-file" htmlFor="video-file">
          <span aria-hidden="true">＋</span>{file ? '更换文件' : '选择视频文件'}
        </label>
        {file && <div className="selected-file"><strong>{file.name}</strong><span>{formatBytes(file.size)}</span></div>}
        {working && progress ? (
          <div className="upload-progress" aria-live="polite">
            <div className="progress-copy">
              <span>{phaseLabels[progress.phase]}</span>
              <span>服务端已确认 {formatBytes(progress.confirmed)} / {formatBytes(progress.total)}</span>
            </div>
            <progress max={100} value={confirmedPercent} aria-label="服务端已确认上传进度" />
            {progress.phase === 'hashing' && <small>摘要计算：{formatBytes(progress.processed)} / {formatBytes(progress.total)}</small>}
            {progress.phase === 'transferring' && progress.requestTotal > 0 && (
              <small>当前请求已发送 {formatBytes(progress.requestSent)} / {formatBytes(progress.requestTotal)}</small>
            )}
            <button className="text-button" type="button" onClick={cancel}>暂停上传</button>
          </div>
        ) : (
          <button className="button button-primary" type="button" onClick={() => void upload()} disabled={!file || !csrf || abandoning !== null}>
            上传 / 继续上传 <span aria-hidden="true">↑</span>
          </button>
        )}
        {recoveries.length > 0 && (
          <div className="recovery-list" aria-label="待恢复的临时上传">
            <p>此账户有 {recoveries.length} 个临时上传。选择原文件并点击“上传 / 继续上传”后，页面会校验完整摘要。</p>
            {recoveries.map((recovery) => (
              <div className="recovery-item" key={recovery.idempotencyKey}>
                <span>{recovery.fileName} · {formatBytes(recovery.totalBytes)}</span>
                <button
                  className="text-button"
                  type="button"
                  onClick={() => void abandon(recovery)}
                  disabled={working || abandoning !== null}
                >
                  {abandoning === recovery.idempotencyKey ? '正在放弃…' : '放弃临时上传'}
                </button>
              </div>
            ))}
          </div>
        )}
        {feedback && <p className="form-feedback" role="status" aria-live="polite">{feedback}</p>}
      </div>
    </section>
  );
}

function useDeletionPolling(
  videoId: string | null,
  onProgress: (progress: DeletionProgress) => void,
  onComplete: () => void,
  onExpired: () => void,
  setError: (message: string | null) => void,
) {
  const read = useCallback(async () => {
    if (!videoId) return;
    try {
      const progress = await getDeletionProgress(videoId);
      onProgress(progress);
      setError(errorMessage(progress.error_code));
    } catch (error) {
      if (isSessionExpired(error)) { onExpired(); return; }
      if (error instanceof ApiError && error.status === 404) { onComplete(); return; }
      setError(apiMessage(error));
    }
  }, [onComplete, onExpired, onProgress, setError, videoId]);

  useEffect(() => {
    if (!videoId) return undefined;
    const poll = () => {
      if (document.visibilityState === 'visible') void read();
    };
    const onVisibility = () => {
      if (document.visibilityState === 'visible') void read();
    };
    const timer = window.setInterval(poll, 2000);
    document.addEventListener('visibilitychange', onVisibility);
    void read();
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [read, videoId]);
  return read;
}

function VideoLibrary({ csrf, userId, onExpired, username, onLogout }: { csrf: string; userId: string; onExpired: () => void; username: string; onLogout: () => void }) {
  const [videos, setVideos] = useState<VideoSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingError, setLoadingError] = useState<string | null>(null);
  const [busyVideoId, setBusyVideoId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deletion, setDeletion] = useState<DeletionProgress | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [deletionError, setDeletionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadingError(null);
    try {
      const result = await listVideos();
      setVideos(result.items);
      setDeletingId((current) => current ?? result.items.find((item) => item.status !== 'ready')?.id ?? null);
    } catch (error) {
      if (isSessionExpired(error)) { onExpired(); return; }
      setLoadingError(apiMessage(error));
    } finally {
      setLoading(false);
    }
  }, [onExpired]);

  useEffect(() => { void refresh(); }, [refresh]);

  const deletionProgress = useCallback((progress: DeletionProgress) => {
    setDeletion(progress);
    if (progress.state === 'ready') setDeletingId(null);
  }, []);
  const deletionComplete = useCallback(() => {
    if (!deletingId) return;
    setVideos((items) => items.filter((item) => item.id !== deletingId));
    setDeletingId(null);
    setDeletion(null);
    setDeletionError(null);
    setFeedback('视频记录及关联数据已删除。');
  }, [deletingId]);
  useDeletionPolling(deletingId, deletionProgress, deletionComplete, onExpired, setDeletionError);

  async function remove(videoId: string) {
    setBusyVideoId(videoId);
    setFeedback(null);
    setDeletionError(null);
    try {
      const result = await requestResult<void>(`/videos/${encodeURIComponent(videoId)}`, {
        method: 'DELETE', csrf,
      });
      if (result.status === 204) {
        setVideos((items) => items.filter((item) => item.id !== videoId));
        setDeletingId(null);
        setDeletion(null);
        setFeedback('视频记录及关联数据已删除。');
      } else if (result.status === 202) {
        setDeletingId(videoId);
        setFeedback('删除正在进行。此期间会按后端进度查询，不再读取视频内容。');
      }
    } catch (error) {
      if (isSessionExpired(error)) { onExpired(); return; }
      setDeletingId(null);
      setDeletionError(apiMessage(error));
    } finally {
      setBusyVideoId(null);
    }
  }

  return (
    <div className="app-shell">
      <Header username={username} onLogout={onLogout} />
      <main className="library-main">
        <div className="page-heading">
          <div><span className="section-label">YOUR LIBRARY</span><h1>课程视频</h1><p>上传课程视频，查看转写与带来源的总结。</p></div>
          <div className="library-count"><strong>{videos.length.toString().padStart(2, '0')}</strong><span>段视频</span></div>
        </div>
        <UploadPanel
          csrf={csrf}
          userId={userId}
          onExpired={onExpired}
          onUploaded={(video) => { window.location.assign(`/videos/${encodeURIComponent(video.id)}`); }}
        />
        {feedback && <div className="notice notice-success" role="status">{feedback}</div>}
        {deletionError && <div className="notice" role="alert">{deletionError}</div>}
        <section className="library-section" aria-labelledby="library-title">
          <div className="section-heading"><div><span className="section-label">RECENT</span><h2 id="library-title">最近添加</h2></div><button className="text-button" type="button" onClick={() => void refresh()} disabled={loading}>刷新列表 ↻</button></div>
          {loading ? <div className="loading-row"><span className="spinner" />正在读取视频列表…</div> : loadingError ? (
            <div className="empty-state"><div className="empty-icon">!</div><h3>暂时无法加载</h3><p>{loadingError}</p><button className="button button-outline" type="button" onClick={() => void refresh()}>重试</button></div>
          ) : videos.length === 0 ? (
            <div className="empty-state"><div className="empty-icon">↗</div><h3>这里还没有课程视频</h3><p>选择一段已验证的视频文件，上传后即可查看处理进度与总结。</p></div>
          ) : (
            <div className="video-grid">
              {videos.map((video) => {
                const isDeleting = deletingId === video.id || video.status !== 'ready';
                return (
                  <article className={`video-card${isDeleting ? ' video-card-deleting' : ''}`} key={video.id}>
                    <div className="video-card-art" aria-hidden="true"><span className="play-glyph">▶</span><span className="art-time">{formatDuration(video.duration_seconds)}</span></div>
                    <div className="video-card-body">
                      <div className="video-card-top"><span className={`status-chip ${isDeleting ? 'status-wait' : 'status-ready'}`}><i />{isDeleting ? '删除处理中' : '可用'}</span><time>{formatDate(video.created_at)}</time></div>
                      <a className="video-title" href={`/videos/${encodeURIComponent(video.id)}`}>{video.display_name}</a>
                      <div className="video-meta"><span>{formatBytes(video.byte_count)}</span><span className="meta-divider" /> <span>{formatDuration(video.duration_seconds)}</span></div>
                      <div className="video-card-actions">
                        <a className="text-link" href={`/videos/${encodeURIComponent(video.id)}`}>打开详情 <span aria-hidden="true">→</span></a>
                        {isDeleting ? (
                          <span className="deletion-inline">{deletion?.video_id === video.id && deletion.stop_confirmation_required ? '等待维护确认' : '按后端状态处理中'}</span>
                        ) : (
                          <button className="text-button text-danger" type="button" disabled={busyVideoId === video.id} onClick={() => void remove(video.id)}>
                            {busyVideoId === video.id ? '正在删除…' : '删除'}
                          </button>
                        )}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
        {deletingId && deletion?.video_id === deletingId && deletion.state !== 'ready' && (
          <div className="deletion-banner" role="status" aria-live="polite">
            <div><strong>删除尚未完成</strong><p>{errorMessage(deletion.error_code) ?? '后端正在清理文件和关联数据。'}</p>{deletion.stop_confirmation_required && <p>请只按 VideoClip Stage 2 Worker 停止与确认步骤操作。</p>}</div>
            <button className="button button-outline button-small" type="button" disabled={busyVideoId === deletingId} onClick={() => void remove(deletingId)}>重试删除</button>
          </div>
        )}
      </main>
      <footer className="site-footer"><span>课迹 · 本地课程视频工作台</span><span>内容仅由所属账号读取</span></footer>
    </div>
  );
}

type SeekHandler = (seconds: number) => void;
const PlayerSeekContext = createContext<SeekHandler | null>(null);

function MediaPlayer({ videoId, onMessage, onSeekReady }: {
  videoId: string;
  onMessage: (message: string | null) => void;
  onSeekReady: (handler: SeekHandler) => void;
}) {
  const player = useRef<HTMLVideoElement>(null);

  useEffect(() => () => {
    const element = player.current;
    if (!element) return;
    element.pause();
    element.removeAttribute('src');
    element.load();
  }, []);

  const seekAndPlay = useCallback(async (seconds: number) => {
    const element = player.current;
    if (!element) return;
    onMessage(null);
    try {
      const readyUntil = Date.now() + 10_000;
      while (element.readyState < HTMLMediaElement.HAVE_METADATA || element.seekable.length === 0) {
        if (element.error) throw new Error('MEDIA_LOAD_FAILED');
        if (Date.now() >= readyUntil) throw new Error('MEDIA_METADATA_TIMEOUT');
        await new Promise((resolve) => window.setTimeout(resolve, 100));
      }
      const duration = Number.isFinite(element.duration) ? element.duration : seconds;
      const target = Math.max(0, Math.min(seconds, duration));
      const seekable = element.seekable;
      if (seekable.length > 0 && (target < seekable.start(0) - 0.5 || target > seekable.end(seekable.length - 1) + 0.5)) {
        const seekUntil = Date.now() + 10_000;
        while (Date.now() < seekUntil && (target < element.seekable.start(0) - 0.5 || target > element.seekable.end(element.seekable.length - 1) + 0.5)) {
          if (element.error) throw new Error('MEDIA_LOAD_FAILED');
          await new Promise((resolve) => window.setTimeout(resolve, 100));
        }
      }
      if (Math.abs(element.currentTime - target) > 0.25) {
        const seeked = new Promise<void>((resolve, reject) => {
          const timeout = window.setTimeout(() => { cleanup(); reject(new Error('MEDIA_SEEK_TIMEOUT')); }, 10_000);
          const onSeeked = () => { cleanup(); resolve(); };
          const onError = () => { cleanup(); reject(new Error('MEDIA_LOAD_FAILED')); };
          const cleanup = () => {
            window.clearTimeout(timeout);
            element.removeEventListener('seeked', onSeeked);
            element.removeEventListener('error', onError);
          };
          element.addEventListener('seeked', onSeeked, { once: true });
          element.addEventListener('error', onError, { once: true });
        });
        element.currentTime = target;
        await seeked;
      }
      await element.play();
      onMessage(`已跳到约 ${formatDuration(target)} 的转写片段并开始播放。`);
    } catch (error) {
      const message = error instanceof Error ? error.message : '';
      if (message === 'MEDIA_LOAD_FAILED') onMessage('视频加载失败，请检查本地文件是否仍可读取。');
      else if (message === 'MEDIA_SEEK_TIMEOUT' || message === 'MEDIA_METADATA_TIMEOUT') onMessage('播放器暂时无法定位到该片段，请稍后重试。');
      else onMessage('浏览器未能开始播放。请使用播放器控件重试或检查浏览器播放权限。');
    }
  }, [onMessage]);

  useEffect(() => {
    onSeekReady(seekAndPlay);
    return () => onSeekReady(() => undefined);
  }, [onSeekReady, seekAndPlay]);

  return (
    <div className="player-shell">
      <video
        ref={player}
        src={`/api/videos/${encodeURIComponent(videoId)}/content`}
        controls
        preload="metadata"
        playsInline
        aria-label="课程视频播放器"
        onError={() => onMessage('视频加载失败，请检查本地文件是否仍可读取。')}
      />
      <span className="visually-hidden">播放器就绪</span>
    </div>
  );
}

function EvidenceButton({ item, label }: { item: SummaryItem; label: string }) {
  const seek = useContext(PlayerSeekContext);
  const evidence = item.evidence[0];
  if (!evidence) return null;
  return (
    <div className="evidence-row">
      <blockquote>{evidence.quote}</blockquote>
      <div className="evidence-actions">
        <span className="time-range">{formatDuration(evidence.start_seconds)} – {formatDuration(evidence.end_seconds)}</span>
        <button className="cite-button" type="button" onClick={() => seek?.(evidence.start_seconds)} aria-label={`回看${label}引用，约 ${formatDuration(evidence.start_seconds)}`}>
          <span aria-hidden="true">▷</span> 回看引用
        </button>
      </div>
    </div>
  );
}

function SummaryContent({ summary }: { summary: Summary }) {
  return (
    <div className="summary-content">
      <section className="summary-group" aria-labelledby="chapter-heading">
        <div className="section-heading compact-heading"><div><span className="section-label">CHAPTERS</span><h3 id="chapter-heading">章节脉络</h3></div><span className="count-pill">{summary.payload.chapters.length} 章</span></div>
        <div className="chapter-list">
          {summary.payload.chapters.map((chapter, index) => (
            <article className="summary-item chapter-item" key={`${index}-${chapter.title ?? ''}`}>
              <span className="item-index">{String(index + 1).padStart(2, '0')}</span>
              <div className="item-main"><h4>{chapter.title}</h4><EvidenceButton item={chapter} label="章节" /></div>
            </article>
          ))}
        </div>
      </section>
      <section className="summary-group" aria-labelledby="keypoints-heading">
        <div className="section-heading compact-heading"><div><span className="section-label">KEY POINTS</span><h3 id="keypoints-heading">知识要点</h3></div><span className="count-pill">{summary.payload.key_points.length} 条</span></div>
        <div className="keypoint-list">
          {summary.payload.key_points.map((point, index) => (
            <article className="summary-item keypoint-item" key={`${index}-${point.text ?? ''}`}>
              <span className="keypoint-mark" aria-hidden="true">✳</span>
              <div className="item-main"><h4>{point.text}</h4><EvidenceButton item={point} label="要点" /></div>
            </article>
          ))}
        </div>
      </section>
      <p className="summary-footnote">引用定位到对应转写片段的时间区间，提供片段级回看，不代表词级时间戳。</p>
    </div>
  );
}

function VideoDetailPage({ videoId, csrf, onExpired, username, onLogout }: { videoId: string; csrf: string; onExpired: () => void; username: string; onLogout: () => void }) {
  const [video, setVideo] = useState<VideoDetail | null>(null);
  const [task, setTask] = useState<AnalysisTask | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const deletingRef = useRef(false);
  const [deletion, setDeletion] = useState<DeletionProgress | null>(null);
  const [deletionError, setDeletionError] = useState<string | null>(null);
  const [busy, setBusy] = useState<'submit' | 'recover' | 'summarize' | 'cleanup' | 'delete' | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [summaryBlocked, setSummaryBlocked] = useState(false);
  const [playerMessage, setPlayerMessage] = useState<string | null>(null);
  const playerSeek = useRef<SeekHandler>(() => undefined);
  const registerSeek = useCallback((handler: SeekHandler) => { playerSeek.current = handler; }, []);

  const completeDeletion = useCallback(() => { window.location.assign('/'); }, []);
  const updateDeletion = useCallback((progress: DeletionProgress) => {
    setDeletion(progress);
    setDeletionError(errorMessage(progress.error_code));
  }, []);
  const reportDeletionError = useCallback((message: string | null) => setDeletionError(message), []);
  const loadDeletion = useDeletionPolling(deleting ? videoId : null, updateDeletion, completeDeletion, onExpired, reportDeletionError);

  const refresh = useCallback(async () => {
    if (deletingRef.current) { await loadDeletion(); return; }
    setError(null);
    try {
      const detail = await getVideo(videoId);
      if (detail.status !== 'ready') {
        deletingRef.current = true;
        setDeleting(true);
        await getDeletionProgress(videoId).then(updateDeletion).catch((failure: unknown) => {
          if (isSessionExpired(failure)) onExpired();
          else if (failure instanceof ApiError && failure.status === 404) completeDeletion();
          else setDeletionError(apiMessage(failure));
        });
        return;
      }
      setVideo(detail);
      if (!detail.analysis_task_id) {
        if (detail.analysis_task_exists) throw new ApiError(503, 'ANALYSIS_TASK_ASSOCIATION_MISSING');
        setTask(null);
        setTranscript(null);
        setSummary(null);
        return;
      }
      const currentTask = await getTask(videoId, detail.analysis_task_id);
      setTask(currentTask);
      try {
        setTranscript(await getTranscript(videoId));
      } catch (failure) {
        if (isSessionExpired(failure)) { onExpired(); return; }
        if (!(failure instanceof ApiError && (failure.status === 404 || failure.status === 409))) throw failure;
        setTranscript(null);
      }
      if (currentTask.status === 'succeeded' && currentTask.summary_status === 'succeeded') {
        setSummary(await getSummary(videoId));
      } else {
        setSummary(null);
      }
    } catch (failure) {
      if (isSessionExpired(failure)) { onExpired(); return; }
      if (failure instanceof ApiError && failure.status === 404) setNotFound(true);
      else if (failure instanceof ApiError && failure.code === 'VIDEO_DELETION_IN_PROGRESS') {
        deletingRef.current = true;
        setDeleting(true);
      } else setError(apiMessage(failure));
    } finally {
      setLoading(false);
    }
  }, [completeDeletion, loadDeletion, onExpired, updateDeletion, videoId]);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!task || !shouldPollTask(task) || deleting) return undefined;
    const poll = () => { if (document.visibilityState === 'visible') void refresh(); };
    const onVisibility = () => { if (document.visibilityState === 'visible') void refresh(); };
    const timer = window.setInterval(poll, 2000);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [deleting, refresh, task]);

  async function startAnalysis() {
    setBusy('submit');
    setActionError(null);
    try {
      await request<AnalysisTask>(`/videos/${encodeURIComponent(videoId)}/tasks`, { method: 'POST', csrf });
      await refresh();
    } catch (failure) {
      if (isSessionExpired(failure)) onExpired();
      else setActionError(apiMessage(failure));
    } finally { setBusy(null); }
  }

  async function continueTask(action: 'recover' | 'summarize') {
    if (!task || busy) return;
    setBusy(action);
    setActionError(null);
    try {
      const endpoint = action === 'recover' ? 'recover' : 'summarize';
      await request<AnalysisTask>(
        `/videos/${encodeURIComponent(videoId)}/tasks/${encodeURIComponent(task.id)}/${endpoint}`,
        { method: 'POST', csrf, body: { attempt_id: task.current_attempt_id } },
      );
      await refresh();
    } catch (failure) {
      if (isSessionExpired(failure)) { onExpired(); return; }
      if (failure instanceof ApiError && failure.code === 'TRANSCRIPT_SOURCE_MISMATCH') {
        setSummaryBlocked(true);
        setActionError(errorMessage(failure.code));
      } else if (failure instanceof ApiError && failure.code === 'SUMMARY_CONFIGURATION_INCOMPATIBLE') {
        setSummaryBlocked(true);
        setActionError(errorMessage(failure.code));
      } else setActionError(apiMessage(failure));
      await refresh();
    } finally { setBusy(null); }
  }

  async function retryCleanup() {
    if (!task || busy) return;
    setBusy('cleanup');
    setActionError(null);
    try {
      await request(`/videos/${encodeURIComponent(videoId)}/cleanup`, {
        method: 'POST', csrf, body: { attempt_id: task.current_attempt_id },
      });
      await refresh();
    } catch (failure) {
      if (isSessionExpired(failure)) onExpired();
      else setActionError(apiMessage(failure));
    } finally { setBusy(null); }
  }

  async function deleteRecord() {
    if (busy) return;
    setBusy('delete');
    setActionError(null);
    setPlayerMessage(null);
    deletingRef.current = true;
    setDeleting(true);
    try {
      const result = await requestResult<void>(`/videos/${encodeURIComponent(videoId)}`, { method: 'DELETE', csrf });
      if (result.status === 204) completeDeletion();
      else if (result.status === 202) {
        setDeletionError('删除处理中。此期间只读取删除进度，不加载媒体或文字内容。');
        await loadDeletion();
      }
    } catch (failure) {
      if (isSessionExpired(failure)) { onExpired(); return; }
      setDeletionError(apiMessage(failure));
      if (failure instanceof ApiError && failure.status !== 0) {
        deletingRef.current = false;
        setDeleting(false);
      }
    } finally { setBusy(null); }
  }

  if (loading) return <div className="app-shell"><Header username={username} onLogout={onLogout} /><main className="detail-main"><div className="loading-row"><span className="spinner" />正在读取后端状态…</div></main></div>;
  if (notFound) return <div className="app-shell"><Header username={username} onLogout={onLogout} /><main className="detail-main"><div className="empty-state"><div className="empty-icon">?</div><h2>找不到这段视频</h2><p>视频不存在，或当前账号没有读取权限。</p><a className="button button-outline" href="/">返回视频库</a></div></main></div>;
  if (deleting) {
    return (
      <div className="app-shell">
        <Header username={username} onLogout={onLogout} />
        <main className="detail-main deletion-view">
          <a className="back-link" href="/">← 返回视频库</a>
          <span className="section-label">DELETION STATUS</span>
          <h1>正在删除这段视频</h1>
          <p>删除尚未被后端证明完成。页面不会读取播放器、转写或总结。</p>
          {deletion && <div className="task-card"><StatusRow label="删除状态" value={deletion.state === 'ready' ? '仍在等待后端清理' : '处理中'} /><StatusRow label="待处理 attempt" value={String(deletion.pending_attempt_ids.length)} />{deletion.stop_confirmation_required && <NoticePanel code="STOP_CONFIRMATION_REQUIRED" />}</div>}
          {(deletionError || actionError) && <div className="notice" role="alert">{deletionError ?? actionError}</div>}
          <div className="deletion-actions"><button className="button button-outline" type="button" onClick={() => void loadDeletion()}>刷新删除进度</button><button className="button button-danger" type="button" disabled={busy === 'delete'} onClick={() => void deleteRecord()}>{busy === 'delete' ? '正在请求…' : '重试删除'}</button></div>
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <Header username={username} onLogout={onLogout} />
      <main className="detail-main">
        <a className="back-link" href="/">← 返回视频库</a>
        {error ? <div className="notice" role="alert">{error}<button className="text-button" type="button" onClick={() => void refresh()}>重试</button></div> : null}
        {actionError && <div className="notice" role="alert">{actionError}</div>}
        {video && (
          <>
            <div className="detail-heading">
              <div className="detail-title-block"><span className="section-label">VIDEO NOTE</span><h1>{video.display_name}</h1><p>{formatDuration(video.duration_seconds)} <span>·</span> {formatBytes(video.byte_count)} <span>·</span> 添加于 {formatDate(video.created_at)}</p></div>
              <button className="button button-danger button-small" type="button" onClick={() => void deleteRecord()} disabled={busy === 'delete'}>{busy === 'delete' ? '正在删除…' : '删除记录'}</button>
            </div>
            <PlayerSeekContext.Provider value={(seconds) => playerSeek.current(seconds)}>
            <div className="detail-grid">
              <div className="detail-main-column">
                <section className="player-card" aria-label="视频回看">
                  <div className="player-card-head"><span className="section-label">WATCH & VERIFY</span><span className="media-kind"><i /> 已验证媒体</span></div>
                  <MediaPlayer videoId={video.id} onMessage={setPlayerMessage} onSeekReady={registerSeek} />
                  {playerMessage && <p className="player-message" role="status" aria-live="polite">{playerMessage}</p>}
                </section>
                <section className="transcript-card" aria-labelledby="transcript-title">
                  <div className="section-heading"><div><span className="section-label">TRANSCRIPT</span><h2 id="transcript-title">语音转写</h2></div><span className="count-pill">{transcript?.items.length ?? 0} 段</span></div>
                  {!task ? <EmptyInline title="开始分析后显示转写" body="提交分析后，服务端会按片段保存转写进度。" /> : transcript?.items.length ? (
                    <div className="transcript-list">
                      {transcript.items.map((segment) => (
                        <article className="transcript-row" key={segment.segment_number}>
                          <button className="transcript-time" type="button" onClick={() => playerSeek.current(segment.video_start_seconds)} aria-label={`回看第 ${segment.segment_number + 1} 段转写`}>
                            <span>{formatDuration(segment.video_start_seconds)}</span><small>{formatDuration(segment.video_end_seconds)}</small>
                          </button>
                          <div className="transcript-text">{segment.text ?? <span className="muted">该片段尚未完成</span>}</div>
                        </article>
                      ))}
                    </div>
                  ) : <EmptyInline title="转写尚未开始" body="片段及进度以服务端当前状态为准。" />}
                </section>
              </div>
              <aside className="detail-side-column">
                <AnalysisPanel
                  task={task}
                  csrf={csrf}
                  busy={busy}
                  onStart={startAnalysis}
                  onRecover={() => void continueTask('recover')}
                  onSummarize={() => void continueTask('summarize')}
                  onCleanup={() => void retryCleanup()}
                  transcriptComplete={Boolean(transcript?.complete)}
                  summaryBlocked={summaryBlocked}
                />
                {summary ? <section className="summary-card" aria-labelledby="summary-title">
                  <div className="summary-card-header"><span className="summary-spark" aria-hidden="true">✳</span><div><span className="section-label">VERIFIED SUMMARY</span><h2 id="summary-title">课程总结</h2></div><span className="status-chip status-ready"><i />已发布</span></div>
                  <SummaryContent summary={summary} />
                </section> : task?.status === 'succeeded' ? (
                  <section className="summary-card"><div className="empty-state compact-empty"><div className="spinner" /><h3>正在读取总结</h3><p>结果以所属用户的总结接口为准。</p></div></section>
                ) : (
                  <section className="summary-placeholder"><span className="placeholder-icon">✳</span><h3>{task?.summary_status === 'failed' ? '总结生成失败' : task?.summary_status === 'waiting' ? '总结尚未发布' : '总结将在验证后显示'}</h3><p>只有后端验证并持久化的合法总结会出现在这里。</p></section>
                )}
              </aside>
            </div>
            </PlayerSeekContext.Provider>
          </>
        )}
      </main>
      <footer className="site-footer"><span>引用可定位到转写片段起点</span><span>语义完整性需结合原视频判断</span></footer>
    </div>
  );
}

function AnalysisPanel({ task, csrf, busy, onStart, onRecover, onSummarize, onCleanup, transcriptComplete, summaryBlocked }: {
  task: AnalysisTask | null;
  csrf: string;
  busy: 'submit' | 'recover' | 'summarize' | 'cleanup' | 'delete' | null;
  onStart: () => void;
  onRecover: () => void;
  onSummarize: () => void;
  onCleanup: () => void;
  transcriptComplete: boolean;
  summaryBlocked: boolean;
}) {
  const unavailable = !csrf || busy !== null;
  const canContinueSummary = Boolean(task && task.status === 'awaiting_summary' && task.summary_status === 'waiting' && task.execution_state === 'stopped' && !summaryBlocked);
  const canRecover = Boolean(task?.recovery_eligible);
  const canCleanup = task?.attempt.cleanup_state === 'failed';
  return (
    <section className="analysis-card" aria-labelledby="analysis-title">
      <div className="analysis-card-header"><div><span className="section-label">ANALYSIS</span><h2 id="analysis-title">处理状态</h2></div>{task && <span className={`status-chip ${task.status === 'succeeded' ? 'status-ready' : task.status === 'failed' ? 'status-failed' : 'status-wait'}`}><i />{taskStatusLabel(task.status)}</span>}</div>
      {!task ? (
        <div className="analysis-start"><p>将视频提交给后端，依次完成转写、总结校验与结果发布。</p><button className="button button-primary button-wide" type="button" onClick={onStart} disabled={unavailable}>{busy === 'submit' ? '正在提交…' : '开始分析'} <span aria-hidden="true">↗</span></button></div>
      ) : (
        <>
          <div className="stage-line"><span className={`stage-marker ${task.status}`} /><div><strong>{stageLabel(task.stage)}</strong><small>{task.stage}</small></div></div>
          <div className="progress-facts"><div><strong>{task.expected_segments === 0 ? '—' : `${task.completed_segments} / ${task.expected_segments}`}</strong><span>已完成片段</span></div><div><strong>{task.delivery_status === 'sent' ? '已投递' : task.delivery_status === 'pending' ? '等待投递' : task.delivery_status === 'unknown' ? '结果不确定' : task.delivery_status === 'failed' ? '投递失败' : '状态未知'}</strong><span>队列投递</span></div></div>
          {task.delivery_error_code && <p className="inline-error">{errorMessage(task.delivery_error_code)}</p>}
          {task.failure_code && <p className="inline-error">{errorMessage(task.failure_code) ?? `处理原因：${task.failure_code}`}</p>}
          {task.summary_status === 'running' && <p className="summary-running"><span className="spinner spinner-small" />正在生成总结，结果尚未发布。</p>}
          {task.status === 'awaiting_summary' && task.summary_status === 'waiting' && <div className="awaiting-box"><strong>转写已完成</strong><p>{transcriptComplete ? '所有已保存片段已由后端确认。' : '后端正在确认完整转写来源。'}</p></div>}
          <StatusRow label="执行者停止事实" value={task.execution_state === 'not_started' ? '未领取' : task.execution_state === 'stopped' ? '后端已确认停止' : '停止尚未确认'} />
          <StatusRow label="总结状态" value={task.summary_status === 'not_started' ? '尚未开始' : task.summary_status === 'waiting' ? '等待总结' : task.summary_status === 'running' ? '生成中' : task.summary_status === 'failed' ? '失败' : task.summary_status === 'succeeded' ? '已发布' : '未知'} />
          {task.recovery_error_code === 'STOP_CONFIRMATION_REQUIRED' && <NoticePanel code={task.recovery_error_code} />}
          {task.execution_state === 'stop_unconfirmed' && task.recovery_error_code !== 'STOP_CONFIRMATION_REQUIRED' && <NoticePanel code="STOP_CONFIRMATION_REQUIRED" />}
          {task.attempt.cleanup_state !== 'cleaned' && <div className={`cleanup-line ${task.attempt.cleanup_state === 'failed' ? 'cleanup-failed' : ''}`}><span>临时文件清理：{task.attempt.cleanup_state === 'failed' ? '失败' : task.attempt.cleanup_state === 'cleaned' ? '已完成' : '待处理'}</span>{task.attempt.cleanup_error_code && <small>{errorMessage(task.attempt.cleanup_error_code) ?? task.attempt.cleanup_error_code}</small>}{canCleanup && <button className="text-button" type="button" onClick={onCleanup} disabled={unavailable}>{busy === 'cleanup' ? '清理中…' : '重试清理'}</button>}</div>}
          {canContinueSummary && <button className="button button-primary button-wide action-button" type="button" onClick={onSummarize} disabled={unavailable}>{busy === 'summarize' ? '正在请求…' : '继续生成总结'} <span aria-hidden="true">↗</span></button>}
          {canRecover && <button className="button button-primary button-wide action-button" type="button" onClick={onRecover} disabled={unavailable}>{busy === 'recover' ? '正在恢复…' : '手动恢复分析'} <span aria-hidden="true">↻</span></button>}
          {task.recovery_error_code && task.recovery_error_code !== 'TRANSCRIPT_COMPLETE_AWAITING_SUMMARY' && task.recovery_error_code !== 'STOP_CONFIRMATION_REQUIRED' && !task.recovery_eligible && <p className="recovery-hint">{errorMessage(task.recovery_error_code)}</p>}
          <p className="model-note">ASR：{task.asr_model}</p>
        </>
      )}
    </section>
  );
}

function StatusRow({ label, value }: { label: string; value: string }) {
  return <div className="status-row"><span>{label}</span><strong>{value}</strong></div>;
}

function NoticePanel({ code }: { code: string }) {
  return <div className="maintenance-notice"><span aria-hidden="true">!</span><p>{errorMessage(code)}</p></div>;
}

function EmptyInline({ title, body }: { title: string; body: string }) {
  return <div className="inline-empty"><span aria-hidden="true">—</span><div><strong>{title}</strong><p>{body}</p></div></div>;
}

export default function App() {
  const [session, setSession] = useState<SessionState>({ user: null, csrf: '', message: null });
  const [uploadSessionEpoch, setUploadSessionEpoch] = useState(0);
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);
  const [pathname, setPathname] = useState(window.location.pathname);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const csrf = await getCsrfToken();
        if (active) setSession((current) => ({ ...current, csrf: csrf.csrf_token }));
        try {
          const user = await getCurrentUser();
          if (active) setSession((current) => ({ ...current, user }));
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 401)) throw error;
        }
      } catch (error) {
        if (active) setBootError(apiMessage(error));
      } finally {
        if (active) setBooting(false);
      }
    })();
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const onPopState = () => setPathname(window.location.pathname);
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  const sessionExpired = useCallback(() => {
    setUploadSessionEpoch((value) => value + 1);
    setSession({ user: null, csrf: '', message: '登录状态已失效，请重新登录。' });
    window.history.replaceState(null, '', '/');
    setPathname('/');
    setBooting(true);
    void getCsrfToken().then(({ csrf_token }) => {
      setSession((current) => ({ ...current, csrf: csrf_token }));
      setBooting(false);
    }).catch(() => setBooting(false));
  }, []);

  const authenticated = useCallback((user: User, csrf: string) => {
    setSession({ user, csrf, message: null });
    setBootError(null);
  }, []);

  const logout = useCallback(async () => {
    setUploadSessionEpoch((value) => value + 1);
    try {
      await request<void>('/auth/logout', { method: 'POST', csrf: session.csrf });
      const csrf = await getCsrfToken().catch(() => ({ csrf_token: '' }));
      setSession({ user: null, csrf: csrf.csrf_token, message: '已安全退出登录。' });
      window.history.replaceState(null, '', '/');
      setPathname('/');
    } catch (error) {
      if (isSessionExpired(error)) sessionExpired();
      else setBootError(apiMessage(error));
    }
  }, [session.csrf, sessionExpired]);

  const videoId = useMemo(() => {
    const match = pathname.match(/^\/videos\/([^/]+)\/?$/);
    return match ? decodeURIComponent(match[1]) : null;
  }, [pathname]);

  if (booting) return <div className="app-shell"><Header /><main className="loading-screen"><span className="spinner" />正在恢复安全会话…</main></div>;
  if (!session.user) {
    return <AuthPage csrf={session.csrf} notice={session.message ?? bootError} onAuthenticated={authenticated} onCsrf={(csrf) => setSession((current) => ({ ...current, csrf }))} />;
  }
  return videoId ? (
    <VideoDetailPage key={`${videoId}:${uploadSessionEpoch}`} videoId={videoId} csrf={session.csrf} onExpired={sessionExpired} username={session.user.username} onLogout={() => void logout()} />
  ) : (
    <VideoLibrary key={`${session.user.id}:${uploadSessionEpoch}`} csrf={session.csrf} userId={session.user.id} onExpired={sessionExpired} username={session.user.username} onLogout={() => void logout()} />
  );
}
