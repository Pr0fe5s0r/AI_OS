/** Base for everything this client throws. Catch this to catch them all. */
export class MarkvectorError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = new.target.name;
  }
}

/** The key is missing, wrong, revoked, or lacks the scope for this call. */
export class AuthError extends MarkvectorError {}

/** No such collection, document or trace in this workspace. */
export class NotFound extends MarkvectorError {}

/** The server rejected the request. The message is the server's own. */
export class InvalidRequest extends MarkvectorError {}

/** The service could not be reached, or failed after retries. */
export class Unavailable extends MarkvectorError {}

/** `wait: true` gave up before the document became searchable. Its own error
 *  rather than a silent return: ingestion is asynchronous, and code that
 *  assumes otherwise is the most common way to write a flaky test. */
export class IndexingTimeout extends MarkvectorError {}

/** Translate an HTTP response into the right error. */
export function errorForStatus(status: number, detail: string): MarkvectorError {
  if (status === 401 || status === 403) return new AuthError(detail);
  if (status === 404) return new NotFound(detail);
  if (status >= 400 && status < 500) return new InvalidRequest(detail);
  return new Unavailable(`${status}: ${detail}`);
}
