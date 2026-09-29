# syntax=docker/dockerfile:1
#
# Applies Prisma's migrations and exits.
#
# A separate image because migrations need Node and the application image
# deliberately does not carry a JavaScript runtime. Docker Compose waits for this
# service to complete successfully before starting the app, so the schema is never
# the application's responsibility.

FROM node:22-slim

# Prisma picks its schema-engine build from the OpenSSL version it finds, and the
# slim base does not ship one. Installed before `npm ci`, because the engine is
# chosen and downloaded at install time.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Only what `prisma migrate deploy` needs: the CLI, the schema, and the committed
# migrations. `npm ci` uses the lockfile so the Prisma version is pinned; the CLI
# is a devDependency, so dev dependencies are included explicitly.
COPY package.json package-lock.json ./
RUN npm ci --include=dev

COPY prisma ./prisma

# The job only reads the copied files and opens a database connection.
USER node

# Fails loudly and non-zero if the migrations cannot be applied, which stops the
# app from starting against an un-migrated database. The local binary rather than
# `npx`, so a missing CLI fails here instead of fetching an unpinned one.
CMD ["/app/node_modules/.bin/prisma", "migrate", "deploy"]
