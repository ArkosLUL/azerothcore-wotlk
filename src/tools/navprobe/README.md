# navprobe

Offline navmesh and height probe. Answers, without a running server, the two questions that make a
computed destination fail silently in game:

- **Is this point on the navmesh?** `MoveTo` returning false on an off-mesh point is invisible at
  the call site.
- **What will happen to its Z?** `UpdateAllowedPositionZ` rewrites Z on the way through, and on a
  map with no vmaps it can rewrite it by hundreds of yards.

It links the same Detour the server links, and re-creates `PathGenerator`'s query sequence against
it, so a path type it reports is the path type the server reports.

## Building and running

The target is generated automatically — `src/tools/CMakeLists.txt` globs subdirectories, so there is
no `CMakeLists.txt` here. It is installed to `env/dist/bin/navprobe` with the other tools.

Under Docker, `docker-compose.override.yml` declares an `ac-navprobe` service that runs the binary
out of the `build` stage with the client-data volume mounted read-only:

```
docker compose --profile tools build ac-navprobe
docker compose --profile tools run --rm ac-navprobe --map 616 coverage
```

Outside Docker, point `--data` at the directory holding `mmaps/`, `maps/` and `vmaps/`.

## Commands

```
navprobe --map ID [--data DIR] <command> [flags]

  coverage                       navmesh params, tile counts, .map and vmap presence
  point X Y Z                    on-mesh test, snapped Z, full height breakdown
  path X1 Y1 Z1 X2 Y2 Z2         PathType mask, poly count, waypoints, length
  ring X Y Z RADIUS HEADINGS     pass/fail table around a centre
```

`--format json` on any of them. Profile flags (`--creature`, `--can-fly`, `--no-swim`, `--falling`,
`--hover`, `--collision`, `--nav`) select which branches of the port apply; the default is a ground
player, which is what `CreateFilter` assumes for anything that is not a `Creature`.

`ring` is the shape that matches how the raid strategies actually compute destinations — a centre, a
radius and N headings — so it is the fastest way to check a formation before shipping it.

## What it mirrors

Ported from `src/server/game/Movement/MovementGenerators/PathGenerator.cpp`:

- the `HaveTile` guard, including the negative-tile workaround, and its short-circuit to
  `BuildShortcut` with `PATHFIND_NORMAL | PATHFIND_NOT_USING_PATH`
- `findNearestPoly` with the `{3, 5, 3}` extents and the `{3, 50, 3}` retry
- the 7 yard far-from-poly threshold and its `FARFROMPOLY_START` / `FARFROMPOLY_END` flags
- `findPath` at `MAX_PATH_LENGTH` 74, `NORMAL` vs `INCOMPLETE` from the last poly
- `findStraightPath` and the full `FindSmoothPath` loop, off-mesh connections included
- the `pointPathLimit` cutoff into `PATHFIND_SHORT`
- `dtQueryFilterExt`, reused rather than re-derived, so the slope cost is the real one

Height follows `Map::GetHeight`, `WorldObject::GetMapHeight` and
`WorldObject::UpdateAllowedPositionZ`, over a `.map` reader that mirrors `GridTerrainData` and the
`VMAP::StaticMapTree` from `src/common`.

## What it does not do

- **Corridor reuse.** A cold query has no prior path, so the subpath and 80%-prefix branches cannot
  apply.
- **Raycast mode.** `_useRaycast` needs a caller that opted in.
- **The slope gate.** `DT_SLOPE_TOO_STEEP` needs liquid data. `--slope-check` is accepted and
  reports that it was ignored.
- **Liquid, anywhere.** So `IsWaterPath` is always false, `UpdateFilter` never widens the include
  flags, and the swim branch of `UpdateAllowedPositionZ` collapses into the ground branch.
- **GameObject collision.** The dynamic tree is live server state.
- **The Blade's Edge Arena rope snap** in `NormalizePath`.

**It preloads every tile on disk.** The server only holds tiles for grids it has loaded, so navprobe
answers "reachable in principle" while an in-game `.mmap` query answers "reachable right now". When
cross-checking the two, stand where you are querying.

**This is a hand-maintained port.** Detour is shared, the logic is copied. If `PathGenerator.cpp`
changes, this does not, and the two will disagree without saying so.

## Verifying it

`.mmap loc` and `.mmap path` (`src/server/scripts/Commands/cs_mmaps.cpp`) are the ground truth. Run
both against the same coordinates and compare tile `[x, y]`, poly ref, path Type and Length.

Two map-level facts, verified against the shipped client data, are useful as fixtures:

- **Map 616 (Eye of Eternity) has no navmesh tiles at all** — `616.mmap` exists with
  `maxTiles = 25`, and there is not one `.mmtile`. It is the only such map of 98.
- **It has no vmaps either**, and all 25 of its `.map` tiles are flat with `gridHeight = 0.0`, while
  the platform sits near Z 266. `navprobe --map 616 point <x> <y> 266` should report terrain 0.0, no
  vmap, and `UpdateAllowedPositionZ` settling at 0.0.
