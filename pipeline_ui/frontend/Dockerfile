# Pipeline_UI — Frontend (React + TypeScript + Vite)
#
# Minimal, Docker Desktop friendly image. Application-layer service ONLY:
# it never joins the internal attack networks. It talks to the backend over
# the application-layer bridge network (service name: backend).
#
# Multi-stage build:
#   1. `build`   — installs ALL dependencies (including devDependencies) so the
#                  `tsc -b && vite build` step can resolve types and plugins.
#   2. runtime   — installs the dependencies needed to run `vite preview` and
#                  serves the pre-built `dist/` bundle.
#
# NODE_ENV is deliberately NOT set to "production" during `npm ci`, because that
# would make npm skip devDependencies (typescript, vite, @vitejs/plugin-react,
# @types/*), which the build and `vite preview` require.

# ---------------------------------------------------------------------------
# Stage 1 — builder: full toolchain, produces dist/
# ---------------------------------------------------------------------------
FROM node:20-slim AS build

WORKDIR /app

# Install dependencies first for better layer caching. devDependencies are
# included because NODE_ENV is not "production" here.
COPY package.json package-lock.json ./
RUN npm ci

# Copy the rest of the source and build the production bundle (dist/).
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — runtime: lean image that serves the built bundle via vite preview
# ---------------------------------------------------------------------------
FROM node:20-slim

WORKDIR /app

# Install dependencies needed to run `vite preview`. Include dev dependencies
# explicitly so `vite` is available regardless of the NODE_ENV default.
COPY package.json package-lock.json ./
RUN npm ci --include=dev

# Bring in only the built artifacts from the builder stage.
COPY --from=build /app/dist ./dist

# vite preview serves the built bundle. --host makes it reachable from outside
# the container; the backend is proxied by the browser via VITE_API_BASE_URL.
EXPOSE 4173

CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
