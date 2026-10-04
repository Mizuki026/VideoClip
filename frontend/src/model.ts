import type { AnalysisTask } from './types';

const taskLabels: Record<string, string> = {
  queued: '等待处理',
  running: '正在处理',
  waiting_model: '等待模型冷却',
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
  waiting_asr: '等待 ASR 冷却',
  waiting_summary: '等待总结模型冷却',
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
  DELIVERY_COORDINATION_ACTIVE: '任务仍在持久化排队，后台会继续协调投递。',
  ANALYSIS_DELIVERY_RETRIES_EXHAUSTED: '分析任务投递补偿次数已耗尽，需要人工处理。',
  ANALYSIS_DELIVERY_DEADLINE_EXPIRED: '分析任务投递等待已超出期限，需要人工处理。',
  ANALYSIS_DELIVERY_BUDGET_UNAVAILABLE: '分析任务投递预算不可用，需要人工检查。',
  ANALYSIS_DELIVERY_BUDGET_EXHAUSTED: '分析任务投递预算已耗尽，需要人工处理。',
  MODEL_WAIT_SINGLE_LIMIT_EXCEEDED: '模型要求等待时间超过单次等待上限，已转人工处理。',
  MODEL_WAIT_TOTAL_LIMIT_EXCEEDED: '模型累计等待时间已达到上限，已转人工处理。',
  SUMMARY_REQUEST_BUDGET_EXHAUSTED: '总结请求预算已耗尽，转写仍保留，不能继续请求总结。',
  AUTOMATIC_RECOVERY_EXHAUSTED: '自动恢复次数已耗尽，需要人工处理。',
  MANUAL_RECOVERY_NOT_RECOMMENDED: '此错误不适合自动重跑，请检查来源、配置或媒体后再处理。',
  LLM_AUTHENTICATION_FAILED: '总结模型鉴权失败。请核对模型账户和密钥配置后再恢复。',
  LLM_MODEL_UNAVAILABLE: '冻结的总结模型当前不可用。请核对模型配置后再恢复。',
  LLM_REQUEST_REJECTED: '总结请求被模型服务拒绝。请检查请求配置后再恢复。',
  MODEL_COOLDOWN_PERSIST_FAILED: '共享冷却状态未能持久化，已阻止自动重试。',
  MODEL_COOLDOWN: '共享模型额度正在冷却。',
  BROKER_CONFIRMATION_UNKNOWN: '队列发送结果暂未确认；后台会按持久化计划复查。',
  BROKER_PAYLOAD_REJECTED: '队列拒绝了发送内容；后台会在独立补偿预算内重试。',
  ATTEMPT_NOT_CURRENT: '该次处理已被后续尝试替代，请刷新页面。',
  VIDEO_DELETION_IN_PROGRESS: '视频正在删除，删除期间不可读取媒体或分析结果。',
  ATTEMPT_TEMP_CLEANUP_FAILED: '临时文件清理失败。分析结果状态保持不变，可单独重试清理。',
  ATTEMPT_CLEANUP_NOT_ELIGIBLE: '后端尚未确认可以清理此 attempt。请完成本项目维护步骤并刷新状态。',
  STORAGE_OPERATION_FAILED: '原视频或临时文件清理失败。删除进度已保留，可再次尝试删除。',
  UPLOAD_MEDIA_INVALID: '服务器未能验证视频格式、音轨或时间轴。',
  MEDIA_DURATION_UNSUPPORTED: '视频时长必须大于 0 且不超过 20 分钟。',
  UPLOAD_IO_FAILED: '服务器写入视频失败，请检查本机数据目录后重试。',
};

const deliveryWaitLabels: Record<string, string> = {
  queued_for_delivery: '已排队，等待首次投递',
  delivery_operation_pending: '投递结果待确认',
  delivery_claim_pending: '消息已发送，等待分析 Worker 领取',
  delivery_retry_wait: '投递异常，等待有界退避后补偿',
  queue_order_wait: '按持久化排队顺序等待前序任务',
  execution_slot_occupied: '分析执行位被其他任务占用',
  stop_confirmation_required: '停止确认阻断，新执行者不会启动',
  delivery_budget_exhausted: '投递补偿预算或期限已耗尽',
  shared_model_cooldown: '同一共享模型额度正在冷却，等待后会重新核对',
  automatic_recovery_pending: '执行失败已确认停止，等待受控恢复协调',
  automatic_recovery_exhausted: '自动恢复次数已耗尽',
  model_wait_manual_required: '模型等待超过限制，需要人工处理',
  summary_request_budget_exhausted: '总结请求预算已耗尽',
  source_configuration_incompatible: '视频来源或冻结配置校验失败',
  transcript_checkpoint_invalid: '历史转写检查点校验失败',
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

export function deliveryWaitLabel(reason: string | null): string | null {
  if (!reason) return null;
  return deliveryWaitLabels[reason] ?? `投递协调状态：${reason}`;
}

export function shouldPollTask(task: AnalysisTask): boolean {
  return task.status === 'queued' || task.status === 'running' || task.status === 'waiting_model' || task.summary_status === 'running';
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
