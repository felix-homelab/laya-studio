# syntax=docker/dockerfile:1
#
# Applies Prisma's migrations and exits.
#
# A separate image because migrations need Node and the application image
# deliberately does not carry a JavaScript runtime. Docker Compose waits for this
# service to complete successfully before starting the app, so the schema is never
# the application's responsibility.

FROM node:22-slim

WORKDIR /app

# Only what `prisma migrate deploy` needs: the CLI, the schema, and the committed
# migrations. `npm ci` uses the lockfile so the Prisma version is pinned.
COPY package.json package-lock.json ./
RUN npm ci --omit=optional

COPY prisma ./prisma

# Fails loudly and non-zero if the migrations cannot be applied, which stops the
# app from starting against an un-migrated database.
CMD ["npx", "prisma", "migrate", "deploy"]
