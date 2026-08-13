/** The one place the SDK's version is written.
 *
 *  It used to live in three: `package.json`, the `VERSION` export, and the
 *  `X-Markvector-Client` header in `client.ts`. They drifted — 0.2.4 shipped
 *  to npm while identifying itself to the server as 0.2.0 for four releases,
 *  so server-side client telemetry was wrong for every one of them. Keep this
 *  as the single source and let the other two derive from it; `npm test`
 *  asserts the `package.json` field still agrees.
 */
export const VERSION = "0.3.0";

/** What the SDK calls itself to the API, in `X-Markvector-Client`. */
export const SDK_CLIENT = `javascript/${VERSION}`;
