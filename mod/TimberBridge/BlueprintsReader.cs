using System.Collections.Generic;
using System.Collections.Immutable;
using Newtonsoft.Json;
using Timberborn.BlockSystem;
using Timberborn.Buildings;
using Timberborn.TemplateSystem;

namespace TimberBridge {

  // Dumps every placeable spec id (Blueprint.Name) plus its 3-D footprint and vertical
  // STACKING capability, so the agent can plan verticality (platforms, build-on-top),
  // not just flat-ground placement. Stacking in Timberborn is per-block blueprint data:
  //   MatterBelow  = what must be in the cell directly below a block (Ground / Stackable /
  //                  GroundOrStackable / Air / Any) -> tells us if the building can sit ON a
  //                  stackable surface (a platform top, another stackable building) or only on ground.
  //   Stackable    = whether something can be built ON TOP of a block (None / BlockObject /
  //                  UnfinishedGround) -> tells us if the building itself is a buildable surface.
  // (Confirmed against the decompiled BlockObjectSpec/BlockSpec + MatterBelowValidator.)
  // Not part of the play loop — a reference dump. Runs on the main thread via the dispatcher.
  public class BlueprintsReader {

    private readonly TemplateService _templates;

    public BlueprintsReader(TemplateService templates) {
      _templates = templates;
    }

    public string ReadJson() {
      var list = new List<object>();
      var seen = new HashSet<string>();
      foreach (BlockObjectSpec spec in _templates.GetAll<BlockObjectSpec>()) {
        string id = spec.Blueprint != null ? spec.Blueprint.Name : null;
        if (string.IsNullOrEmpty(id) || !seen.Add(id)) {
          continue;
        }
        list.Add(Describe(id, spec));
      }
      return JsonConvert.SerializeObject(new { ok = true, count = list.Count, specs = list });
    }

    // size{x,y,z} + stacking summary. `stackable` = something can be built on top of this
    // building (it is a valid support surface, i.e. a platform/roof). `base_matter` = the
    // requirement of its BASE (z=0) cells: "ground" (must sit on terrain), "stackable" (must
    // sit on a stackable surface), "ground_or_stackable" (either), "any"/"air" (unconstrained).
    // From those the agent derives can_stack_on = base allows a stackable surface below.
    private static object Describe(string id, BlockObjectSpec spec) {
      var size = spec.Size;                    // Vector3Int (x, y = horizontal; z = vertical)
      ImmutableArray<BlockSpec> blocks;
      try { blocks = spec.Blocks; } catch { blocks = default; }

      bool stackableTop = false;               // any block: Stackable != None
      // Collect the MatterBelow requirement across BASE-level (z==0) cells.
      var baseMatter = new HashSet<string>();
      int area = (size.x > 0 && size.y > 0) ? size.x * size.y : 0;

      if (!blocks.IsDefaultOrEmpty && area > 0) {
        for (int i = 0; i < blocks.Length; i++) {
          BlockSpec bs = blocks[i];
          if (bs.Stackable != BlockStackable.None) {
            stackableTop = true;
          }
          int z = i / area;                    // index layout: (z*Size.y + y)*Size.x + x
          if (z == 0) {
            baseMatter.Add(bs.MatterBelow.ToString());
          }
        }
      }

      // Reduce the base requirement to a single, agent-friendly token.
      string baseReq;
      if (baseMatter.Contains("Ground") && baseMatter.Contains("Stackable")) baseReq = "ground_or_stackable";
      else if (baseMatter.Contains("GroundOrStackable")) baseReq = "ground_or_stackable";
      else if (baseMatter.Contains("Ground")) baseReq = "ground";
      else if (baseMatter.Contains("Stackable")) baseReq = "stackable";
      else if (baseMatter.Contains("Air")) baseReq = "air";
      else if (baseMatter.Count > 0) baseReq = "any";
      else baseReq = "unknown";

      bool canStackOn = baseReq == "stackable" || baseReq == "ground_or_stackable"
                        || baseReq == "any" || baseReq == "air";

      return new {
        id,
        building = spec.HasSpec<BuildingSpec>(),
        size = new { x = size.x, y = size.y, z = size.z },
        stackable = stackableTop,   // can something be built ON TOP of this
        can_stack_on = canStackOn,  // can this be placed on a stackable surface (not only ground)
        base_matter = baseReq,
      };
    }

  }

}
