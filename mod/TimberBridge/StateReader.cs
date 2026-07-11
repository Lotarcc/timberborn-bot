using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Timberborn.BlockSystem;
using Timberborn.Buildings;
using Timberborn.BuildingsReachability;
using Timberborn.ConstructionSites;
using Timberborn.EntitySystem;
using Timberborn.GameCycleSystem;
using Timberborn.GameDistricts;
using Timberborn.Goods;
using Timberborn.Population;
using Timberborn.ResourceCountingSystem;
using Timberborn.TimeSystem;
using Timberborn.WorkSystem;
using UnityEngine;

namespace TimberBridge {

  // Builds the digested /state snapshot. Every method here runs on the Unity
  // main thread (invoked through MainThreadDispatcher), so touching game
  // services is safe. Serialized with Newtonsoft (bundled with the game) —
  // UnityEngine.JsonUtility silently drops mod-defined nested types.
  // Phase 1: time + resources + population + buildings.
  public class StateReader {

    private readonly GameCycleService _cycle;
    private readonly IDayNightCycle _time;
    private readonly ResourceCountingService _resources;
    private readonly IGoodService _goods;
    private readonly PopulationService _population;
    private readonly EntityComponentRegistry _entities;
    private readonly DistrictCenterRegistry _districts;
    private readonly WeatherReader _weather;
    private readonly ReachabilityReader _reachability;

    public StateReader(GameCycleService cycle,
                       IDayNightCycle time,
                       ResourceCountingService resources,
                       IGoodService goods,
                       PopulationService population,
                       EntityComponentRegistry entities,
                       DistrictCenterRegistry districts,
                       WeatherReader weather,
                       ReachabilityReader reachability) {
      _cycle = cycle;
      _time = time;
      _resources = resources;
      _goods = goods;
      _population = population;
      _entities = entities;
      _districts = districts;
      _weather = weather;
      _reachability = reachability;
    }

    public string ReadStateJson() {
      var dto = new StateDto {
        ok = true,
        time = new TimeDto {
          cycle = _cycle.Cycle,
          day = _cycle.CycleDay,
          hour = _time.HoursPassedToday,
          daytime = _time.IsDaytime
        },
        population = ReadPopulation(),
        resources = ReadResources(),
        buildings = ReadBuildings(),
        district_center = ReadDistrictCenter(),
        weather = ReadWeather()
      };
      dto.alerts = ComputeAlerts(dto);
      return JsonConvert.SerializeObject(dto);
    }

    // ------------------------------------------------------------------
    // Deterministic triage. Every survival rule the agent must not miss is
    // enforced HERE, as data — not left to the LLM prompt. The agent's planner
    // sorts by these; the LLM only chooses how to fix them.
    // ------------------------------------------------------------------
    private List<AlertDto> ComputeAlerts(StateDto s) {
      var alerts = new List<AlertDto>();
      Dictionary<string, int> counts = s.buildings != null ? s.buildings.counts : null;

      bool hasPump = HasBuilding(counts, "WaterPump") || HasBuilding(counts, "DeepWaterPump")
                     || HasBuilding(counts, "LargeWaterPump");
      bool hasLumberjack = HasBuilding(counts, "LumberjackFlag");
      bool hasFood = HasBuilding(counts, "GathererFlag") || HasBuilding(counts, "EfficientFarmHouse")
                     || HasBuilding(counts, "EfficientFarmhouse") || HasBuilding(counts, "Farmhouse")
                     || HasBuilding(counts, "FarmHouse");

      // How many days of supply the colony must hold to be safe: while temperate,
      // the whole next hazard + 2 days of margin; while inside a hazard, what's
      // left of it + 1. Weather read failure falls back to a 5-day cushion.
      float neededDays = 5f;
      if (s.weather != null) {
        neededDays = s.weather.current == "temperate" && s.weather.next != null
            ? (s.weather.next.duration_days > 0 ? s.weather.next.duration_days : 3) + 2f
            : s.weather.current_ends_in_days + 1f;
      }

      GoodDto water = FindGood(s.resources, "Water");
      GoodDto log = FindGood(s.resources, "Log") ?? FindGood(s.resources, "Logs");

      if (!hasLumberjack) {
        alerts.Add(A("no_log_production", "critical",
                     "No LumberjackFlag: nothing can be built without logs.",
                     "Place a LumberjackFlag (FREE) next to wild trees, path-connected."));
      }
      if (!hasPump) {
        alerts.Add(A("no_water_pump", "critical",
                     "No water pump: beavers have no drinking water source.",
                     "Place a WaterPump (12 logs) on land adjacent to clean water."));
      } else if (water != null && water.days_remaining >= 0f && water.days_remaining < neededDays) {
        alerts.Add(A("water_understocked", "critical",
                     "Water covers " + water.days_remaining.ToString("0.0") + "d, need "
                     + neededDays.ToString("0.0") + "d for the coming hazard.",
                     "Add SmallTanks and advance time to fill them BEFORE the hazard."));
      }
      if (!hasFood) {
        alerts.Add(A("no_food_production", "warn",
                     "No food production (gatherer/farm).",
                     "Place a GathererFlag (FREE) near berry bushes; farm on moist soil."));
      }
      if (s.population != null && s.population.homeless > 0) {
        alerts.Add(A("homeless", "warn",
                     s.population.homeless + " beavers homeless (bad sleep; Folktails stop breeding).",
                     "Build Lodges (12 logs, 3 beds) near workplaces."));
      }
      if (log != null && log.stored <= 0 && s.buildings != null && s.buildings.under_construction > 0) {
        alerts.Add(A("logs_zero_sites_waiting", "critical",
                     s.buildings.under_construction + " construction site(s) waiting but 0 logs in stock.",
                     "Ensure LumberjackFlag is working near trees, then advance time (set_speed)."));
      }
      if (s.buildings != null && s.buildings.under_construction > 0
          && (log == null || log.stored > 0)) {
        alerts.Add(A("sites_in_progress", "info",
                     s.buildings.under_construction + " site(s) under construction.",
                     "Advance time (set_speed 3-5) so builders can finish before placing more."));
      }
      if (s.buildings != null && s.buildings.list != null) {
        foreach (BuildingDetailDto b in s.buildings.list) {
          if (!b.reachable) {
            alerts.Add(A("building_unreachable", "critical",
                         b.spec + " at (" + b.x + "," + b.y + "," + b.z + ") is NOT reachable by beavers.",
                         "Connect it with Path back to the district center, or demolish it and rebuild somewhere reachable."));
          }
        }
      }
      return alerts;
    }

    private static AlertDto A(string id, string severity, string message, string suggestion) {
      return new AlertDto { id = id, severity = severity, message = message, suggestion = suggestion };
    }

    // Building ids are faction-suffixed ("WaterPump.Folktails") — match the bare prefix.
    private static bool HasBuilding(Dictionary<string, int> counts, string bareId) {
      if (counts == null) return false;
      foreach (KeyValuePair<string, int> kv in counts) {
        if (kv.Value <= 0) continue;
        string name = kv.Key;
        if (string.Equals(name, bareId, StringComparison.OrdinalIgnoreCase)) return true;
        if (name.StartsWith(bareId + ".", StringComparison.OrdinalIgnoreCase)) return true;
      }
      return false;
    }

    private static GoodDto FindGood(List<GoodDto> goods, string id) {
      if (goods == null) return null;
      foreach (GoodDto g in goods) {
        if (string.Equals(g.good, id, StringComparison.OrdinalIgnoreCase)) return g;
      }
      return null;
    }

    private PopulationDto ReadPopulation() {
      PopulationData p = _population.GlobalPopulationData;
      return new PopulationDto {
        total = p.TotalPopulation,
        adults = p.NumberOfAdults,
        kits = p.NumberOfChildren,
        bots = p.NumberOfBots,
        free_workslots = p.BeaverWorkplaceData.FreeWorkslots,
        unemployed = p.BeaverWorkplaceData.Unemployed,
        free_beds = p.BedData.FreeBeds,
        homeless = p.BedData.Homeless
      };
    }

    private List<GoodDto> ReadResources() {
      var list = new List<GoodDto>();
      int pop = _population.GlobalPopulationData.TotalPopulation;
      foreach (string goodId in _goods.Goods) {
        ResourceCount c = _resources.GetGlobalResourceCount(goodId);
        // Show a good if it's present, has storage, OR is a core survival good
        // (water/food must appear even at 0 — that's the critical signal).
        bool present = c.AllStock != 0 || c.InputOutputCapacity != 0;
        if (!present && !IsCoreGood(goodId)) {
          continue;
        }
        var dto = new GoodDto {
          good = goodId,
          stored = c.AvailableStock,
          all_stock = c.AllStock,
          capacity = c.InputOutputCapacity,
          fill_rate = c.FillRate
        };
        // Days of supply for goods with a known per-beaver daily use (the survival number).
        float perDay = DailyUsePerBeaver(goodId);
        dto.days_remaining = (perDay > 0f && pop > 0) ? c.AvailableStock / (pop * perDay) : -1f;
        list.Add(dto);
      }
      return list;
    }

    // Per-beaver daily consumption (KB). Water is the survival-critical one; food is
    // spread across many goods, so the agent sums food itself using the KB rate 2.67.
    private static float DailyUsePerBeaver(string goodId) {
      return goodId == "Water" ? 2.13f : 0f;
    }

    // Goods that must appear in /state even at zero stock/capacity. Water is
    // survival-critical; Log/Plank are the build materials the agent needs to see
    // to reason about construction (buildings are now real sites that consume them).
    private static bool IsCoreGood(string goodId) {
      switch (goodId) {
        case "Water":
        case "Log":
        case "Logs":
        case "Plank":
        case "Planks":
          return true;
        default:
          return false;
      }
    }

    private BuildingsDto ReadBuildings() {
      var counts = new Dictionary<string, int>();
      var list = new List<BuildingDetailDto>();
      foreach (Building b in _entities.GetEnabled<Building>()) {
        string id;
        try {
          id = b.Spec.Blueprint.Name;
        } catch {
          continue; // skip a building whose spec/blueprint is unavailable
        }
        counts.TryGetValue(id, out int n);
        counts[id] = n + 1;
        BuildingDetailDto detail = ReadBuildingDetail(b, id);
        if (detail != null) list.Add(detail);
      }

      int underConstruction = 0;
      foreach (ConstructionSite _ in _entities.GetEnabled<ConstructionSite>()) {
        underConstruction++;
      }

      return new BuildingsDto { counts = counts, under_construction = underConstruction, list = list };
    }

    // Per-building status the agent needs to see its own mistakes: finished vs
    // site (with material progress + what's still missing), paused, staffing,
    // and REACHABLE — the game's own checks (DistrictBuilding.InstantDistrict for
    // finished buildings, ReachableConstructionSite.IsReachableByBuilders for
    // sites), i.e. the same logic behind the in-game "Unconnected"/"Unreachable"
    // warnings. reachable==false means demolish-or-connect.
    private BuildingDetailDto ReadBuildingDetail(Building b, string specId) {
      try {
        BlockObject block = b.GetComponent<BlockObject>();
        if (block == null) return null;
        Vector3Int c = block.Coordinates;
        var dto = new BuildingDetailDto {
          spec = specId, x = c.x, y = c.y, z = c.z,
          progress = -1f, workers = -1, max_workers = -1,
          // Ground-truth access tile(s): the coordinate(s) the game requires ON the
          // district road for this building to be staffed/reachable (from
          // BuildingAccessible/Accessible). null for buildings without a
          // BuildingAccessible (they connect by footprint, not an access point).
          access = ReadAccessTiles(block),
          access_diag = _reachability.AccessDiag(block)
        };

        bool finished = block.IsFinished;
        var pausable = b.GetComponent<PausableBuilding>();
        bool paused = pausable != null && pausable.Paused;

        if (finished) {
          dto.status = paused ? "paused" : "finished";
          // Game-truth: DistrictBuilding.InstantDistrict for buildings; road-spill
          // membership for paths/walkables (which have no DistrictBuilding). This
          // fixes the old `db == null` shortcut that reported unconnected paths as
          // reachable=true unconditionally.
          dto.reachable = _reachability.IsObjectReachable(block);
        } else {
          dto.status = "site";
          var site = b.GetComponent<ConstructionSite>();
          if (site != null) {
            dto.progress = site.MaterialProgress;
            dto.missing = ReadMissingMaterials(site);
          }
          // Sites: builder-specific reachability is the accurate signal. Fall back to
          // road-spill/DistrictBuilding truth when the site has no ReachableConstructionSite
          // (e.g. path sites), so an unconnected path site isn't a false positive either.
          var rcs = b.GetComponent<ReachableConstructionSite>();
          dto.reachable = rcs != null ? rcs.IsReachableByBuilders() : _reachability.IsObjectReachable(block);
        }

        var wp = b.GetComponent<Workplace>();
        if (wp != null) {
          dto.workers = wp.NumberOfAssignedWorkers;
          dto.max_workers = wp.MaxWorkers;
        }
        return dto;
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] building detail failed for " + specId + ": " + e);
        return null;
      }
    }

    // The access tile(s) the game connects this building through — the coordinate(s)
    // that must sit ON the district road for it to be reachable (see ReachabilityReader
    // .AccessTiles for the confirmed API chain). Returns null for buildings without a
    // BuildingAccessible. Guarded so an access read never takes down the snapshot.
    private List<CoordDto> ReadAccessTiles(BlockObject block) {
      try {
        var tiles = _reachability.AccessTiles(block);
        if (tiles == null || tiles.Count == 0) return null;
        var list = new List<CoordDto>();
        foreach (Vector3Int t in tiles) {
          list.Add(new CoordDto { x = t.x, y = t.y, z = t.z });
        }
        return list;
      } catch {
        return null;
      }
    }

    // Goods a construction site still needs: required amount minus in stock —
    // the same arithmetic the game's FinishNow uses for its top-up.
    private static Dictionary<string, int> ReadMissingMaterials(ConstructionSite site) {
      try {
        var missing = new Dictionary<string, int>();
        foreach (var g in site.Inventory.AllowedGoods) {
          string goodId = g.StorableGood.GoodId;
          int need = g.Amount - site.Inventory.AmountInStock(goodId);
          if (need > 0) missing[goodId] = need;
        }
        return missing.Count > 0 ? missing : null;
      } catch {
        return null;
      }
    }

    // Weather forecast (current phase + next hazard). Wrapped so a weather-read
    // failure never takes down the whole /state snapshot.
    private WeatherReader.WeatherDto ReadWeather() {
      try {
        return _weather.Read();
      } catch (Exception e) {
        Debug.LogError("[TimberBridge] weather read failed: " + e);
        return null;
      }
    }

    // The main district center's coordinate — the anchor for placement until /map lands.
    private CoordDto ReadDistrictCenter() {
      foreach (DistrictCenter dc in _districts.FinishedDistrictCenters) {
        Vector3Int c = dc.CenterCoordinates;
        return new CoordDto { x = c.x, y = c.y, z = c.z };
      }
      return null;
    }

    // --- DTOs (public fields; serialized by Newtonsoft) ---

    private class StateDto {
      public bool ok;
      public TimeDto time;
      public PopulationDto population;
      public List<GoodDto> resources;
      public BuildingsDto buildings;
      public CoordDto district_center;
      public WeatherReader.WeatherDto weather;
      public List<AlertDto> alerts;
    }

    private class AlertDto {
      public string id;
      public string severity; // critical | warn | info
      public string message;
      public string suggestion;
    }

    private class CoordDto {
      public int x;
      public int y;
      public int z;
    }

    private class TimeDto {
      public int cycle;
      public int day;
      public float hour;
      public bool daytime;
    }

    private class PopulationDto {
      public int total;
      public int adults;
      public int kits;
      public int bots;
      public int free_workslots;
      public int unemployed;
      public int free_beds;
      public int homeless;
    }

    private class GoodDto {
      public string good;
      public int stored;
      public int all_stock;
      public int capacity;
      public float fill_rate;
      public float days_remaining; // -1 if not computed (unknown per-beaver use)
    }

    private class BuildingsDto {
      public Dictionary<string, int> counts;
      public int under_construction;
      public List<BuildingDetailDto> list;
    }

    private class BuildingDetailDto {
      public string spec;
      public int x;
      public int y;
      public int z;
      public string status;    // finished | paused | site
      public float progress;   // material progress 0..1 for sites; -1 otherwise
      public Dictionary<string, int> missing; // goods still needed (sites only)
      public int workers;      // -1 when not a workplace
      public int max_workers;
      public bool reachable;   // game-truth: false => unconnected/unreachable
      public List<CoordDto> access; // access tile(s) the game requires on-road; null if none
      public string access_diag; // DIAGNOSTIC: why access is null (temporary)
    }

  }

}
