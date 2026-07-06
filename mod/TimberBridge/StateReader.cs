using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Timberborn.Buildings;
using Timberborn.ConstructionSites;
using Timberborn.EntitySystem;
using Timberborn.GameCycleSystem;
using Timberborn.GameDistricts;
using Timberborn.Goods;
using Timberborn.Population;
using Timberborn.ResourceCountingSystem;
using Timberborn.TimeSystem;
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

    public StateReader(GameCycleService cycle,
                       IDayNightCycle time,
                       ResourceCountingService resources,
                       IGoodService goods,
                       PopulationService population,
                       EntityComponentRegistry entities,
                       DistrictCenterRegistry districts) {
      _cycle = cycle;
      _time = time;
      _resources = resources;
      _goods = goods;
      _population = population;
      _entities = entities;
      _districts = districts;
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
        district_center = ReadDistrictCenter()
      };
      return JsonConvert.SerializeObject(dto);
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
      foreach (string goodId in _goods.Goods) {
        ResourceCount c = _resources.GetGlobalResourceCount(goodId);
        // Show a good if it's present, has storage, OR is a core survival good
        // (water/food must appear even at 0 — that's the critical signal).
        bool present = c.AllStock != 0 || c.InputOutputCapacity != 0;
        if (!present && !IsCoreGood(goodId)) {
          continue;
        }
        list.Add(new GoodDto {
          good = goodId,
          stored = c.AvailableStock,
          all_stock = c.AllStock,
          capacity = c.InputOutputCapacity,
          fill_rate = c.FillRate
        });
      }
      return list;
    }

    // Goods that must appear in /state even at zero stock/capacity (survival-critical).
    // Water can be 0 with no tank yet, which is exactly when the agent must see it.
    private static bool IsCoreGood(string goodId) {
      return goodId == "Water";
    }

    private BuildingsDto ReadBuildings() {
      var counts = new Dictionary<string, int>();
      foreach (Building b in _entities.GetEnabled<Building>()) {
        string id;
        try {
          id = b.Spec.Blueprint.Name;
        } catch {
          continue; // skip a building whose spec/blueprint is unavailable
        }
        counts.TryGetValue(id, out int n);
        counts[id] = n + 1;
      }

      int underConstruction = 0;
      foreach (ConstructionSite _ in _entities.GetEnabled<ConstructionSite>()) {
        underConstruction++;
      }

      return new BuildingsDto { counts = counts, under_construction = underConstruction };
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
    }

    private class BuildingsDto {
      public Dictionary<string, int> counts;
      public int under_construction;
    }

  }

}
