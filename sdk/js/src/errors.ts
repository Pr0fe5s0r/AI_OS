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

/** Too many requests, or too many at once. Thrown after retries are spent.
 *
 *  Its own type because the caller can do something specific about it:
 *  `retryAfter` is the server's own answer, in seconds, to "when should I come
 *  back?" — already waited through automatically for GETs, so seeing this
 *  means the wait exceeded the client's retry budget rather than that the
 *  limit was momentary.
 *
 *      try {
 *        await docs.answer(question);
 *      } catch (e) {
 *        if (e instanceof RateLimited) scheduleAgainIn(e.retryAfter);
 *      }
 */
export class RateLimited extends MarkvectorError {
  constructor(
    message: string,
    /** Seconds until the caller may try again, as the server reported. */
    readonly retryAfter = 1,
    /** The burst capacity that was exhausted, when the server reported one. */
    readonly limit: number | null = null,
  ) {
    super(message);
  }
}

/** The service could not be reached, or failed after retries. */
export class Unavailable extends MarkvectorError {}

/** `wait: true` gave up before the document became searchable. Its own error
 *  rather than a silent return: ingestion is asynchronous, and code that
 *  assumes otherwise is the most common way to write a flaky test. */
export class IndexingTimeout extends MarkvectorError {}

/** Translate an HTTP response into the right error.
 *
 *  `headers` is optional so existing callers keep working; pass it and a 429
 *  comes back as RateLimited carrying the server's own timing. */
export function errorForStatus(
  status: number,
  detail: string,
  headers?: Headers,
): MarkvectorError {
  if (status === 401 || status === 403) return new AuthError(detail);
  if (status === 404) return new NotFound(detail);
  if (status === 429) {
    const after = Number(headers?.get("retry-after") ?? 1);
    const limit = Number(headers?.get("ratelimit-limit") ?? NaN);
    return new RateLimited(
      detail,
      Number.isFinite(after) && after >= 0 ? after : 1,
      Number.isFinite(limit) ? limit : null,
    );
  }
  if (status >= 400 && status < 500) return new InvalidRequest(detail);
  return new Unavailable(`${status}: ${detail}`);
}
