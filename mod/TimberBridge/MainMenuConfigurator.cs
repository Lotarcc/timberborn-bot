using Bindito.Core;

namespace TimberBridge {

  // Binds the auto-loader in the main-menu context so it can trigger loading a
  // save from the menu. Separate from BridgeConfigurator, which is Game-context.
  [Context("MainMenu")]
  public class MainMenuConfigurator : Configurator {

    protected override void Configure() {
      Bind<AutoLoader>().AsSingleton();
    }

  }

}
