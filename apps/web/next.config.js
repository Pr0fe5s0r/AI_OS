/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me";

const nextConfig = {
  /* Next gzips everything it proxies, INCLUDING text/event-stream — and gzip
     buffers the whole body, so an SSE client receives nothing until the
     response ends. That silently defeats every stream we have (agent chat,
     workflow runs, the feed nudge): curl looked fine because curl doesn't ask
     for gzip; the browser always does. Setting a Content-Encoding upstream
     doesn't help, Next re-compresses anyway. Compression belongs at the edge
     (a CDN or reverse proxy), not in this dev/app proxy. */
  compress: false,
  async rewrites() {
    // The browser only ever calls same-origin /api/*; Next proxies to the API.
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

module.exports = nextConfig;
