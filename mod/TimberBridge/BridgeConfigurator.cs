using Bindito.Core;

namespace TimberBridge {

  // Registers the HTTP server in the "Game" context, so it starts when a
  // settlement loads (where the game-state services we will expose live).
  // Binding it as a singleton is enough for the engine to call its
  // ILoadableSingleton.Load() / IUnloadableSingleton.Unload().
  [Context("Game")]
  public class BridgeConfigurator : Configurator {

    protected override void Configure() {
      Bind<MainThreadDispatcher>().AsSingleton();
      Bind<StateReader>().AsSingleton();
      Bind<Actuator>().AsSingleton();
      Bind<BlueprintsReader>().AsSingleton();
      Bind<MapReader>().AsSingleton();
      Bind<BridgeHttpServer>().AsSingleton();
    }

  }

}
