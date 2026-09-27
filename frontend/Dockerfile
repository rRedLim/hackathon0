# ---------- сборка ----------
FROM node:24-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# ---------- раздача ----------
FROM nginx:1.27-alpine
# адрес backend можно переопределить: docker run -e BACKEND_URL=http://host:8080 ...
ENV BACKEND_URL=http://backend:8080 \
    NGINX_RESOLVER=127.0.0.11
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
