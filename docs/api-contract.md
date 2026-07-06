# TimberBridge HTTP/JSON contract

The seam between the mod (inside the game) and the player agent. Localhost HTTP on the Windows box, reached from the Mac over the SSH tunnel. Small, validated, and **digested** — the bridge does the math in C# so the model reads dashboards, not raw dumps.

Scope note: `kb_lookup`, `playbook_read/append`, and design retrieval are **agent-local** (files + embeddings on the agent host), NOT bridge endpoints. The bridge only exposes the live game.

## Endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | `/ping` | liveness + versions (survives reconnect after load) |
| GET | `/state` | digested colony snapshot (the workhorse) |
| GET | `/map?x&z&w&h&layers` | terrain/water/moisture grid for a bbox (construction only) |
| GET | `/events?since=<cursor>` | what changed since a cursor |
| POST | `/act` | one validated command (see enum) |
| GET | `/blueprints` | one-shot spec dump (costs/sizes/outputs) — KB reconciliation |

### GET /ping
```json
{ "ok": true, "game_version": "1.0.13.1", "bridge_version": "0.1.0", "in_game": true, "paused": true, "scene": "Game" }
```
`in_game:false` when at the main menu or mid-load — the agent polls `/ping` to reconnect after a `load` tears down the game scene.

### GET /state  (the contract's center of gravity)
```json
{
  "time":   { "cycle": 2, "day": 6, "time_of_day": "Daytime", "hour": 9.4 },
  "weather":{ "current": "temperate", "current_ends_in_days": 4.2,
              "next": { "type": "drought", "in_days": 4.2, "duration_days": 5 } },
  "population": { "total": 14, "adults": 12, "kits": 2,
                 "distress": { "thirst": {"warning":1,"critical":0},
                               "hunger": {"warning":2,"critical":0},
                               "sleep":  {"warning":0,"critical":0} } },
  "resources": [
    { "good":"Water","stored":62,"capacity":300,"net_per_day":-4.2,"days_remaining":4.1 },
    { "good":"Food","stored":210,"capacity":400,"net_per_day":3.0,"days_remaining":11.0 },
    { "good":"Log","stored":38,"net_per_day":2.0 },
    { "good":"Plank","stored":9,"net_per_day":0.5 } ],
  "water_sites": [ { "id":"pump_1","depth":1.8,"contamination":0.0,"drinkable":true } ],
  "buildings": { "counts": {"WaterPump":1,"LargeWaterTank":1,"Lodge":2,"Farmhouse":1},
                 "unstaffed": ["Farmhouse#3"], "paused": [], "under_construction": 1 },
  "alerts": [
    { "id":"water_understocked_for_forecast", "severity":"high",
      "message":"stored water 4.1d < needed (5+2)*2.13*14 = 209",
      "suggestion":"add Large Tanks / raise pump priority" } ],
  "event_cursor": 812
}
```
Field → source (from `docs/reference/confirmed-api.md`): `weather.next` ← `DroughtWeather/BadtideWeather.GetDurationAtCycle(cycle+1)` + `WeatherService`; `population.distress` ← aggregate `NeedManager.NeedIsBelowWarningThreshold/InCriticalState` over beaver entities; `resources` ← inventory aggregation (GAP: inventory service); `water_sites` ← `IThreadSafeWaterMap.WaterDepth/ColumnContamination`; `buildings` ← `EntityComponentRegistry.GetEnabled()` filtered by type.

`days_remaining` and `net_per_day` are **computed in the bridge** — the single most important design choice. Anything the bridge can't compute pushes reasoning back onto the cheap model.

### GET /map
Compact typed grids for a bounding box; layers ∈ `terrain_height`, `water_depth`, `contamination`, `moisture`, `occupied`. Returned as row-major arrays, not per-tile objects. Requested only when a design needs placement planning.

### GET /events?since=cursor
```json
{ "cursor": 861, "events": [
  { "seq":841,"t":"drought_started" },
  { "seq":855,"t":"beaver_died","cause":"thirst" },
  { "seq":860,"t":"construction_finished","spec":"LargeWaterTank","id":"tank_2" } ] }
```
Sourced from game events (`CycleEndedEvent`, `HazardousWeatherStartedEvent`, `EntityDeletedEvent`, block finished-state events).

### POST /act
Request: `{ "command": <enum>, "args": { ... } }`. One command per call.

| command | args | notes |
|---|---|---|
| `place_building` | `spec, x, y, z, orientation` | validated via `BlockValidator.BlocksValid` before commit |
| `demolish` | `entity_id` \| `x,y,z` | |
| `designate_area` | `tool: cut_trees\|plant\|clear\|dig, spec?, x,z,w,h` | uses `AreaIterator` |
| `set_priority` | `entity_id, level` | worker/builder priority |
| `pause_building` | `entity_id, paused: bool` | |
| `set_speed` | `speed: 0..10` | `0` = pause (`SpeedManager.ChangeSpeed`) |
| `advance` | `hours: float` | `IDayNightCycle.JumpTimeInHours`, then auto-pause |
| `save` | `slot` | rollback checkpoint |
| `load` | `slot` | tears down scene → agent reconnects via `/ping` |

Response (success): `{ "ok": true, "applied": {"entity_id":"tank_2"} }`

Response (**teaching error** — the key to cheap-model reliability):
```json
{ "ok": false,
  "error": "invalid_placement",
  "reason": "overlaps Lodge#2 at (12,0,34)",
  "suggestion": { "nearest_valid": {"x":14,"y":0,"z":34,"orientation":"North"} } }
```
On placement failure the bridge scans nearby `Placement`s with `BlocksValid` and returns the nearest valid tile so the agent retries a fix instead of reasoning geometry from scratch.

### GET /blueprints
One-shot enumeration of `BlockObjectSpec`/`ComponentSpec` (id, cost, footprint, workers, power, recipe I/O, science cost). Not used in the play loop — it reconciles the KB's `(v?)` numbers against installed ground truth (Phase 1).

## Cross-cutting rules
- **Time model:** the agent keeps the game paused; state is read at a stable tick. `advance` moves a bounded amount then re-pauses, so every decision sees a consistent world.
- **Threading:** GETs that only touch `IThreadSafeWaterMap` may serve off-thread; everything else is queued to the Unity main thread via `IUpdatableSingleton.UpdateSingleton()` (see `docs/reference/modding-api.md`).
- **Versioning:** `/ping.bridge_version`; breaking `/state` changes bump a `schema` field so the agent fails loudly, not silently.
- **Auth:** localhost-only bind; the SSH tunnel is the trust boundary. No auth in v1.
