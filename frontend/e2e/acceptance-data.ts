export function shouldCleanupAcceptanceData(status: string | undefined): boolean {
  return status === 'passed';
}
