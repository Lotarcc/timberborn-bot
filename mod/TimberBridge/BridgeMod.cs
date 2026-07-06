using Timberborn.ModManagerScene;
using UnityEngine;

namespace TimberBridge {

  // Mod entry point. Runs once, very early, before any game context exists.
  // The HTTP server itself is a Game-context singleton (see BridgeConfigurator).
  public class BridgeMod : IModStarter {

    public void StartMod(IModEnvironment modEnvironment) {
      Debug.Log("[TimberBridge] mod loaded; HTTP bridge starts when a settlement is loaded.");
    }

  }

}
