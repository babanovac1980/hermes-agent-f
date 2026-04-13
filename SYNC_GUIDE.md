# Hermes Fork Sync Guide

## Remote Setup

```
NousResearch/hermes-agent   ← upstream (never push here — read only)
        ↓ fork
babanovac1980/hermes-agent-f ← origin (your GitHub)
        ↓ clone
/home/bogdan/AI_Projects/hermes-agent-f  ← local dev machine
        ↓ deploy
hermes-agent (Unraid docker container, source at /opt/hermes)
```

Verify remotes are configured correctly:
```bash
git remote -v
# Should show:
# origin    https://github.com/babanovac1980/hermes-agent-f.git (fetch)
# origin    https://github.com/babanovac1980/hermes-agent-f.git (push)
# upstream  https://github.com/NousResearch/hermes-agent.git (fetch)
# upstream  https://github.com/NousResearch/hermes-agent.git (push)
```

If upstream is missing:
```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
```

---

## Sync Workflow (repeat whenever upstream has updates)

Run this on your **local dev machine** (`/home/bogdan/AI_Projects/hermes-agent-f`):

```bash
# 1. Fetch latest from upstream
git fetch upstream --tags

# 2. Make sure you're on your working branch
git checkout chore/sync-v0.8.0

# 3. Merge upstream into your branch (preserves your commits on top)
git merge upstream/main --no-edit

# 4. Fix conflicts if any appear
#    Your Jellyfin plugin lives in plugins/jellyfin/ which upstream
#    never touches, so it will always be conflict-free.
#    Common conflict spots: cli.py, hermes_cli/config.py, toolsets.py

# 5. Push updated branch to your GitHub fork
git push origin chore/sync-v0.8.0
```

---

## Updating the Unraid Container

### Option A — Fast update (Python-only changes, no new packages)

Use this for day-to-day plugin/skill/tool tweaks. No rebuild needed.

```bash
ssh root@192.168.1.100

docker exec -u root hermes-agent bash -c \
  "cd /opt/hermes && git fetch origin chore/sync-v0.8.0 && git reset --hard origin/chore/sync-v0.8.0"

docker restart hermes-agent
```

### Option B — Full rebuild (new pip packages, Dockerfile changes, major upstream update)

Use this after a large upstream sync that adds new dependencies or
changes the Dockerfile.

> **Important:** The GitHub Actions workflow in this repo only builds
> images for `NousResearch/hermes-agent`, **not** for your fork.
> You need to build the image manually on Unraid or locally.

#### Build on Unraid directly (recommended — no local Docker needed):

```bash
ssh root@192.168.1.100

# Pull the latest source into the container first
docker exec -u root hermes-agent bash -c \
  "cd /opt/hermes && git fetch origin chore/sync-v0.8.0 && git reset --hard origin/chore/sync-v0.8.0"

# Build a new image from the updated source
docker build -t hermes-agent-f:latest /opt/hermes

# Stop and remove the old container
docker stop hermes-agent
docker rm hermes-agent

# Re-create with same volumes/settings (adjust to match your Unraid template)
docker run -d \
  --name hermes-agent \
  --restart unless-stopped \
  -v /mnt/user/appdata/hermes:/opt/data \
  -e HERMES_HOME=/opt/data \
  hermes-agent-f:latest
```

#### Alternative: build locally and push to a private registry
If you have a private Docker registry or use Unraid's local registry:
```bash
# On local dev machine
docker build -t hermes-agent-f:latest .
docker tag hermes-agent-f:latest your-registry/hermes-agent-f:latest
docker push your-registry/hermes-agent-f:latest

# On Unraid
docker pull your-registry/hermes-agent-f:latest
# then recreate the container as above
```

---

## When to Use Each Option

| Situation | Option |
|-----------|--------|
| Tweaked Jellyfin plugin, added a skill | A (fast update) |
| Pulled upstream fixes, no new packages | A (fast update) |
| Upstream added new pip dependencies | B (rebuild) |
| Dockerfile changed | B (rebuild) |
| Major version sync (100+ commits) | B (rebuild) |

---

## Verify After Update

After either option, confirm everything loaded correctly:

```bash
docker exec -it hermes-agent hermes doctor
```

Or inside an interactive session:
```
/plugins       → should list: jellyfin v1.0.0, model_switcher v1.0.0
/tools         → should include 6 jellyfin_* tools
```

Functional check:
```
get my jellyfin library stats
```

---

## Why Jellyfin Survives Syncs Without Conflicts

The Jellyfin integration lives entirely in `plugins/jellyfin/`:

```
plugins/jellyfin/
├── plugin.yaml        # manifest: name, version, requires_env, provides_tools
├── __init__.py        # register(ctx) — wires tools via PluginContext
├── jellyfin_client.py # all handlers, schemas, auth helpers
└── SKILL.md           # companion skill for IMDB cross-referencing etc.
```

Upstream **never touches** `plugins/jellyfin/`, so merges are always clean.
No core files (`toolsets.py`, `model_tools.py`, `hermes_cli/config.py`) need
to be modified when you update Jellyfin — changes stay inside the plugin dir.
