using System;
using System.Collections.Generic;
using Timberborn.GameCycleSystem;
using Timberborn.Goods;
using Timberborn.Population;
using Timberborn.ResourceCountingSystem;
using Timberborn.TimeSystem;
using UnityEngine;

namespace TimberBridge {

  // Builds the digested /state snapshot. Every method here runs on the Unity
  // main thread (invoked through MainThreadDispatcher), so touching game
  // services is safe. Phase 1 in progress: time + resources + population
  // confirmed; buildings / weather / alerts layered in next.
  public class StateReader {

    private readonly GameCycleService _cycle;
    private readonly IDayNightCycle _time;
    private readonly ResourceCountingService _resources;
    private readonly IGoodService _goods;
    private readonly PopulationService _population;

    public StateReader(GameCycleService cycle,
                       IDayNightCycle time,
                       ResourceCountingService resources,
                       IGoodService goods,
                       PopulationService population) {
      _cycle = cycle;
      _time = time;
      _resources = resources;
      _goods = goods;
      _population = population;
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
        resources = ReadResources()
      };
      return JsonUtility.ToJson(dto);
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

    private GoodDto[] ReadResources() {
      var list = new List<GoodDto>();
      foreach (string goodId in _goods.Goods) {
        ResourceCount c = _resources.GetGlobalResourceCount(goodId);
        if (c.AllStock == 0 && c.InputOutputCapacity == 0) {
          continue; // good not present in the settlement
        }
        list.Add(new GoodDto {
          good = goodId,
          stored = c.AvailableStock,
          all_stock = c.AllStock,
          capacity = c.InputOutputCapacity,
          fill_rate = c.FillRate
        });
      }
      return list.ToArray();
    }

    // --- DTOs (JsonUtility-serializable: public fields, no dictionaries) ---

    [Serializable]
    public class StateDto {
      public bool ok;
      public TimeDto time;
      public PopulationDto population;
      public GoodDto[] resources;
    }

    [Serializable]
    public class TimeDto {
      public int cycle;
      public int day;
      public float hour;
      public bool daytime;
    }

    [Serializable]
    public class PopulationDto {
      public int total;
      public int adults;
      public int kits;
      public int bots;
      public int free_workslots;
      public int unemployed;
      public int free_beds;
      public int homeless;
    }

    [Serializable]
    public class GoodDto {
      public string good;
      public int stored;
      public int all_stock;
      public int capacity;
      public float fill_rate;
    }

  }

}
