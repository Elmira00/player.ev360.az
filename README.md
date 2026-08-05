# player.ev360.az

Django project that downloads Matterport tours (via the existing `matterport-dl.py`
script) and, eventually, serves them locally so `ev360.az` never touches Matterport's
CDN or branding directly.

## Status of this skeleton
- ✅ Submit page → creates a `MatterportTour` row, kicks off a Celery task
- ✅ Celery task → runs `matterport-dl.py <url>` as a subprocess, tracks status
- ✅ Dashboard page → lists all tours + status + a **placeholder** local link
  (`http://player.ev360.az/tour/<id>/`) — this link does not resolve to anything yet.
- ❌ Not built yet: auth/login, actually *serving* downloaded tours, the
  ev360.az ↔ player.ev360.az HTTP client integration.

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Point this at wherever your working matterport-dl.py checkout lives:
export MATTERPORT_DL_DIR=/path/to/matterport-dl

python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/

# terminal 1
redis-server

# terminal 2
celery -A player_ev360 worker --loglevel=info

# terminal 3
python manage.py runserver
```

Visit `http://127.0.0.1:8000/` to submit a link, `http://127.0.0.1:8000/dashboard/`
to watch status.

## Serving — open decision for next session

`matterport-dl.py` ships its own `OurSimpleHTTPRequestHandler` HTTP server that does
more than serve static files: it replays graph API POST requests as JSON, redirects
cropped textures, and swaps in `.modified.` file versions. That logic is specific
and non-trivial to reimplement in Django.

Recommended approach: once a tour is `ready`, spawn
`python matterport-dl.py <id> 127.0.0.1 <free_port>` as a long-running subprocess
(one per tour, or an LRU pool), and have Django/nginx reverse-proxy
`player.ev360.az/tour/<id>/` → `127.0.0.1:<port>/`. This reuses the already-working
serving code instead of duplicating it.
