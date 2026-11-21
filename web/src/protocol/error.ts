export class ProtocolApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly status: number;

  constructor(
    status: number,
    code: string,
    message: string,
    retryable = false
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}
