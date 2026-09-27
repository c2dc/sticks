# Pipeline_UI — Frontend (React + TypeScript + Vite)
#
# Minimal, Docker Desktop friendly image. Application-layer service ONLY:
# it never joins the internal attack networks. It talks to the backend over
# the application-layer bridge network (service name: backend).
#
# Build the SPA, then serve it with `vite preview`. Keeping a single Node image
# avoids pulling an extra web-server image and keeps the setup portable.

FROM node:20-slim

ENV NODE_ENV=production

WORKDIR /app

# Install dependencies first for better layer caching.
COPY package.json package-lock.json ./
RUN npm ci

# Copy the rest of the source and build the production bundle.
COPY . .
RUN npm run build

# vite preview serves the built bundle. --host makes it reachable from outside
# the container; the backend is proxied by the browser via VITE_API_BASE_URL.
EXPOSE 4173

CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
