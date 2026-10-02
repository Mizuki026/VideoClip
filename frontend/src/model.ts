import type { AnalysisTask } from './types';

const taskLabels: Record<string, string> = {
  queued: '等待处理',
  running: '正在处理',
  awaiting_summary: '转写已完成，等待总结',
  failed: '处理失败',
  succeeded: '分析完成',
};

const stageLabels: Record<string, string> = {
  queued: '等待 Worker 领取',
  validating_source: '正在验证视频',
  extracting_audio: '正在提取音频',
  preparing_manifest: '正在准备片段',
  transcribing: '正在转写',
  awaiting_summary: '转写完成',
  queued_summary: '等待生成总结',
  summarizing: '正在生成总结',
  failed: '处理已停止',
  succeeded: '总结已发布',
};

const errorLabels: Record<string, string> = {
  TRANSCRIPT_COMPLETE_AWAITING_SUMMARY: '转写已完成，等待安全校验和总结。',
  TRANSCRIPT_SOURCE_MISMATCH: '历史转写来源校验未通过。记录已保留，无法安全生成总结。',
  TRANSCRIPT_MANIFEST_INCOMPLETE: '转写片段不完整，暂不能生成总结。',
  SUMMARY_CONFIGURATION_INCOMPATIBLE: '原分析配置无法验证，暂不能继续总结。',
  STOP_CONFIRMATION_REQUIRED: '旧 Worker 尚未确认停止。请按本项目 Stage 2 维护步骤停止并确认 Worker 后，再刷新此页。',
  DELIVERY_NOT_STALE: '任务仍在等待 Worker；可稍后刷新查看。',
  BROKER_CONFIRMATION_UNKNOWN: '队列确认结果不确定，请检查投递状态后再按页面资格恢复。',
  BROKER_PAYLOAD_REJECTED: '队列未接受任务；可使用页面提供的恢复操作。',
  ATTEMPT_NOT_CURRENT: '该次处理已被后续尝试替代，请刷新页面。',
  VIDEO_DELETION_IN_PROGRESS: '视频正在删除，删除期间不可读取媒体或分析结果。',
  ATTEMPT_TEMP_CLEANUP_FAILED: '临时文件清理失败。分析结果状态保持不变，可单独重试清理。',
  ATTEMPT_CLEANUP_NOT_ELIGIBLE: '后端尚未确认可以清理此 attempt。请完成本项目维护步骤并刷新状态。',
  STORAGE_OPERATION_FAILED: '原视频或临时文件清理失败。删除进度已保留，可再次尝试删除。',
  UPLOAD_MEDIA_INVALID: '服务器未能验证视频格式、音轨或时间轴。',
  MEDIA_DURATION_UNSUPPORTED: '视频时长必须大于 0 且不超过 20 分钟。',
  UPLOAD_IO_FAILED: '服务器写入视频失败，请检查本机数据目录后重试。',
};

export function taskStatusLabel(status: string): string {
  return taskLabels[status] ?? '状态未知';
}

export function stageLabel(stage: string): string {
  return stageLabels[stage] ?? '等待后端状态';
}

export function errorMessage(code: string | null): string | null {
  if (!code) return null;
  return errorLabels[code] ?? `后端返回状态：${code}`;
}

export function shouldPollTask(task: AnalysisTask): boolean {
  return task.status === 'queued' || task.status === 'running' || task.summary_status === 'running';
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remaining = whole % 60;
  const clock = `${minutes.toString().padStart(2, '0')}:${remaining.toString().padStart(2, '0')}`;
  return hours > 0 ? `${hours}:${clock}` : clock;
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes;
  let unit = -1;
  do {
    value /= 1024;
    unit += 1;
  } while (value >= 1024 && unit < units.length - 1);
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

export function formatDate(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}
