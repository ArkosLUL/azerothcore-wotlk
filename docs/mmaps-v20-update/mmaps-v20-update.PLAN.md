# Update mmaps to v20 (Blade's Edge Arena pathfinding fix)

## Context

Upstream bumped `MMAP_VERSION` from 19 to 20 in commit `f839009e9` — *"fix(Core/MMaps): fix Blade's Edge
Arena falling/edge pathing (bump mmap version to 20) (#25720)"*. This is a **breaking data change**: the
worldserver refuses any `.mmtile` whose header version doesn't match the compiled `MMAP_VERSION`, so the
binary and the extracted mmaps must be swapped together. Running one without the other kills pathfinding
across the whole server, not just in Blade's Edge Arena.

The fix itself has two halves:

1. **Generator side** — off-mesh connections moved out of the removed `--offMeshInput` CLI flag into
   `mmaps-config.yaml` (`offmeshConnections:` list), and two hand-authored connections were added for the
   Blade's Edge Arena ropes. The map-562 `walkableRadius: 0` override was dropped in favour of these.
2. **Runtime side** — `PathGenerator::NormalizePath()` now snaps path points onto an analytic model of the
   two arena ropes (catenary sag included) on map 562 instead of calling `UpdateAllowedPositionZ`, which is
   what dropped players and pets through the world near the rope edges.

### Current state (verified 2026-07-26)

| Thing | State |
| --- | --- |
| Branch `Custom` (HEAD `a076e4b16`) | `MMAP_VERSION 19`, working tree clean |
| Commit `f839009e9` | present in local/origin/upstream `test-staging` only — **not** in `Custom`, **not** in `upstream/Playerbot` |
| Commit `df54196fe` | follow-up "fix(CI): fix data version v20 to v20.0 (#26697)" — also `test-staging` only |
| `Custom` vs `test-staging` | 135 commits behind (incl. 878 SQL files), 14 commits ahead |
| Local commits vs the 13 files the fix touches | no overlap — no conflict expected |
| Runtime | Docker Compose. `ac-worldserver`, `ac-authserver`, `ac-database` running; `ac-client-data-init` exited 0 |
| Client data | volume `azerothcore-wotlk-pb_ac-client-data`, `INSTALLED_VERSION=v19`, 3780 mmaps files, 3.1 GB |
| Free space | 914 GB on the Docker disk — not a constraint |
| `var/client` | **absent** — no WoW client available for local re-extraction |
| Pre-extracted v20 | `wowgaming/client-data` tag **`v20.0`** (not `v20`), asset `Data.zip`, 1.14 GB, published 2026-07-19 |

### Key file references

- [MapDefines.h:29](../../src/common/Collision/Maps/MapDefines.h#L29) — `#define MMAP_VERSION 19` → `20`
- [MMapMgr.cpp:88-94](../../src/common/Collision/Management/MMapMgr.cpp#L88-L94) — the rejection point; error
  text `MMAP:loadMap: {:03}{:02}{:02}.mmtile was built with generator v{}, expected v{}`
- [functions.sh:156-185](../../apps/installer/includes/functions.sh#L156-L185) — `inst_download_client_data`,
  `local VERSION=v20.0`; skips the download when `data-version` already matches
- [docker-compose.yml:87](../../docker-compose.yml#L87) — data volume mounted `:ro` into `ac-worldserver`
- [mmaps-config.yaml](../../src/tools/mmaps_generator/mmaps-config.yaml) — new `offmeshConnections:` block

## Step 1 — get the v20 code onto `Custom`

> **Requires an explicit go-ahead.** Per the read-only-git rule, an agent executing this plan must not run
> `merge`, `cherry-pick`, or anything else that moves refs without the user naming the command.

**Recommended: full merge**, matching the PR #1–#6 workflow already in the branch history.

```bash
git merge test-staging      # or: open a PR from test-staging into Custom, as before
```

Brings 135 commits: the two mmap commits plus 878 SQL files and unrelated core fixes. The DB auto-updater
applies the SQL on the next worldserver start.

*Alternative if the mmap change should stay isolated:* `git cherry-pick f839009e9 df54196fe`. The 13 touched
files have no local modifications, so it should apply clean; it leaves `Custom` 135 commits behind for a
later merge.

Confirm afterwards:

```bash
grep -n "MMAP_VERSION" src/common/Collision/Maps/MapDefines.h     # expect 20
grep -n "local VERSION=" apps/installer/includes/functions.sh     # expect v20.0
```

The `v20.0` check matters: the original commit wrote `v20`, and that tag **does not exist** on GitHub
(`/releases/tags/v20` → 404). Without `df54196fe` the downloader 404s.

## Step 2 — stop the stack

The data volume is mounted read-only into a *running* worldserver. Replacing mmaps underneath a live server
is not safe.

```powershell
docker compose down
```

## Step 3 — wipe the client-data volume

`unzip -o` overwrites but never deletes. Any v19 `.mmtile` that v20 no longer produces would survive and be
rejected at load. The download is the same 1.14 GB either way, so wiping costs nothing.

```powershell
docker volume rm azerothcore-wotlk-pb_ac-client-data
```

*Lighter alternative:* leave the volume and only remove `/azerothcore/env/dist/data/data-version` so the
downloader re-runs — accepts the stale-file risk above.

## Step 4 — rebuild the images from the new code

Both the worldserver **and** the client-data image must be rebuilt: the latter bakes `apps/` (and therefore
the `VERSION=v20.0` string) into the image.

```powershell
docker compose build ac-client-data-init ac-worldserver ac-authserver
```

## Step 5 — install v20 data

```powershell
docker compose up ac-client-data-init
```

Downloads `https://github.com/wowgaming/client-data/releases/download/v20.0/data.zip` (1.14 GB; GitHub asset
URLs are case-insensitive, so lowercase `data.zip` resolves to `Data.zip`), unzips into the volume, and
writes `INSTALLED_VERSION=v20.0`.

*Alternative — re-extract locally.* Only if the generator should be run in-house: place a WoW 3.3.5a client
at `./var/client`, then `docker compose --profile tools run --rm ac-tools`, running `map_extractor`,
`vmap4_extractor && vmap4_assembler Buildings vmaps`, `mmaps_generator`. Native build equivalent is
`-DTOOLS_BUILD=maps-only` (the announcement's `-DTOOLS=1` is TrinityCore syntax; AzerothCore uses
`TOOLS_BUILD` with values `none|all|db-only|maps-only`). Costs many hours of CPU for an identical result.

## Step 6 — bring the stack back up

```powershell
docker compose up -d
```

`ac-db-import` runs first and applies the pending SQL from the merge.

## Verification

1. **Data version**
   ```powershell
   docker exec ac-worldserver cat /azerothcore/env/dist/data/data-version   # INSTALLED_VERSION=v20.0
   docker exec ac-worldserver sh -c "ls /azerothcore/env/dist/data/mmaps | wc -l"
   ```

2. **No rejected tiles** — the version mismatch is logged loudly by `MMapMgr`. After the server finishes
   loading, this must return nothing:
   ```powershell
   Select-String -Path env\dist\logs\Server.log -Pattern "was built with generator|Bad header in mmap"
   ```
   A hit means the binary and data are out of step — recheck steps 1 and 5.

3. **Binary carries v20** — confirm the running worldserver was actually rebuilt:
   ```powershell
   docker compose images ac-worldserver     # image ID should differ from the pre-rebuild one
   ```

4. **In-game, Blade's Edge Arena (map 562)** — GM commands from
   [cs_mmaps.cpp:47-54](../../src/server/scripts/Commands/cs_mmaps.cpp#L47-L54):
   - `.mmap loadedtiles` — tiles load without error
   - stand on a rope, `.mmap loc` — reports a valid poly, prints the recast config from the tile header
   - `.mmap path` while pathing across a rope — no fall-through, path points follow the rope's sag
   - walk a pet/bot across both ropes; ropes are tile `31,20` on map 562

5. **Regression sweep** — pathfinding is global, so spot-check a couple of unrelated zones (a city, an
   instance) for normal NPC movement before declaring done.

## Rollback

If v20 misbehaves: `docker compose down`, revert the merge/cherry-pick on `Custom`, rebuild, then
`docker volume rm azerothcore-wotlk-pb_ac-client-data` and re-run `ac-client-data-init` — with
`VERSION=v19` restored by the revert it re-downloads the v19 dataset. Nothing in the character or world DB
depends on the mmap version, so no DB rollback is involved.

## Notes

- Only `.mmtile` files carry a version header. The `.mmap` files (navmesh params) do not and are never
  invalidated — but the full `Data.zip` replaces everything regardless.
- `mmaps_generator`'s `shouldSkipTile` compares `mmapVersion` against `MMAP_VERSION`, so a version bump
  forces a full regeneration; there is no partial-regeneration shortcut. Regenerating map 562 alone would
  leave every other map at v19 and rejected.
- Unmerged upstream branch `fix/mmaps-config-overrides-and-aliases` (`65018acd7`, "mmaps config overrides +
  bot steep-slope-aware nav filter") touches the same area. Out of scope here; worth watching for a future
  bump.
