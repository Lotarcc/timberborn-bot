using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Timberborn.BlockSystem;
using Timberborn.BuilderPrioritySystem;
using Timberborn.Buildings;
using Timberborn.CameraSystem;        // CameraService                (CONFIRMED via decompile: injectable singleton)
using Timberborn.ConstructionSites;
using Timberborn.Coordinates;
using Timberborn.EntitySystem;
using Timberborn.Forestry;
using Timberborn.GameDistricts;
using Timberborn.GameSaveRepositorySystem;
using Timberborn.GameSaveRuntimeSystem;
using Timberborn.Planting;
using Timberborn.PrioritySystem;
using Timberborn.TemplateSystem;
using Timberborn.TerrainSystem;       // ITerrainService              (CONFIRMED, MapReader uses it)
using Timberborn.TimeSystem;
using Timberborn.WaterBuildings;      // WaterInputSpec               (CONFIRMED via decompile)
using Timberborn.WaterSystem;         // IThreadSafeWaterMap          (CONFIRMED, MapReader uses it)
using UnityEngine;

namespace TimberBridge {

  // Executes /act commands. Every call runs on the Unity main thread (via
  // MainThreadDispatcher). Returns a JSON string. Placement mirrors the game's
  // own BuildingPlacer: validate -> ConstructionFactory (buildings) or
  // BlockObjectFactory (paths/etc). Invalid placement returns a teaching error
  // with the nearest valid tile so the agent can retry.
  public class Actuator {

    private static readonly Vector3Int[] SearchOffsets = BuildSearchOffsets(8);

    private readonly SpeedManager _speed;
    private readonly TemplateService _templates;
    private readonly BlockObjectFactory _blockFactory;
    private readonly BlockValidator _validator;
    private readonly IBlockService _blocks;
    private readonly ConstructionFactory _construction;
    private readonly EntityService _entities;
    private readonly GameSaver _saver;
    private readonly GameLoader _loader;
    private readonly DistrictCenterRegistry _districts;
    private readonly ReachabilityReader _reachability;
    private readonly TreeCuttingArea _cuttingArea;
    private readonly ResourcesReader _resources;
    private readonly IThreadSafeWaterMap _waterMap;
    private readonly ITerrainService _terrain;
    private readonly PlantingService _planting;
    private readonly CameraService _camera;

    public Actuator(SpeedManager speed,
                    TemplateService templates,
                    BlockObjectFactory blockFactory,
                    BlockValidator validator,
                    IBlockService blocks,
                    ConstructionFactory construction,
                    EntityService entities,
                    GameSaver saver,
                    GameLoader loader,
                    DistrictCenterRegistry districts,
                    ReachabilityReader reachability,
                    TreeCuttingArea cuttingArea,
                    ResourcesReader resources,
                    IThreadSafeWaterMap waterMap,
                    ITerrainService terrain,
                    PlantingService planting,
                    CameraService camera) {
      _speed = speed;
      _templates = templates;
      _blockFactory = blockFactory;
      _validator = validator;
      _blocks = blocks;
      _construction = construction;
      _entities = entities;
      _saver = saver;
      _loader = loader;
      _districts = districts;
      _reachability = reachability;
      _cuttingArea = cuttingArea;
      _resources = resources;
      _waterMap = waterMap;
      _terrain = terrain;
      _planting = planting;
      _camera = camera;
    }

    public string Act(string command, JObject args) {
      try {
        switch (command) {
          case "set_speed": return SetSpeed(GetFloat(args, "speed", 1f));
          case "pause": return SetSpeed(0f);
          case "place_building":
            // Default to a real construction site: beavers haul materials and build
            // it over time, consuming Logs/goods — the actual game loop. Pass
            // instant=true only for debug/god-mode instant finish. Accept the spec
            // under any common key models emit (spec/spec_id/building/name).
            return PlaceBuilding(GetStr(args, "spec") ?? GetStr(args, "spec_id")
                                   ?? GetStr(args, "building") ?? GetStr(args, "name"),
                                 GetCoord(args, "x"), GetCoord(args, "y"), GetCoord(args, "z"),
                                 GetStr(args, "orientation"), GetBool(args, "instant", false),
                                 GetBool(args, "auto_connect", true));
          case "demolish": return Demolish(GetInt(args, "x"), GetInt(args, "y"), GetInt(args, "z"));
          case "set_priority":
            return SetPriority(GetCoord(args, "x"), GetCoord(args, "y"), GetCoord(args, "z"),
                               GetStr(args, "priority"));
          case "save": return Save(GetStr(args, "name"));
          case "designate_cutting": return DesignateCutting(args, true);
          case "undesignate_cutting": return DesignateCutting(args, false);
          case "designate_planting": return DesignatePlanting(args);
          case "set_camera": return SetCamera(args);
          case "batch": return Batch(args);
          default: return Err("not_implemented", command);
        }
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] act '" + command + "' failed: " + e);
        return Err("exception", e.Message);
      }
    }

    private string SetSpeed(float speed) {
      if (speed < 0f) speed = 0f;
      _speed.ChangeSpeed(speed);
      return Ok(new { command = "set_speed", speed });
    }

    private string PlaceBuilding(string specId, int x, int y, int z, string orientation, bool instant, bool autoConnect) {
      if (string.IsNullOrEmpty(specId)) return Err("bad_args", "spec required");
      BlockObjectSpec spec = FindSpec(specId);
      if (spec == null) return Err("unknown_spec", specId);
      if (!TryParseOrientation(orientation, out Orientation o)) return Err("bad_orientation", orientation);

      var coord = new Vector3Int(x, y, z);
      var placement = new Placement(coord, o, FlipMode.Unflipped);

      // WATER-INTAKE BUILDINGS (WaterPump / LargeWaterPump / etc.): the footprint sits on
      // LAND (MatterBelow.Ground), and a separate intake column one tile above the base must
      // hang OVER water deep enough that WaterInputCoordinates.Depth > 0 — otherwise the pump
      // places but is permanently obstructed (produces nothing). CONFIRMED via decompile:
      //   * BlockValidator.BlocksValid does NOT check water at all (only terrain/occupancy/
      //     matter-below), and it does NOT run IPreviewValidator. So the water requirement is
      //     invisible to BlocksValid — approach (B) alone would place obstructed pumps.
      //   * WaterInputPipeValidator.IsValid == (WaterInputCoordinates.Depth > 0), where
      //     Depth = (Transform(WaterInputSpec.WaterInputCoordinates,placement).z + BaseZ + 1)
      //             - firstOccupiedZ, capped by WaterInputSpec.MaxDepth (pump=shallow, large=deep).
      // We therefore run our OWN shoreline search that satisfies BOTH BlocksValid (footprint on
      // land) AND the intake-over-clean-water predicate, trying the 4 orientations so the intake
      // faces the water. Guarded — any failure falls through to the normal path below.
      if (IsWaterIntake(spec)) {
        Placement wp;
        if (TryPlaceWaterIntake(spec, coord, o, out wp)) {
          coord = wp.Coordinates;
          o = wp.Orientation;
          placement = wp;
        } else {
          // No clean, deep-enough shoreline tile found near the guess or district center.
          return Err("invalid_placement",
                     specId + " invalid at (" + x + "," + y + "," + z + "); no clean-water shoreline "
                     + "tile found where the intake sits over water of the accepted depth near the "
                     + "requested point or district center");
        }
      } else if (!_validator.BlocksValid(spec, placement)) {
        // Search around the guess, then around the district center (where building
        // actually happens), across a few height levels.
        Vector3Int found;
        if (FindValidNear(spec, coord, o, out found)
            || FindValidNear(spec, GetDistrictCenter(coord), o, out found)) {
          return Err("invalid_placement",
                     specId + " invalid at (" + x + "," + y + "," + z + ")",
                     new { nearest_valid = new { x = found.x, y = found.y, z = found.z, orientation = o.ToString() } });
        }
        return Err("invalid_placement",
                   specId + " invalid at (" + x + "," + y + "," + z + "); no buildable tile found near the district center");
      }

      // AUTO-ORIENT: when we're going to auto-connect a real building, pick the orientation
      // whose access tile connects to the district road with the shortest contiguous route,
      // so the agent never has to reason about facing. Only reorients among VALID placements
      // and only when it strictly improves connectivity; otherwise keeps the requested one.
      // (ChooseBestOrientation returns `o` unchanged on any uncertainty, so `placement` stays
      // valid.) The requested orientation is respected on ties.
      // Skip auto-orient for water-intake buildings: their orientation is pinned by
      // TryPlaceWaterIntake so the intake faces the water, and ChooseBestOrientation only
      // scores road connectivity (not the water predicate) — letting it rotate here could
      // turn the intake back toward dry land and silently obstruct the pump.
      if (autoConnect && spec.HasSpec<BuildingSpec>() && !IsWaterIntake(spec)) {
        Orientation best = ChooseBestOrientation(spec, coord, o);
        if (best != o) {
          o = best;
          placement = new Placement(coord, o, FlipMode.Unflipped);
        }
      }

      string mode;
      // Capture the just-created entity so AutoConnect resolves the access tile from the
      // ACTUAL object rather than a coord re-lookup. GetBottomObjectAt(coord) returns the
      // TERRAIN block for a relocated/sloped building (e.g. a water pump snapped to the
      // shoreline at a lower z), whose GetComponent<BuildingAccessible> is null -> access
      // resolves null -> AutoConnect fell back to the footprint ring and left the building
      // short of its real access tile. The created ConstructionSite/BlockObject carries the
      // BuildingAccessible directly (confirmed: unfinished buildings report their access).
      BlockObject placedObject = null;
      if (spec.HasSpec<BuildingSpec>()) {
        BuildingSpec buildingSpec = spec.GetSpec<BuildingSpec>();
        if (instant) { var created = _construction.CreateAsFinished(buildingSpec, placement); placedObject = created != null ? created.GetComponent<BlockObject>() : null; mode = "finished"; }
        else { var created = _construction.CreateAsUnfinished(buildingSpec, placement); placedObject = created != null ? created.GetComponent<BlockObject>() : null; mode = "construction_site"; }
      } else {
        if (instant) { placedObject = _blockFactory.CreateFinished(spec, placement); mode = "finished"; }
        else { placedObject = _blockFactory.CreateUnfinished(spec, placement); mode = "construction_site"; }
      }

      // A finished building is only staffed/reachable if it touches the district ROAD
      // network. Lay a Path from the building's real ACCESS tile back to that network.
      // Never fail the placement if this can't connect (report reason so the agent can
      // bridge/dam). Only meaningful for real buildings; a placed Path is its own connection.
      object autoConnectResult = null;
      if (autoConnect && spec.HasSpec<BuildingSpec>()) {
        autoConnectResult = AutoConnect(placedObject, coord, spec);
      }

      // Report the tile+orientation ACTUALLY used (water-intake recovery may have relocated
      // and re-oriented the building from the requested x/y/z), plus the originally requested
      // point, so the agent sees exactly where the pump landed.
      return Ok(new { command = "place_building", spec = specId,
                      x = coord.x, y = coord.y, z = coord.z,
                      requested = new { x, y, z },
                      orientation = o.ToString(), mode, auto_connect = autoConnectResult });
    }

    // === WATER-INTAKE PLACEMENT (WaterPump / LargeWaterPump / DeepWaterPump / …) ===
    //
    // How far along the shoreline we scan for a valid pump tile (Chebyshev radius). A pump
    // must land on land whose intake column hangs over clean water of the accepted depth;
    // the requested guess is often the water tile itself (invalid — pumps sit on LAND), so
    // we sweep nearby land tiles nearest-first.
    private static readonly Vector3Int[] WaterSearchOffsets = BuildSearchOffsets(12);

    // Generic detection of a water-intake building: it carries a WaterInputSpec component
    // spec (CONFIRMED: WaterPump/LargeWaterPump attach WaterInputSpec + WaterInputCoordinates).
    // Falls back to a name check so a spec-shape change can't silently disable the feature.
    // Guarded — never throws.
    private bool IsWaterIntake(BlockObjectSpec spec) {
      try {
        if (spec.HasSpec<WaterInputSpec>()) return true;
      } catch { /* fall through to name check */ }
      try {
        string name = spec.Blueprint != null ? spec.Blueprint.Name : null;
        return name != null && name.IndexOf("Pump", StringComparison.OrdinalIgnoreCase) >= 0;
      } catch {
        return false;
      }
    }

    // Try to find a valid placement for a water-intake building near `guess` (then near the
    // district center), trying all 4 orientations so the intake faces the water. A placement
    // is accepted iff BOTH:
    //   (1) BlockValidator.BlocksValid(spec, placement) — footprint on land, no clashes; AND
    //   (2) the intake column sits over CLEAN water of the accepted depth (IntakeSatisfied).
    // Returns the nearest such placement (nearest to `guess`, then a sweep around the DC).
    // Guarded — returns false on any failure so the caller reports invalid_placement cleanly.
    private bool TryPlaceWaterIntake(BlockObjectSpec spec, Vector3Int guess, Orientation requested, out Placement placement) {
      placement = new Placement(guess, requested, FlipMode.Unflipped);
      try {
        // First: is the requested tile+orientation already a valid, water-satisfied placement?
        if (_validator.BlocksValid(spec, placement) && IntakeSatisfied(spec, guess, requested)) {
          return true;
        }
        // Sweep the shoreline near the guess, then near the district center. Prefer the
        // requested orientation on ties by trying it first for each candidate tile.
        if (SearchWaterIntake(spec, guess, requested, out placement)) return true;
        Vector3Int dc = GetDistrictCenter(guess);
        if (dc != guess && SearchWaterIntake(spec, dc, requested, out placement)) return true;
        return false;
      } catch {
        return false;
      }
    }

    // Nearest-first sweep of candidate LAND tiles around `center`, across a few z-levels,
    // trying the requested orientation first then the other three, returning the first
    // placement that is BlocksValid AND has its intake over clean water of accepted depth.
    private bool SearchWaterIntake(BlockObjectSpec spec, Vector3Int center, Orientation requested, out Placement placement) {
      Orientation[] orients = OrientationsPreferring(requested);
      int[] zDeltas = { 0, -1, 1, -2, 2, -3, 3 };
      foreach (int dz in zDeltas) {
        int z = center.z + dz;
        if (z < 0) continue;
        // center tile first, then nearest-first ring.
        if (TryOrientationsAt(spec, new Vector3Int(center.x, center.y, z), orients, out placement)) return true;
        foreach (Vector3Int off in WaterSearchOffsets) {
          var c = new Vector3Int(center.x + off.x, center.y + off.y, z);
          if (TryOrientationsAt(spec, c, orients, out placement)) return true;
        }
      }
      placement = new Placement(center, requested, FlipMode.Unflipped);
      return false;
    }

    private bool TryOrientationsAt(BlockObjectSpec spec, Vector3Int coord, Orientation[] orients, out Placement placement) {
      foreach (Orientation o in orients) {
        var p = new Placement(coord, o, FlipMode.Unflipped);
        if (_validator.BlocksValid(spec, p) && IntakeSatisfied(spec, coord, o)) {
          placement = p;
          return true;
        }
      }
      placement = new Placement(coord, orients[0], FlipMode.Unflipped);
      return false;
    }

    private static Orientation[] OrientationsPreferring(Orientation first) {
      var all = new List<Orientation> { first };
      foreach (Orientation o in new[] { Orientation.Cw0, Orientation.Cw90, Orientation.Cw180, Orientation.Cw270 }) {
        if (o != first) all.Add(o);
      }
      return all.ToArray();
    }

    // The intake predicate the game uses (CONFIRMED via decompile of WaterInputCoordinates):
    //   startCoord = Transform(WaterInputSpec.WaterInputCoordinates, placement) + (0,0, BaseZ+1)
    //   scan z from startCoord.z-1 down to startCoord.z-MaxDepth; the first OCCUPIED tile
    //   (terrain-underground OR a non-overridable block) stops the column; Depth = gap size.
    //   WaterInputPipeValidator.IsValid == (Depth > 0).
    // We additionally require the intake tile to hold CLEAN water (WaterDepth>0,
    // contamination==0) so the placed pump actually produces clean water — the caller's
    // "near a clean-water shoreline" contract. Returns false on any uncertainty (guarded),
    // which just makes that candidate be skipped.
    private bool IntakeSatisfied(BlockObjectSpec spec, Vector3Int coord, Orientation o) {
      try {
        WaterInputSpec wis;
        try { wis = spec.GetSpec<WaterInputSpec>(); }
        catch { return false; }               // no water-input spec -> not our predicate
        if (wis == null) return false;

        int maxDepth = Mathf.Max(1, wis.MaxDepth);
        // Transform the footprint-local intake coord by this placement, matching the game's
        // Blocks.Transform: Orientation.Transform(local) + placementCoord (Unflipped = identity).
        Vector3Int intakeXY = coord + o.Transform(wis.WaterInputCoordinates);
        int baseZ = spec.BaseZ;               // spec-declared base offset (usually 0)
        int topZ = intakeXY.z + baseZ + 1;    // startCoordinates.z in the game code

        // Find the first occupied z scanning down (game's GetZCoordinateLimitedByDepth). The
        // intake floor sits just above it; Depth = topZ - intakeFloorZ. Depth>0 required.
        int intakeFloorZ = topZ - maxDepth;   // default if nothing occupied within maxDepth
        for (int zz = topZ - 1; zz >= topZ - maxDepth; zz--) {
          if (IntakeTileOccupied(new Vector3Int(intakeXY.x, intakeXY.y, zz))) {
            intakeFloorZ = zz + 1;
            break;
          }
        }
        int depth = topZ - intakeFloorZ;
        if (depth <= 0) return false;         // == WaterInputPipeValidator.IsValid == false

        // The tile the intake actually draws from is the column floor. Require clean water
        // there so the pump is productive (a dry gap satisfies Depth>0 but pumps nothing).
        var intakeCell = new Vector3Int(intakeXY.x, intakeXY.y, intakeFloorZ);
        float waterDepth = _waterMap.WaterDepth(intakeCell);
        if (waterDepth <= 0f) return false;   // no water under the intake
        float contamination = _waterMap.ColumnContamination(intakeCell);
        if (contamination > 0f) return false; // not clean water
        return true;
      } catch {
        return false;
      }
    }

    // Mirrors WaterInputCoordinates.IsTileOccupied: terrain-underground counts as occupied,
    // as does any non-overridable block object (pipes/allowers don't block). Guarded.
    private bool IntakeTileOccupied(Vector3Int cell) {
      try {
        if (_terrain.Underground(cell)) return true;
        return _blocks.AnyNonOverridableObjectsAt(cell, BlockOccupations.All);
      } catch {
        return false;
      }
    }

    // Number of Path construction sites AutoConnect will ever lay in one call. Hard cap
    // so a runaway BFS (e.g. a very long route hugging a lake) can't carpet the map.
    private const int MaxPathTiles = 48;
    // BFS exploration cap: how many tiles we pop before giving up looking for a road.
    // Must be large enough for a breadth-first flood to actually REACH the road: to
    // reach Manhattan distance d, BFS pops ~2*d^2 tiles, so a road ~10 tiles away
    // needs ~200 pops. 60 only reached buildings ~3 tiles from the DC. Cover the map.
    private const int MaxBfsExpansion = 4000;

    // Lay a contiguous Path from a freshly placed building back to the district-road
    // network so the FINISHED building is connected (staffed/reachable). Runs on the
    // main thread (Act does). Wrapped end-to-end in try/catch: a paving failure must
    // NEVER fail the building placement or throw out of Act.
    //
    // ROOT CAUSE THIS FIXES (CONFIRMED via decompile): the game connects a building
    // through its ACCESS point, not by mere adjacency. DistrictBuilding.InstantDistrict
    // becomes non-null iff DistrictCenter.AccessibleIsOnInstantDistrictRoad ->
    // IDistrictService.IsOnInstantDistrictRoad(District, access) is true, where `access`
    // is BuildingAccessible.Accessible's single access world-pos (from CalculateAccess()
    // = GridToWorldCentered(PositionedEntrance.Coordinates)). WorldToId floors that to
    // ONE grid tile. So THAT tile — the access tile — must itself be a road node. For a
    // LumberjackFlag the access tile is the flag's own tile: paving the ring around the
    // footprint (never the access tile) leaves it unreachable forever. We therefore route
    // to the access tile and guarantee the access tile ends up ON the road.
    //
    // Returns an anonymous object folded into place_building's JSON under "auto_connect":
    //   { connected:bool, paths_laid:int, path_tiles:[{x,y,z}], access_tiles?:[..], reason?:string }
    private object AutoConnect(BlockObject placedObject, Vector3Int buildingCoord, BlockObjectSpec buildingSpec) {
      try {
        BlockObjectSpec pathSpec = FindSpec("Path");
        if (pathSpec == null) {
          return new { connected = false, paths_laid = 0, reason = "no_path_spec" };
        }

        // Resolve the game's actual access tile(s) from the just-created object (ground
        // truth: BuildingAccessible on the real entity). Only fall back to a coord lookup
        // if we somehow weren't handed the object.
        List<Vector3Int> accessTiles = placedObject != null ? _reachability.AccessTiles(placedObject) : null;
        if (accessTiles == null || accessTiles.Count == 0) {
          accessTiles = ResolveAccessTiles(buildingCoord);
        }
        bool haveAccess = accessTiles != null && accessTiles.Count > 0;

        // The building's own footprint tiles. In access-tile mode these are IMPASSABLE
        // obstacles for the BFS so the route wraps AROUND the building and never tries to
        // pave through it (the footprint tiles occupy the Path slot and can't be paved,
        // and routing "through" them would break contiguity).
        var footprint = FootprintTiles(buildingCoord, buildingSpec);

        // Seed tiles for the BFS. CONTIGUITY-CRITICAL: seed ONLY the access tile(s) when we
        // have them, so the reconstructed parent chain is one connected route from an access
        // tile onto the road. Seeding the footprint ring (multiple disjoint sides) is what
        // produced the old stranded-segment bug — the DC-side seeds joined the road on their
        // own while the access-side seed stayed floating behind the building. The footprint
        // ring is used ONLY as the fallback when the building exposes no access tile.
        List<Vector3Int> startTiles = haveAccess
            ? accessTiles
            : FootprintAdjacentTiles(buildingCoord, buildingSpec);

        // Already connected? An access tile (or, in fallback, a footprint-adjacent tile)
        // already ON the road means no paving needed.
        foreach (Vector3Int t in startTiles) {
          if (_reachability.IsTileOnDistrictRoad(t)) {
            return new { connected = true, paths_laid = 0, path_tiles = new object[0],
                         access_tiles = CoordList(accessTiles) };
          }
        }

        // (b+c) Single contiguous route from an access tile onto the district road, routing
        // AROUND the footprint. This is the SAME computation the pre-placement orientation
        // scorer runs in dry-run mode — here we compute it for real and pave it.
        List<Vector3Int> route = ComputeContiguousRoute(pathSpec, startTiles, footprint, haveAccess);

        if (route == null) {
          // No land route to any district road within the cap (e.g. across a lake).
          // Do NOT fail the building — just report so the agent knows to build a bridge/dam.
          return new { connected = false, paths_laid = 0, reason = "no_land_route",
                       access_tiles = CoordList(accessTiles) };
        }

        // route is [road, .., accessTile]. Pave EVERY tile that isn't already on the road:
        // the access tile plus all intermediate tiles, forming one connected chain onto the
        // road. Tiles already on the road (the road end) are skipped — they're the target.
        var laid = new List<object>();
        foreach (Vector3Int tile in route) {
          if (_reachability.IsTileOnDistrictRoad(tile)) continue; // road end / already road
          if (laid.Count >= MaxPathTiles) break;
          if (HasPathAt(tile)) continue;                 // already a path here (still contiguous)
          if (!CanPave(pathSpec, tile)) continue;        // re-validate before creating
          // Lay the connector path as a CONSTRUCTION SITE so it's built by beavers like
          // everything else (user requirement). Paths are free (0 materials) and beavers
          // build them outward from the road: the tile adjacent to the road is reachable
          // by builders first, and each built tile makes the next reachable, so the chain
          // completes and the building connects. (Reachability is therefore delayed until
          // the chain is built - the agent must wait, not demolish, while paths are under
          // construction.)
          _blockFactory.CreateUnfinished(pathSpec, new Placement(tile, Orientation.Cw0, FlipMode.Unflipped));
          laid.Add(new { x = tile.x, y = tile.y, z = tile.z });
        }

        return new { connected = true, paths_laid = laid.Count, path_tiles = laid,
                     access_tiles = CoordList(accessTiles) };
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] auto_connect failed: " + e);
        return new { connected = false, paths_laid = 0, reason = "exception" };
      }
    }

    // Single contiguous route from a start (access) tile onto the nearest district-road
    // tile, routing AROUND the footprint obstacles. Returns the route as [road, .., access]
    // (road end included so callers can skip it), or null when no road is reachable within
    // the BFS cap. LAYS NOTHING — this is the shared core used both to actually pave (in
    // AutoConnect) and to DRY-RUN score orientations (in PlaceBuilding). Contiguity is
    // guaranteed: each BFS parent edge is a 4-neighbour, so the reconstructed chain is
    // connected end to end. Guarded — never throws (returns null on any error).
    private List<Vector3Int> ComputeContiguousRoute(BlockObjectSpec pathSpec,
                                                    List<Vector3Int> startTiles,
                                                    HashSet<Vector3Int> footprint,
                                                    bool useFootprintObstacles) {
      try {
        var parent = new Dictionary<Vector3Int, Vector3Int>();
        var visited = new HashSet<Vector3Int>();
        var queue = new Queue<Vector3Int>();
        Vector3Int roadTile = default(Vector3Int);
        bool foundRoad = false;

        // Footprint obstacle test is by (x,y): the building occupies its whole column,
        // and terrain-snapped neighbours may carry a different z than the stored footprint.
        var footprintXY = new HashSet<Vector2Int>();
        if (useFootprintObstacles) {
          foreach (Vector3Int f in footprint) footprintXY.Add(new Vector2Int(f.x, f.y));
        }

        // Seeds map to themselves; reconstruction stops at parent==self, so the route always
        // includes the access tile it started from.
        foreach (Vector3Int t in startTiles) {
          if (useFootprintObstacles && footprintXY.Contains(new Vector2Int(t.x, t.y))) continue;
          if (visited.Add(t)) { parent[t] = t; queue.Enqueue(t); }
        }

        int expanded = 0;
        while (queue.Count > 0 && expanded < MaxBfsExpansion) {
          Vector3Int cur = queue.Dequeue();
          expanded++;

          if (_reachability.IsTileOnDistrictRoad(cur)) {
            roadTile = cur;
            foundRoad = true;
            break;
          }

          // Terrain-aware neighbours: each horizontal step lands on that column's
          // SURFACE height, and is walkable only if the height change is <= 1 (beavers
          // ramp up/down a single level; paths follow terrain). This lets a pump at
          // water level connect up to the district road one level above.
          foreach (Vector3Int n in WalkableNeighbors(cur)) {
            if (!visited.Add(n)) continue;
            if (useFootprintObstacles && footprintXY.Contains(new Vector2Int(n.x, n.y))) continue;
            // Walk a neighbour if we can route through it: it's already on the road,
            // it already carries a path (traverse it, we just won't re-lay), or a fresh
            // Path would validly place there.
            if (!_reachability.IsTileOnDistrictRoad(n) && !HasPathAt(n) && !CanPave(pathSpec, n)) continue;
            parent[n] = cur;
            queue.Enqueue(n);
          }
        }

        if (!foundRoad) return null;

        var route = new List<Vector3Int>();
        Vector3Int walk = roadTile;
        while (true) {
          route.Add(walk);
          Vector3Int prev;
          if (!parent.TryGetValue(walk, out prev) || prev.Equals(walk)) break;
          walk = prev;
        }
        return route;
      } catch {
        return null;
      }
    }

    // AUTO-ORIENT: choose the placement orientation whose ACCESS tile connects to the
    // district road with the SHORTEST contiguous route, so the agent never has to reason
    // about which way a building faces. A building's access tile is orientation-dependent
    // (access = coord + Orientation.Transform(Entrance.Coordinates), CONFIRMED via decompile:
    // PositionedEntrance.Coordinates = Blocks.Transform(Entrance.Coordinates, placement),
    // and GridToWorldCentered->WorldToGridInt round-trips an integer tile). When access faces
    // AWAY from the road the wrap-around BFS can dead-end (no_land_route); the toward-road
    // orientation connects cleanly.
    //
    // Only applies to buildings with an entrance (the ones that carry BuildingAccessible;
    // Awake throws otherwise). Scores each of the 4 orientations that is a VALID placement by
    // the dry-run route length (0 if the access tile is already on the road; +inf if no route).
    // Prefers the agent's requested orientation on ties. Returns the requested orientation
    // unchanged when nothing scores better (or on any uncertainty) — never worse than before.
    private Orientation ChooseBestOrientation(BlockObjectSpec spec, Vector3Int coord, Orientation requested) {
      try {
        // Only entrance-bearing buildings have an orientation-dependent access tile.
        if (!spec.Entrance.HasEntrance) return requested;
        BlockObjectSpec pathSpec = FindSpec("Path");
        if (pathSpec == null) return requested;

        Orientation[] candidates = { Orientation.Cw0, Orientation.Cw90, Orientation.Cw180, Orientation.Cw270 };
        Orientation best = requested;
        int bestScore = int.MaxValue;
        bool haveBest = false;

        foreach (Orientation o in candidates) {
          // Must be a valid placement in this orientation, else skip.
          if (!_validator.BlocksValid(spec, new Placement(coord, o, FlipMode.Unflipped))) continue;

          Vector3Int access = AnalyticAccessTile(spec, coord, o);
          var footprint = FootprintTiles(coord, spec); // orientation-agnostic bounding box (safe superset)
          int score;
          if (_reachability.IsTileOnDistrictRoad(access)) {
            score = 0; // already on the road — perfect
          } else {
            var route = ComputeContiguousRoute(pathSpec,
                                                new List<Vector3Int> { access },
                                                footprint, useFootprintObstacles: true);
            score = route == null ? int.MaxValue : route.Count; // shorter route == better
          }

          // Strictly-better wins; on a tie, prefer the agent's requested orientation.
          bool better = !haveBest || score < bestScore
                        || (score == bestScore && o == requested);
          if (better) { best = o; bestScore = score; haveBest = true; }
        }

        // If nothing connects (all +inf) keep the requested orientation — AutoConnect will
        // report no_land_route honestly rather than silently reorienting to no benefit.
        if (!haveBest || bestScore == int.MaxValue) return requested;
        return best;
      } catch {
        return requested;
      }
    }

    // The access grid tile for a placement WITHOUT creating the entity, replicating the
    // game's transform: access = coord + Orientation.Transform(Entrance.Coordinates)
    // (FlipMode.Unflipped is identity). Matches BuildingAccessible.CalculateAccess() for the
    // common (non-ForceOneFinalAccess) case; the real post-placement AccessTiles remains the
    // ground truth used for the actual paving, so a ForceOneFinalAccess building still paves
    // correctly — this only steers orientation selection.
    private static Vector3Int AnalyticAccessTile(BlockObjectSpec spec, Vector3Int coord, Orientation o) {
      Vector3Int local = spec.Entrance.Coordinates;
      return coord + o.Transform(local);
    }

    // Find the just-placed building's BlockObject at `coord` and read its game access
    // tile(s) via ReachabilityReader.AccessTiles (BuildingAccessible/Accessible). Returns
    // null when we can't resolve a BuildingAccessible (path/walkable or an odd spec) so
    // AutoConnect falls back to the footprint ring. Guarded — never throws.
    private List<Vector3Int> ResolveAccessTiles(Vector3Int coord) {
      try {
        BlockObject obj = _blocks.AnyObjectAt(coord) ? _blocks.GetBottomObjectAt(coord) : null;
        if (obj == null) return null;
        return _reachability.AccessTiles(obj);
      } catch {
        return null;
      }
    }

    private static object CoordList(List<Vector3Int> tiles) {
      if (tiles == null || tiles.Count == 0) return null;
      var list = new List<object>();
      foreach (Vector3Int t in tiles) list.Add(new { x = t.x, y = t.y, z = t.z });
      return list;
    }

    // The building's footprint tiles at its z, approximated from the spec's unrotated
    // block size (buildingCoord is the origin corner). Orientation is ignored — the size
    // bounding box is a safe superset of the true footprint (only over-covers when a
    // rotated non-square building is off-axis), which is exactly what we want when using
    // it as a BFS obstacle set: never route through the building. Falls back to the single
    // origin tile on any uncertainty. Guarded — never throws.
    private HashSet<Vector3Int> FootprintTiles(Vector3Int buildingCoord, BlockObjectSpec spec) {
      var footprint = new HashSet<Vector3Int>();
      try {
        Vector3Int size = spec.Size; // x,y horizontal; z vertical
        int sx = Mathf.Max(1, size.x);
        int sy = Mathf.Max(1, size.y);
        for (int dx = 0; dx < sx; dx++) {
          for (int dy = 0; dy < sy; dy++) {
            footprint.Add(new Vector3Int(buildingCoord.x + dx, buildingCoord.y + dy, buildingCoord.z));
          }
        }
      } catch {
        footprint.Clear();
      }
      if (footprint.Count == 0) {
        footprint.Add(buildingCoord);
      }
      return footprint;
    }

    // Orthogonal ring of tiles around a building's footprint at the building's z — the
    // FALLBACK BFS seeds when a building exposes no access tile. Each actual Path placement
    // is validated separately.
    private List<Vector3Int> FootprintAdjacentTiles(Vector3Int buildingCoord, BlockObjectSpec spec) {
      var footprint = FootprintTiles(buildingCoord, spec);
      var result = new List<Vector3Int>();
      var seen = new HashSet<Vector3Int>();
      foreach (Vector3Int f in footprint) {
        foreach (Vector3Int n in Orthogonal(f)) {
          if (footprint.Contains(n)) continue;
          if (seen.Add(n)) result.Add(n);
        }
      }
      return result;
    }

    private static IEnumerable<Vector3Int> Orthogonal(Vector3Int c) {
      yield return new Vector3Int(c.x + 1, c.y, c.z);
      yield return new Vector3Int(c.x - 1, c.y, c.z);
      yield return new Vector3Int(c.x, c.y + 1, c.z);
      yield return new Vector3Int(c.x, c.y - 1, c.z);
    }

    // The 4 horizontal neighbours snapped to each column's terrain SURFACE height,
    // walkable only if the height step from `c` is <= 1 (beavers/paths ramp one
    // level). Lets auto-connect route across small terrain steps (e.g. a pump at
    // the water's edge up to the district road one level higher).
    private IEnumerable<Vector3Int> WalkableNeighbors(Vector3Int c) {
      var dirs = new[] {
        new Vector2Int(1, 0), new Vector2Int(-1, 0),
        new Vector2Int(0, 1), new Vector2Int(0, -1),
      };
      foreach (Vector2Int d in dirs) {
        int nx = c.x + d.x, ny = c.y + d.y;
        int sz;
        try { sz = _terrain.GetTerrainHeightBelow(new Vector3Int(nx, ny, TerrainTopZ)); }
        catch { sz = c.z; }
        if (Math.Abs(sz - c.z) <= 1) {
          yield return new Vector3Int(nx, ny, sz);
        }
      }
    }

    // A height ceiling for surface scans; the map's vertical extent never exceeds this.
    private const int TerrainTopZ = 32;

    // Can a Path be placed here? Valid per BlockValidator (handles terrain/water/occupancy)
    // and not already occupied by a non-path object we'd clash with.
    private bool CanPave(BlockObjectSpec pathSpec, Vector3Int tile) {
      if (HasPathAt(tile)) return false;
      return _validator.BlocksValid(pathSpec, new Placement(tile, Orientation.Cw0, FlipMode.Unflipped));
    }

    // Is there already a path/road occupying this tile's Path slot? CONFIRMED: the game
    // stores at most one object in the per-tile Path slot (paths, roads, path sites);
    // IBlockService.GetPathObjectAt returns it or null. Robust and spec-name-free.
    private bool HasPathAt(Vector3Int tile) {
      try {
        return _blocks.GetPathObjectAt(tile) != null;
      } catch {
        return false;
      }
    }

    private string Demolish(int x, int y, int z) {
      // A building's reported coord (e.g. a pump's water-intake tile at z-1) may not
      // be the exact deletable block tile. Search the z-column then the 3x3
      // neighbourhood so "remove the thing at roughly here" just works.
      BlockObject obj = FindDeletableNear(x, y, z);
      if (obj == null) return Err("nothing_there", "(" + x + "," + y + "," + z + ")");
      Vector3Int at = obj.Coordinates;
      _entities.Delete(obj);
      return Ok(new { command = "demolish", x = at.x, y = at.y, z = at.z, requested = new { x, y, z } });
    }

    private BlockObject FindDeletableNear(int x, int y, int z) {
      int[] zDeltas = { 0, 1, -1, 2, -2, 3 };
      foreach (int dz in zDeltas) {
        BlockObject o = DeletableAt(new Vector3Int(x, y, z + dz));
        if (o != null) return o;
      }
      for (int dx = -1; dx <= 1; dx++) {
        for (int dy = -1; dy <= 1; dy++) {
          if (dx == 0 && dy == 0) continue;
          foreach (int dz in zDeltas) {
            BlockObject o = DeletableAt(new Vector3Int(x + dx, y + dy, z + dz));
            if (o != null) return o;
          }
        }
      }
      return null;
    }

    private BlockObject DeletableAt(Vector3Int coord) {
      if (!_blocks.AnyObjectAt(coord)) return null;
      BlockObject obj = _blocks.GetBottomObjectAt(coord);
      if (obj == null || !obj.CanDelete()) return null;
      try {
        string name = obj.GetComponent<Building>()?.Spec?.Blueprint?.Name;
        if (name != null && name.StartsWith("DistrictCenter", StringComparison.OrdinalIgnoreCase)) return null;
      } catch { /* no building component — a path etc, deletable */ }
      return obj;
    }

    // Set builder priority on a construction site. BuilderPrioritizable is only
    // Enabled while the building is unfinished — on a finished building this
    // correctly reports not_a_site.
    private string SetPriority(int x, int y, int z, string priorityStr) {
      var coord = new Vector3Int(x, y, z);
      if (!_blocks.AnyObjectAt(coord)) return Err("nothing_there", "(" + x + "," + y + "," + z + ")");
      BlockObject obj = _blocks.GetBottomObjectAt(coord);
      if (obj == null) return Err("nothing_there", "(" + x + "," + y + "," + z + ")");
      var pr = obj.GetComponent<BuilderPrioritizable>();
      if (pr == null || !pr.Enabled) {
        return Err("not_a_site", "no active construction site at (" + x + "," + y + "," + z + ")");
      }
      if (!TryParsePriority(priorityStr, out Priority p)) return Err("bad_priority", priorityStr);
      pr.SetPriority(p);
      return Ok(new { command = "set_priority", x, y, z, priority = p.ToString() });
    }

    private static bool TryParsePriority(string s, out Priority p) {
      p = Priority.Normal;
      if (string.IsNullOrEmpty(s)) return true;
      switch (s.Trim().ToLowerInvariant()) {
        case "0": case "verylow": case "very_low": p = Priority.VeryLow; return true;
        case "1": case "low": p = Priority.Low; return true;
        case "2": case "normal": p = Priority.Normal; return true;
        case "3": case "high": p = Priority.High; return true;
        case "4": case "veryhigh": case "very_high": p = Priority.VeryHigh; return true;
        default: return false;
      }
    }

    // Designate (or undesignate) trees for cutting. Cutting in Timberborn is a
    // GLOBAL registry keyed by tile: TreeCuttingArea.AddCoordinates — a staffed
    // Lumberjack then fells any reachable designated tree (the flag is a workplace,
    // not a radius). Args: {"tiles":[{x,y,z},...]} to mark specific trees, or
    // {"all":true} (or no tiles) to mark every currently-mature tree on the map.
    private string DesignateCutting(JObject args, bool add) {
      var tiles = new List<Vector3Int>();
      JArray arr = args?["tiles"] as JArray;
      bool all = GetBool(args, "all", false);
      if (arr != null && arr.Count > 0 && !all) {
        foreach (JToken t in arr) {
          var o = t as JObject;
          if (o == null) continue;
          tiles.Add(new Vector3Int(
            Present(o["x"]) ? o["x"].ToObject<int>() : 0,
            Present(o["y"]) ? o["y"].ToObject<int>() : 0,
            Present(o["z"]) ? o["z"].ToObject<int>() : 0));
        }
      } else {
        // No explicit tiles (or all=true): operate on every mature tree.
        tiles = _resources.MatureTreeTiles();
      }
      if (tiles.Count == 0) return Err("no_trees", "no tiles given and no mature trees found");

      if (add) { _cuttingArea.AddCoordinates(tiles); }
      else { _cuttingArea.RemoveCoordinates(tiles); }
      return Ok(new { command = add ? "designate_cutting" : "undesignate_cutting", tiles = tiles.Count });
    }

    // Designate tiles for a Forester to replant (the wood-sustainability loop).
    // Like cutting, planting is a global registry keyed by tile: a Forester only
    // plants MARKED spots that are also moist+uncontaminated+empty+in-range. Args:
    // {"tiles":[{x,y,z},...], "species":"Pine"}. species is a plantable template
    // name; defaults to "Pine" (a common Folktails tree). The game's own validators
    // reject bad tiles at plant time, so over-marking is safe.
    private string DesignatePlanting(JObject args) {
      string species = GetStr(args, "species") ?? "Pine";
      var tiles = new List<Vector3Int>();
      JArray arr = args?["tiles"] as JArray;
      if (arr != null) {
        foreach (JToken t in arr) {
          var o = t as JObject;
          if (o == null) continue;
          tiles.Add(new Vector3Int(
            Present(o["x"]) ? o["x"].ToObject<int>() : 0,
            Present(o["y"]) ? o["y"].ToObject<int>() : 0,
            Present(o["z"]) ? o["z"].ToObject<int>() : 0));
        }
      }
      if (tiles.Count == 0) return Err("bad_args", "tiles[] required");
      int marked = 0;
      foreach (Vector3Int t in tiles) {
        try { _planting.SetPlantingCoordinates(t, species); marked++; }
        catch (Exception e) { Debug.LogError("[TimberBridge] plant mark failed at " + t + ": " + e); }
      }
      return Ok(new { command = "designate_planting", species, tiles = marked });
    }

    // Frame the colony for a screenshot: move the RTS camera to look at a target tile
    // from a high, top-down-ish angle, zoomed out enough to see the whole base.
    //
    // Args: {x, y, z? (target tile; if x/y omitted, center on the district center),
    //        zoom (0..1 normalized, OR a raw camera distance >1), tilt? (degrees, steep=top-down)}
    //
    // Camera driving (all CONFIRMED via decompile of Timberborn.CameraSystem.CameraService,
    // a public injectable singleton — ISaveableSingleton/ILoadableSingleton/ILateUpdatableSingleton):
    //   * void MoveTargetTo(Vector3 worldPoint)  -> sets Target (the look-at point); it
    //     internally does WorldToGrid -> ClampTarget(map bounds) -> GridToWorld, so we must
    //     pass a WORLD position (we convert the grid tile via CoordinateSystem.GridToWorldCentered).
    //     CONFIRMED.
    //   * void SetZoomLevel(float distanceFromTarget, float delta) -> converts a desired
    //     camera-to-target distance into ZoomLevel and CLAMPS it to the active zoom limits.
    //     Passing a huge distance saturates to the game's max zoom-out (whole-base overview),
    //     so our distance constants need not be exact. CONFIRMED.
    //   * float ZoomLevel { get; set; }          -> resulting zoom, read back for the report. CONFIRMED.
    //   * float VerticalAngle { get; set; }      -> camera pitch/tilt in degrees; 90 = straight
    //     down, ~0 = horizon. The public setter is UNCLAMPED (unlike ModifyVerticalAngle, which
    //     clamps to VerticalAngleLimits), letting us pick a steeper top-down framing than normal
    //     play allows. We clamp to [1,89] ourselves to avoid a degenerate straight-down flip.
    //     CONFIRMED.
    // The camera reads Target/ZoomLevel/VerticalAngle in its per-frame LateUpdateSingleton
    // (UpdatePositionAndRotation), so the change takes effect on the next rendered frame —
    // before /screenshot captures. CONFIRMED (UpdatePositionAndRotation reads exactly these).
    //
    // Guarded end-to-end: any failure returns a clean Err instead of throwing out of Act.
    private string SetCamera(JObject args) {
      try {
        // Resolve the target tile. If no explicit coordinate was given, frame the district
        // center (where the base is). Otherwise use x/y; snap z to the terrain surface when
        // omitted so the look-at point sits on the ground, not at z=0.
        Vector3Int target;
        bool haveCoord = Present(args?["x"]) || Present(args?["y"])
                         || Present(args?["position"]) || Present(args?["pos"])
                         || Present(args?["coordinates"]) || Present(args?["coord"]);
        if (haveCoord) {
          int x = GetCoord(args, "x");
          int y = GetCoord(args, "y");
          int z;
          if (Present(args?["z"]) || (args?["position"] is JObject) || (args?["position"] is JArray)) {
            z = GetCoord(args, "z");
          } else {
            z = 0;
          }
          // If z wasn't meaningfully supplied, snap to the terrain surface at (x,y).
          if (!Present(args?["z"])) {
            try { z = _terrain.GetTerrainHeightBelow(new Vector3Int(x, y, TerrainTopZ)); }
            catch { z = 0; }
          }
          target = new Vector3Int(x, y, z);
        } else {
          target = GetDistrictCenter(new Vector3Int(0, 0, 0));
        }

        // Grid tile -> centered world position -> camera look-at. CONFIRMED: MoveTargetTo
        // expects a world point (it clamps to the map internally).
        Vector3 world = CoordinateSystem.GridToWorldCentered(target);
        _camera.MoveTargetTo(world);

        // Zoom: normalized 0..1 (0 = close, 1 = wide) maps to a camera distance; a value >1
        // is taken as an explicit distance. Omitted -> a wide overview (large distance that
        // SetZoomLevel saturates to the game's max zoom-out). SetZoomLevel clamps to the real
        // limits, so these constants only set the usable normalized span.
        const float NearDistance = 25f;   // zoom=0  : close in
        const float FarDistance = 600f;   // zoom=1  : wide overview (clamps to max if smaller)
        float distance;
        if (Present(args?["zoom"])) {
          float zoom = GetFloat(args, "zoom", 1f);
          distance = zoom <= 1f
              ? Mathf.Lerp(NearDistance, FarDistance, Mathf.Clamp01(zoom))
              : zoom; // explicit distance
        } else {
          distance = FarDistance; // default: frame the whole base
        }
        _camera.SetZoomLevel(distance, 0f);

        // Tilt (pitch): steep top-down-ish by default so a screenshot reads like an overview.
        // 90 = straight down; ~55-80 keeps buildings legible in 3D. Clamped to [1,89].
        float tilt = Present(args?["tilt"]) ? GetFloat(args, "tilt", 70f) : 70f;
        tilt = Mathf.Clamp(tilt, 1f, 89f);
        _camera.VerticalAngle = tilt;

        return Ok(new {
          command = "set_camera",
          target = new { x = target.x, y = target.y, z = target.z },
          world = new { x = world.x, y = world.y, z = world.z },
          zoom_level = _camera.ZoomLevel,
          distance,
          tilt = _camera.VerticalAngle
        });
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] set_camera failed: " + e);
        return Err("exception", e.Message);
      }
    }

    // Execute an ordered list of actions in one main-thread hop:
    // {"command":"batch","args":{"actions":[{"command":..,"args":{..}},...],"stop_on_error":false}}
    // Returns per-action results so the agent can commit a whole mini-plan per
    // decision turn instead of paying one LLM round-trip per placement.
    private string Batch(JObject args) {
      JArray actions = args?["actions"] as JArray;
      if (actions == null || actions.Count == 0) return Err("bad_args", "actions[] required");
      if (actions.Count > 16) return Err("bad_args", "max 16 actions per batch");
      bool stopOnError = args?["stop_on_error"] != null
                         && args["stop_on_error"].Type != JTokenType.Null
                         && args["stop_on_error"].ToObject<bool>();

      var results = new List<JObject>();
      bool allOk = true;
      foreach (JToken t in actions) {
        var item = t as JObject;
        string cmd = item != null ? (string)item["command"] : null;
        string res;
        if (string.IsNullOrEmpty(cmd)) {
          res = Err("bad_args", "each action needs a command");
        } else if (cmd == "batch") {
          res = Err("bad_args", "no nested batches");
        } else {
          res = Act(cmd, item["args"] as JObject);
        }
        JObject parsed = JObject.Parse(res);
        parsed["command"] = cmd ?? "?";
        results.Add(parsed);
        bool ok = parsed["ok"] != null && parsed["ok"].ToObject<bool>();
        if (!ok) {
          allOk = false;
          if (stopOnError) break;
        }
      }
      return JsonConvert.SerializeObject(new {
        ok = allOk, command = "batch", executed = results.Count,
        total = actions.Count, results
      });
    }

    private string Save(string name) {
      if (string.IsNullOrEmpty(name)) return Err("bad_args", "name required");
      SaveReference loaded = _loader.LoadedSave;
      var saveRef = new SaveReference(name, loaded.SettlementReference);
      _saver.SaveInstantlySkippingNameValidation(saveRef, () => { });
      return Ok(new { command = "save", name });
    }

    // spec id -> BlockObjectSpec. Building ids are faction-qualified
    // ("WaterPump.Folktails"); accept the bare id too, case-insensitively, so the
    // agent can just say "WaterPump". Path etc. are bare and match exactly.
    private BlockObjectSpec FindSpec(string specId) {
      if (string.IsNullOrEmpty(specId)) return null;
      foreach (BlockObjectSpec spec in _templates.GetAll<BlockObjectSpec>()) {
        string name = spec.Blueprint != null ? spec.Blueprint.Name : null;
        if (name == null) continue;
        if (string.Equals(name, specId, System.StringComparison.OrdinalIgnoreCase)) return spec;
        if (name.StartsWith(specId + ".", System.StringComparison.OrdinalIgnoreCase)) return spec;
      }
      return null;
    }

    private Vector3Int GetDistrictCenter(Vector3Int fallback) {
      foreach (DistrictCenter dc in _districts.FinishedDistrictCenters) {
        return dc.CenterCoordinates;
      }
      return fallback;
    }

    // Spiral x/y (nearest-first) at a few height levels around center; first valid wins.
    private bool FindValidNear(BlockObjectSpec spec, Vector3Int center, Orientation o, out Vector3Int found) {
      int[] zDeltas = { 0, -1, 1, -2, 2, -3, 3 };
      foreach (int dz in zDeltas) {
        int z = center.z + dz;
        if (z < 0) continue;
        foreach (Vector3Int off in SearchOffsets) {
          var c = new Vector3Int(center.x + off.x, center.y + off.y, z);
          if (_validator.BlocksValid(spec, new Placement(c, o, FlipMode.Unflipped))) {
            found = c;
            return true;
          }
        }
      }
      found = new Vector3Int(0, 0, 0);
      return false;
    }

    private static bool TryParseOrientation(string s, out Orientation o) {
      o = Orientation.Cw0;
      if (string.IsNullOrEmpty(s)) return true;
      switch (s.Trim().ToLowerInvariant()) {
        case "0": case "cw0": case "n": case "north": o = Orientation.Cw0; return true;
        case "90": case "cw90": case "e": case "east": o = Orientation.Cw90; return true;
        case "180": case "cw180": case "s": case "south": o = Orientation.Cw180; return true;
        case "270": case "cw270": case "w": case "west": o = Orientation.Cw270; return true;
        default: return false;
      }
    }

    private static Vector3Int[] BuildSearchOffsets(int radius) {
      var list = new List<KeyValuePair<int, Vector3Int>>();
      for (int dx = -radius; dx <= radius; dx++) {
        for (int dy = -radius; dy <= radius; dy++) {
          if (dx == 0 && dy == 0) continue;
          list.Add(new KeyValuePair<int, Vector3Int>(dx * dx + dy * dy, new Vector3Int(dx, dy, 0)));
        }
      }
      list.Sort((a, b) => a.Key.CompareTo(b.Key));
      var arr = new Vector3Int[list.Count];
      for (int i = 0; i < list.Count; i++) arr[i] = list[i].Value;
      return arr;
    }

    // --- arg + result helpers ---
    private static bool Present(JToken t) { return t != null && t.Type != JTokenType.Null; }
    private static string GetStr(JObject a, string k) { JToken t = a?[k]; return Present(t) ? t.ToString() : null; }
    private static int GetInt(JObject a, string k) { JToken t = a?[k]; return Present(t) ? t.ToObject<int>() : 0; }
    private static float GetFloat(JObject a, string k, float d) { JToken t = a?[k]; return Present(t) ? t.ToObject<float>() : d; }
    private static bool GetBool(JObject a, string k, bool d) { JToken t = a?[k]; return Present(t) ? t.ToObject<bool>() : d; }

    // Coordinate from flat args[k], nested args.position{k}, or a position array
    // args.position[[x,y,z]] — models emit all three shapes.
    private static int GetCoord(JObject a, string k) {
      JToken t = a?[k];
      if (Present(t)) return t.ToObject<int>();
      JToken pos = a?["position"] ?? a?["pos"] ?? a?["coordinates"] ?? a?["coord"];
      if (pos is JObject po) {
        t = po[k];
        if (Present(t)) return t.ToObject<int>();
      } else if (pos is JArray pa) {
        int idx = k == "x" ? 0 : (k == "y" ? 1 : 2);
        if (idx < pa.Count && Present(pa[idx])) return pa[idx].ToObject<int>();
      }
      return 0;
    }

    private static string Ok(object applied) {
      return JsonConvert.SerializeObject(new { ok = true, applied });
    }
    private static string Err(string error, string detail = null, object suggestion = null) {
      return JsonConvert.SerializeObject(new { ok = false, error, detail, suggestion });
    }

  }

}
