using System.Collections.Generic;
using Newtonsoft.Json;
using Timberborn.BlockSystem;
using Timberborn.Buildings;
using Timberborn.TemplateSystem;

namespace TimberBridge {

  // Dumps every placeable spec id (Blueprint.Name) so the agent/KB use the real
  // ids and place_building's FindSpec matches. Not part of the play loop — a
  // one-shot reference. Runs on the main thread via the dispatcher.
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
        list.Add(new { id, building = spec.HasSpec<BuildingSpec>() });
      }
      return JsonConvert.SerializeObject(new { ok = true, count = list.Count, specs = list });
    }

  }

}
