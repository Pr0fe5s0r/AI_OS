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

EXPOSE 3000
CMD ["npm", "run", "dev"]
