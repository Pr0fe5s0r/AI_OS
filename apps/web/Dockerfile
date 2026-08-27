FROM node:20-alpine

WORKDIR /app
# The lockfile is copied too, and `npm ci` installs exactly what it pins. With
# only package.json, `npm install` re-resolves inside the image and the
# container can end up on different versions than the host it was tested on —
# which is how a dependency added on the host (d3) went missing in here while
# package.json swore it was there.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# `next.config.js` resolves rewrites() during the BUILD and freezes the result
# into routes-manifest.json — the /api/* proxy destination is not re-read at
# startup. So API_URL has to be present here, not only in the runtime env, or
# the deployed app proxies to the stale default baked into the config.
ARG API_URL
ENV API_URL=$API_URL
RUN npm run build

EXPOSE 3000
# Production server, NOT `next dev`. Dev mode behind a public origin fails two
# ways that look like infrastructure problems but are not: Next 15+ answers 403
# to /_next/* requests from an origin it hasn't been told to trust (the chunks
# and the woff2 that would not load), and the client keeps dialling a hot-reload
# websocket at wss://<host>/_next/hmr that no deployment should be serving.
# `next start` has no HMR and serves hashed static assets outright.
CMD ["npm", "start"]
