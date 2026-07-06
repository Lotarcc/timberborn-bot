using System;
using System.Collections.Generic;
using Newtonsoft.Json;
using Timberborn.BlockSystem;                    // BlockObject.Coordinates          (CONFIRMED)
using Timberborn.Cutting;                        // Cuttable                          (CONFIRMED)
using Timberborn.EntitySystem;                   // EntityComponentRegistry           (CONFIRMED)
using Timberborn.Gathering;                      // Gatherable                        (CONFIRMED)
using Timberborn.Growing;                        // Growable.IsGrown                  (CONFIRMED)
using Timberborn.NaturalResourcesModelSystem;    // NaturalResourceModel (registered) (CONFIRMED)
using Timberborn.TemplateSystem;                 // TemplateSpec.TemplateName         (CONFIRMED)
using UnityEngine;                               // Vector3Int, Debug

namespace TimberBridge {

  // Exposes the map's WILD natural resources so the agent stops placing
  // Lumberjack/Forester/Gatherer flags blind. Every method runs on the Unity
  // main thread (invoked through MainThreadDispatcher), so touching game
  // components is safe. Serialized with Newtonsoft (bundled) like Actuator.
  //
  // ENUMERATION (CONFIRMED): NaturalResourceModel is the single IRegisteredComponent
  // present on every natural-resource entity (Timberborn.NaturalResourcesModelSystem).
  // It is the only registered component in that graph — Cuttable/Gatherable/Yielder/
  // Growable are plain BaseComponents, so they are NOT directly enumerable via
  // GetEnabled<T>(). We iterate GetEnabled<NaturalResourceModel>() and read the
  // sibling components off each entity.
  //
  // CLASSIFICATION: an entity with a Cuttable is a TREE (lumberjack/forester target);
  // an entity with a Gatherable is a GATHERABLE (gatherer target). A resource may in
  // principle carry both — we emit it into "trees" if Cuttable is present, else into
  // "gatherables". Good id comes from the component's own spec Yielder.Yield.Id
  // (Cuttable.YielderSpec.Yield.Id -> typically "Log"; Gatherable.YielderSpec.Yield.Id
  // -> "Berries"/etc).
  //
  // MATURITY:
  //   tree.mature      = Growable.IsGrown (CONFIRMED: a Lumberjack only fells grown
  //                      trees; Growable.IsGrown == _timeTrigger.Finished). If there is
  //                      no Growable (non-growing resource) it is treated as mature.
  //   gatherable.ready = Gatherable.Yielder.IsYielding (CONFIRMED: yield present to
  //                      harvest; IsYielding == Yield.Amount > 0). AND, if the plant has
  //                      a Growable, it must also be grown.
  public class ResourcesReader {

    // Cap per list. If exceeded we still report full counts and set "truncated":true.
    private const int MaxEntries = 400;

    private readonly EntityComponentRegistry _entities;   // CONFIRMED: Bind<EntityComponentRegistry> exists (StateReader injects it)

    public ResourcesReader(EntityComponentRegistry entities) {
      _entities = entities;
    }

    public string ReadJson() {
      var counts = new Dictionary<string, int>();
      var trees = new List<ResourceDto>();
      var gatherables = new List<ResourceDto>();
      bool treesTruncated = false;
      bool gatherablesTruncated = false;

      foreach (NaturalResourceModel model in _entities.GetEnabled<NaturalResourceModel>()) {
        try {
          var cuttable = model.GetComponent<Cuttable>();
          var gatherable = model.GetComponent<Gatherable>();
          if (cuttable == null && gatherable == null) {
            continue; // a natural resource that is neither cut nor gathered (e.g. decorative)
          }

          var block = model.GetComponent<BlockObject>();
          if (block == null) continue;
          Vector3Int c = block.Coordinates;

          string species = ReadSpecies(model);
          var growable = model.GetComponent<Growable>();
          bool grown = growable == null || growable.IsGrown; // no Growable => treat as mature

          if (cuttable != null) {
            string good = SafeGood(cuttable.YielderSpec);
            Bump(counts, good ?? species ?? "unknown");
            if (trees.Count < MaxEntries) {
              trees.Add(new ResourceDto {
                x = c.x, y = c.y, z = c.z,
                species = species, good = good,
                mature = grown
              });
            } else {
              treesTruncated = true;
            }
          } else {
            // gatherable != null
            string good = SafeGood(gatherable.YielderSpec);
            Bump(counts, good ?? species ?? "unknown");
            // Ready = yield currently present AND (if it grows) fully grown.
            bool ready = grown && GatherableIsYielding(gatherable);
            if (gatherables.Count < MaxEntries) {
              gatherables.Add(new ResourceDto {
                x = c.x, y = c.y, z = c.z,
                species = species, good = good,
                ready = ready
              });
            } else {
              gatherablesTruncated = true;
            }
          }
        } catch (Exception e) {
          // One bad entity must never kill the whole list.
          Debug.LogError("[TimberBridge] resource read failed: " + e);
        }
      }

      var dto = new ResourcesDto {
        ok = true,
        counts = counts,
        trees = trees,
        gatherables = gatherables,
        truncated = treesTruncated || gatherablesTruncated
      };
      return JsonConvert.SerializeObject(dto);
    }

    // CONFIRMED: Gatherable.Yielder : Yielder; Yielder.IsYielding => Yield.Amount > 0.
    private static bool GatherableIsYielding(Gatherable gatherable) {
      try {
        return gatherable.Yielder != null && gatherable.Yielder.IsYielding;
      } catch {
        return false;
      }
    }

    // CONFIRMED: CuttableSpec/GatherableSpec.Yielder : YielderSpec; YielderSpec.Yield : GoodAmountSpec; .Id : string.
    private static string SafeGood(Timberborn.Yielding.YielderSpec spec) {
      try {
        return spec != null && spec.Yield != null ? spec.Yield.Id : null;
      } catch {
        return null;
      }
    }

    // CONFIRMED: TemplateSpec.TemplateName is the clean species/prefab name (e.g. "Pine",
    // "BlueberryBush"). Fall back to the GameObject Name if the spec is missing.
    private static string ReadSpecies(NaturalResourceModel model) {
      try {
        var template = model.GetComponent<TemplateSpec>();
        if (template != null && !string.IsNullOrEmpty(template.TemplateName)) {
          return template.TemplateName;
        }
        return model.Name;
      } catch {
        return null;
      }
    }

    private static void Bump(Dictionary<string, int> counts, string key) {
      counts.TryGetValue(key, out int n);
      counts[key] = n + 1;
    }

    // --- DTOs (public fields; serialized by Newtonsoft) ---

    private class ResourcesDto {
      public bool ok;
      public Dictionary<string, int> counts;
      public List<ResourceDto> trees;
      public List<ResourceDto> gatherables;
      public bool truncated;
    }

    private class ResourceDto {
      public int x;
      public int y;
      public int z;
      public string species;
      public string good;
      // Only one of these is meaningful per bucket; Newtonsoft emits both, but the
      // consumer reads "mature" for trees and "ready" for gatherables. Defaults are
      // fine (false) for the unused field.
      public bool mature;
      public bool ready;
    }

  }

}
