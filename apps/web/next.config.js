/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me";

const nextConfig = {
  /* Next 16 builds with Turbopack, which infers the project root by walking up
     for a lockfile. There is a stray bun.lock in the user's home directory, so
     it walked clear out of the repository and warned about it. Pinned here:
     the root is this app, never whatever happens to sit above the checkout. */
  turbopack: { root: __dirname },
  /* Next gzips everything it proxies, INCLUDING text/event-stream — and gzip
     buffers the whole body, so an SSE client receives nothing until the
     response ends. That silently defeats every stream we have (agent chat,
     workflow runs, the feed nudge): curl looked fine because curl doesn't ask
     for gzip; the browser always does. Setting a Content-Encoding upstream
     doesn't help, Next re-compresses anyway. Compression belongs at the edge
     (a CDN or reverse proxy), not in this dev/app proxy. */
  compress: false,
  /* How large a request body the proxy will carry. Next's default is 10MB, and
     it does not reject a larger one — it forwards the FIRST 10MB, the upstream
     sees a truncated multipart body and resets the connection, and the browser
     is handed a bare 500. Measured on a 17MB PDF: 30.5 seconds, then
     "Internal Server Error", with nothing whatsoever in the API log because the
     request never arrived intact.

       Request body exceeded 10MB for /api/items/file
       Failed to proxy http://api:8000/api/items/file Error: socket hang up

     Matched to the API's own MAX_UPLOAD_MB (128MB) so the two agree. If the
     proxy limit is the smaller of the pair, the API's polite "file too large"
     refusal can never be reached and every oversized upload dies as a 500
     instead. */
  experimental: { proxyClientMaxBodySize: "128mb" },
  async rewrites() {
    // The browser only ever calls same-origin /api/*; Next proxies to the API.
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

module.exports = nextConfig;
