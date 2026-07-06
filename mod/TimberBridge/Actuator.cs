using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Timberborn.BlockSystem;
using Timberborn.BuilderPrioritySystem;
using Timberborn.Buildings;
using Timberborn.ConstructionSites;
using Timberborn.Coordinates;
using Timberborn.EntitySystem;
using Timberborn.GameDistricts;
using Timberborn.GameSaveRepositorySystem;
using Timberborn.GameSaveRuntimeSystem;
using Timberborn.PrioritySystem;
using Timberborn.TemplateSystem;
using Timberborn.TimeSystem;
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
                    ReachabilityReader reachability) {
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

      if (!_validator.BlocksValid(spec, placement)) {
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

      string mode;
      if (spec.HasSpec<BuildingSpec>()) {
        BuildingSpec buildingSpec = spec.GetSpec<BuildingSpec>();
        if (instant) { _construction.CreateAsFinished(buildingSpec, placement); mode = "finished"; }
        else { _construction.CreateAsUnfinished(buildingSpec, placement); mode = "construction_site"; }
      } else {
        if (instant) { _blockFactory.CreateFinished(spec, placement); mode = "finished"; }
        else { _blockFactory.CreateUnfinished(spec, placement); mode = "construction_site"; }
      }

      // A finished building is only staffed/reachable if it touches the district ROAD
      // network. Lay a Path from the building back to that network. Never fail the
      // placement if this can't connect (report reason so the agent can bridge/dam).
      // Only meaningful for real buildings; a placed Path is its own connection.
      object autoConnectResult = null;
      if (autoConnect && spec.HasSpec<BuildingSpec>()) {
        autoConnectResult = AutoConnect(coord, spec);
      }

      return Ok(new { command = "place_building", spec = specId, x, y, z,
                      orientation = o.ToString(), mode, auto_connect = autoConnectResult });
    }

    // Number of Path construction sites AutoConnect will ever lay in one call. Hard cap
    // so a runaway BFS (e.g. a very long route hugging a lake) can't carpet the map.
    private const int MaxPathTiles = 20;
    // BFS exploration cap: how many tiles we pop before giving up looking for a road.
    private const int MaxBfsExpansion = 60;

    // Lay a contiguous Path from a freshly placed building back to the district-road
    // network so the FINISHED building is connected (staffed/reachable). Runs on the
    // main thread (Act does). Wrapped end-to-end in try/catch: a paving failure must
    // NEVER fail the building placement or throw out of Act.
    //
    // Returns an anonymous object folded into place_building's JSON under "auto_connect":
    //   { connected:bool, paths_laid:int, path_tiles:[{x,y,z}], reason?:string }
    private object AutoConnect(Vector3Int buildingCoord, BlockObjectSpec buildingSpec) {
      try {
        BlockObjectSpec pathSpec = FindSpec("Path");
        if (pathSpec == null) {
          return new { connected = false, paths_laid = 0, reason = "no_path_spec" };
        }

        // (a) Candidate start tiles: the orthogonal ring around the building footprint,
        // at the building's z. If ANY is already on the district road, we're connected.
        var startTiles = FootprintAdjacentTiles(buildingCoord, buildingSpec);
        foreach (Vector3Int t in startTiles) {
          if (_reachability.IsTileOnDistrictRoad(t)) {
            return new { connected = true, paths_laid = 0, path_tiles = new object[0] };
          }
        }

        // (b) BFS outward from the start tiles (4-neighbour, same z) for the nearest
        // tile already on the district road. Track parents to rebuild the route.
        var parent = new Dictionary<Vector3Int, Vector3Int>();
        var visited = new HashSet<Vector3Int>();
        var queue = new Queue<Vector3Int>();
        Vector3Int roadTile = default(Vector3Int);
        bool foundRoad = false;

        foreach (Vector3Int t in startTiles) {
          if (visited.Add(t)) queue.Enqueue(t);
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

          foreach (Vector3Int n in Orthogonal(cur)) {
            if (!visited.Add(n)) continue;
            // Walk a neighbour if we can route through it: it's already on the road,
            // it already carries a path (traverse it, we just won't re-lay), or a fresh
            // Path would validly place there.
            if (!_reachability.IsTileOnDistrictRoad(n) && !HasPathAt(n) && !CanPave(pathSpec, n)) continue;
            parent[n] = cur;
            queue.Enqueue(n);
          }
        }

        if (!foundRoad) {
          // No land route to any district road within the cap (e.g. across a lake).
          // Do NOT fail the building — just report so the agent knows to build a bridge/dam.
          return new { connected = false, paths_laid = 0, reason = "no_land_route" };
        }

        // (c) Rebuild the route road<-...<-start, then lay Path on every tile from the
        // building side UP TO (but not including) the road tile.
        var route = new List<Vector3Int>();
        Vector3Int walk = roadTile;
        while (parent.TryGetValue(walk, out Vector3Int prev)) {
          route.Add(walk);   // walk is a non-road tile here (roadTile has no parent unless it was a start tile)
          walk = prev;
        }
        route.Add(walk); // the start tile
        // route is road-adjacent .. start; drop the road tile itself if it slipped in.
        route.RemoveAll(tile => _reachability.IsTileOnDistrictRoad(tile));

        var laid = new List<object>();
        foreach (Vector3Int tile in route) {
          if (laid.Count >= MaxPathTiles) break;
          if (HasPathAt(tile)) continue;                 // already a path here
          if (!CanPave(pathSpec, tile)) continue;        // re-validate before creating
          _blockFactory.CreateUnfinished(pathSpec, new Placement(tile, Orientation.Cw0, FlipMode.Unflipped));
          laid.Add(new { x = tile.x, y = tile.y, z = tile.z });
        }

        return new { connected = true, paths_laid = laid.Count, path_tiles = laid };
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] auto_connect failed: " + e);
        return new { connected = false, paths_laid = 0, reason = "exception" };
      }
    }

    // Orthogonal ring of tiles around a building's footprint at the building's z. The
    // footprint is approximated from the spec's unrotated block size (buildingCoord is
    // the origin corner). Orientation is ignored — this only needs to be a reasonable
    // set of seed tiles for the BFS; each actual Path placement is validated separately.
    // On any uncertainty we fall back to the 4 orthogonal neighbours of buildingCoord.
    private List<Vector3Int> FootprintAdjacentTiles(Vector3Int buildingCoord, BlockObjectSpec spec) {
      var result = new List<Vector3Int>();
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
      // Orthogonal neighbours of the footprint that are not themselves in the footprint.
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
      var coord = new Vector3Int(x, y, z);
      if (!_blocks.AnyObjectAt(coord)) return Err("nothing_there", "(" + x + "," + y + "," + z + ")");
      BlockObject obj = _blocks.GetBottomObjectAt(coord);
      if (obj == null || !obj.CanDelete()) return Err("not_deletable", "(" + x + "," + y + "," + z + ")");
      _entities.Delete(obj);
      return Ok(new { command = "demolish", x, y, z });
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
