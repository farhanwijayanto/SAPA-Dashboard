FROM node:20-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

# Build-time env untuk Vite. Diisi lewat:
#   docker build --build-arg VITE_EDGE_INGEST_KEY=xxx ...
# VITE_EDGE_INGEST_KEY (opsional) -> supaya halaman /edge bisa kirim frame
#   tanpa login (mode kiosk layar gate). Kalau kosong, halaman /edge tetap
#   jalan tapi WAJIB login dulu (JWT dipakai sebagai gantinya).
ARG VITE_EDGE_INGEST_KEY=""
ARG VITE_API_URL=""
ENV VITE_EDGE_INGEST_KEY=$VITE_EDGE_INGEST_KEY
ENV VITE_API_URL=$VITE_API_URL

RUN chmod -R +x node_modules/.bin && npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
