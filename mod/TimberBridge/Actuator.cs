using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Timberborn.BlockSystem;
using Timberborn.Buildings;
using Timberborn.ConstructionSites;
using Timberborn.Coordinates;
using Timberborn.EntitySystem;
using Timberborn.GameDistricts;
using Timberborn.GameSaveRepositorySystem;
using Timberborn.GameSaveRuntimeSystem;
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

    public Actuator(SpeedManager speed,
                    TemplateService templates,
                    BlockObjectFactory blockFactory,
                    BlockValidator validator,
                    IBlockService blocks,
                    ConstructionFactory construction,
                    EntityService entities,
                    GameSaver saver,
                    GameLoader loader,
                    DistrictCenterRegistry districts) {
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
    }

    public string Act(string command, JObject args) {
      try {
        switch (command) {
          case "set_speed": return SetSpeed(GetFloat(args, "speed", 1f));
          case "pause": return SetSpeed(0f);
          case "place_building":
            return PlaceBuilding(GetStr(args, "spec") ?? GetStr(args, "spec_id"), GetCoord(args, "x"),
                                 GetCoord(args, "y"), GetCoord(args, "z"), GetStr(args, "orientation"),
                                 GetBool(args, "instant", true));
          case "demolish": return Demolish(GetInt(args, "x"), GetInt(args, "y"), GetInt(args, "z"));
          case "save": return Save(GetStr(args, "name"));
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

    private string PlaceBuilding(string specId, int x, int y, int z, string orientation, bool instant) {
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
      return Ok(new { command = "place_building", spec = specId, x, y, z, orientation = o.ToString(), mode });
    }

    private string Demolish(int x, int y, int z) {
      var coord = new Vector3Int(x, y, z);
      if (!_blocks.AnyObjectAt(coord)) return Err("nothing_there", "(" + x + "," + y + "," + z + ")");
      BlockObject obj = _blocks.GetBottomObjectAt(coord);
      if (obj == null || !obj.CanDelete()) return Err("not_deletable", "(" + x + "," + y + "," + z + ")");
      _entities.Delete(obj);
      return Ok(new { command = "demolish", x, y, z });
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

    // Coordinate from flat args[k] or nested args.position[k] (models nest either way).
    private static int GetCoord(JObject a, string k) {
      JToken t = a?[k];
      if (!Present(t)) { JObject pos = a?["position"] as JObject; t = pos?[k]; }
      return Present(t) ? t.ToObject<int>() : 0;
    }

    private static string Ok(object applied) {
      return JsonConvert.SerializeObject(new { ok = true, applied });
    }
    private static string Err(string error, string detail = null, object suggestion = null) {
      return JsonConvert.SerializeObject(new { ok = false, error, detail, suggestion });
    }

  }

}
