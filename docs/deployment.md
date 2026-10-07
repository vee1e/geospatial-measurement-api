# Deployment

```
browser ──https://geo.lverma.com──> Vercel (static frontend)
              │  /api/* rewrite
              ▼
        https://geo-api.lverma.com ──> VPS Caddy ──> geoapi container :8000
                                                   (data bind mount /srv/geoapi/data)
```

The frontend calls `/api` on its own origin. Vercel rewrites those requests to the API, so the browser never sees a second origin and CORS is only there for people calling the API directly.

## Live endpoints

| What | URL | Where |
| --- | --- | --- |
| Web interface | https://geo.lverma.com | Vercel, project `geo-measure-web` |
| API | https://geo-api.lverma.com | VPS `lakshit` behind Caddy |
| OpenAPI docs | https://geo-api.lverma.com/docs | served by FastAPI |
| Source | https://github.com/vee1e/geospatial-measurement-api | public repo |

## DNS

Both records live in the `lverma.com` zone, which is served by Vercel DNS. They were created with the Vercel CLI:

```bash
vercel dns add lverma.com geo      CNAME cname.vercel-dns.com   # frontend on Vercel
vercel dns add lverma.com geo-api  A      103.127.146.28        # API on the VPS
```

`vercel dns ls lverma.com` lists the zone. The `A 103.127.146.28` pattern matches the other API subdomains on this domain (`bulk-api`, `krply-api`, `subidx-api`, `workorder-api`).

## Backend on the VPS

The API runs as a container on the same Docker network as the existing `personal-apps` stack, so Caddy reaches it by container name and no host port is opened.

```bash
ssh lakshit
git clone -b main https://github.com/vee1e/geospatial-measurement-api /srv/geoapi
cd /srv/geoapi
./deploy.sh
```

`deploy.sh` pulls, makes `data/` writable by the container's uid (10001), rebuilds, waits for the health check, and curls `/api/health/` through Caddy.

Caddy site block, appended to `/srv/personal-apps/Caddyfile`:

```
geo-api.lverma.com {
    reverse_proxy geoapi:8000
}
```

Reloading Caddy without dropping the other sites:

```bash
docker exec personal-apps-caddy-1 caddy validate --config /etc/caddy/Caddyfile
docker exec personal-apps-caddy-1 caddy reload   --config /etc/caddy/Caddyfile
```

The container joins the network by name in `compose.yaml`:

```yaml
networks:
  personal-apps:
    external: true
    name: personal-apps_default
```

Uploads and the SQLite file live in `/srv/geoapi/data`, bind-mounted to `/data`. That directory is owned by uid 10001, the user the image runs as.

## Frontend on Vercel

```bash
cd frontend
vercel project add geo-measure-web     # once
vercel link --project geo-measure-web  # once per checkout
vercel domains add geo.lverma.com      # once
vercel deploy --prod
```

`vercel.json` carries the rewrite to the API, so no environment variable is needed at build time. For a different API host, set `VITE_API_BASE` with `vercel env add VITE_API_BASE production` and redeploy.

## Verification

```bash
curl -s https://geo.lverma.com/api/health/          # {"status":"ok"}
curl -s https://geo-api.lverma.com/docs             # 200
curl -s -F "file=@sample.kml" https://geo.lverma.com/api/files/
```

Then read `GET /api/files/{id}/measurements/` for the id the upload returns.

On the VPS:

```bash
cd /srv/geoapi && docker compose ps      # healthy
docker logs geoapi --tail 50
docker stats --no-stream geoapi
```

## Rollback

Backend: `git log` on the VPS checkout, `git checkout <sha>`, `./deploy.sh`. Data is in `data/` and is untouched by a rollback.

Frontend: `vercel ls geo-measure-web` lists deployments, `vercel rollback` promotes an earlier one.

## Configuration

Set in `compose.yaml` under `services.geoapi.environment`:

| Variable | Value here | Purpose |
| --- | --- | --- |
| `GEO_CORS_ORIGINS` | `https://geo.lverma.com` plus the preview URL | Origins allowed to call the API directly |
| `GEO_MAX_UPLOAD_BYTES` | `26214400` | 25 MB upload cap |

Everything else uses the defaults listed in the README.
