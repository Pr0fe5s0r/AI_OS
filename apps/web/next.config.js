/** @type {import('next').NextConfig} */
const API_URL = process.env.API_URL || "http://localhost:8000";

const nextConfig = {
  async rewrites() {
    // The browser only ever calls same-origin /api/*; Next proxies to the API.
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
};

module.exports = nextConfig;
