FROM node:22-alpine

WORKDIR /app

COPY package.json ./
COPY certs ./certs
COPY src ./src

ENV NODE_ENV=production
ENV PORT=8000

EXPOSE 8000

USER node

CMD ["node", "src/server.mjs"]
