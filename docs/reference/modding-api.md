Reference for TimberBridge mod development. Targets Timberborn v1.0.13.1 (Stable).

# Timberborn Modding API Reference (for TimberBridge)

TimberBridge is a C# mod that hosts an in-process HTTP+JSON server inside the running
game to give the player agent full observe/act control. This document distills the
official Mechanistry modding wiki plus community knowledge into the concrete facts
needed to build it. Where the wiki is silent, items are flagged **[NEEDS-DECOMPILE]**
and must be confirmed against the game's `Timberborn.*` assemblies in the install's
`BepInEx`-free `Timberborn_Data/Managed/` folder (via ILSpy/dnSpy).

Confidence legend: **[WIKI]** = stated in official docs; **[COMMUNITY]** = corroborated
by community mods/guides; **[NEEDS-DECOMPILE]** = inferred, must verify.

---

## 1. Mod project layout & manifest

### Two on-disk locations

| Location | Path | Role |
|---|---|---|
| Runtime (local) | `Documents/Timberborn/Mods/<ModName>/` | Where the game loads local mods, mod.io downloads, and Mod-Builder output. **This is where TimberBridge's built output lands.** |
| Runtime (Workshop) | `<steam>/steamapps/workshop/content/1062090/` | Steam Workshop mods. Not used for private dev. |
| Dev (Unity project) | The `timberborn-modding` Unity project on the Windows box | Source. The Mod Builder compiles from here into the runtime location. |

`1062090` is Timberborn's Steam AppID. **[WIKI]**

### Built mod directory contents **[WIKI]**

A loaded mod folder contains some subset of:

- `manifest.json` — **required** metadata (see below).
- `Code.dll` (or any mod DLL) — compiled mod code. TimberBridge's assembly lives here.
- `AssetBundles/` — `.assets` bundles (not needed for a headless server mod).
- `Blueprints/` (also `Recipes/`, `Buildings/`, `Goods/`, `Materials/` etc.) — JSON specs. Not needed for TimberBridge.
- `Localizations/` — CSV (`enUS_myMod.csv`). Not needed.
- `Sprites/` — PNG + `.meta.json`. Not needed.

TimberBridge is effectively **manifest.json + one DLL** (plus, if we use Harmony, a
declared dependency — see §6).

### Version subfolders **[WIKI]**

A mod may contain `version-x` subfolders. If any exist, the game loads the folder
"closest but not higher than the game's current version" and **ignores root content**.
Because we pin to a single Stable build (v1.0.13.1), keep it simple: put everything in
root, set `MinimumGameVersion` accordingly, and re-verify after any game update.

### manifest.json fields **[WIKI]**

Required:

| Field | Meaning |
|---|---|
| `Name` | Display name. |
| `Version` | Mod version string. |
| `Id` | Unique id; recommended `domain/username`-style prefix. |
| `MinimumGameVersion` | Semantic version. Set to `1.0.13.1` (or lower compatible) for our pin. |

Optional:

| Field | Meaning |
|---|---|
| `Description` | Free text. |
| `RequiredMods` | Array of `{ "Id", "MinimumVersion" }`. **Use this to declare Harmony if we patch (§6).** |
| `OptionalMods` | Array of `{ "Id" }`. |

### How the Mod Builder produces a loadable mod **[WIKI]**

The Mod Builder is a Unity Editor tool opened via **`Timberborn → Show Mod Builder`**.
Project layout it expects:

- `manifest.json` at the **mod project root**.
- Scripts in any subdirs, each wrapped in an **assembly definition (`.asmdef`)** file — this is what compiles to the mod DLL.
- `AssetBundles/` subdir for bundle assets (with a `Resources/` subdir for loadable assets).
- `Data/` — files copied directly into the built mod's directory.
- `Root/` — files copied to the **root** of the built mod.

Build modes:

- **Dev build** — skips steps for speed (our default during iteration).
- **Clean build** — release; can also emit a ZIP for mod.io upload.

On success the Unity console prints `Build completed successfully`, and the mod is
written into `Documents/Timberborn/Mods/`. Post-build hooks can open the mods dir or
launch the game. **[WIKI]**

---

## 2. Load & entry point

- On startup the game **scans for all implementations of `IModStarter`**, instantiates each, and calls `StartMod`. **[WIKI]**
- Signature: `void StartMod(IModEnvironment modEnvironment)`. **[WIKI]**
- The `IModStarter` implementation **must have a parameterless constructor**. **[WIKI]** (So no DI into the starter itself — DI happens later via configurators, §3.)
- `IModEnvironment` gives access to the **mod's directory on disk** (use it to locate config files, e.g. the HTTP port/bind config). **[WIKI]**

Minimal entry point **[WIKI]**:

```csharp
public class TimberBridgeModStarter : IModStarter {
  public void StartMod(IModEnvironment modEnvironment) {
    Debug.Log("[TimberBridge] StartMod");
    // If patching: new Harmony("timberbridge").PatchAll();  // see §6
  }
}
```

Note: `StartMod` runs **very early**, before game scenes/DI contexts exist. Do **not**
touch game services here. Register a configurator (§3) and start the HTTP server from a
singleton `Load()` (§4).

---

## 3. Dependency injection (Bindito)

Timberborn's DI framework is **Bindito**. **[WIKI]**

### Writing a configurator **[WIKI]**

Implement `IConfigurator`, annotate with `[Context("<scope>")]`, and bind types in a
`Configure` method (the interface method that receives the container/binder).

Scopes (`[Context(...)]` values):

| Scope | When active |
|---|---|
| `"Bootstrapper"` | Global context (spans everything). |
| `"MainMenu"` | Main menu scene. |
| `"Game"` | **A colony is loaded and playing — this is TimberBridge's primary scope.** |
| `"MapEditor"` | Map editor. |

### Bindings **[WIKI]**

- `AsSingleton()` — instantiated once, persists for the scene's lifetime.
- `AsTransient()` — new instance per dependent.
- `MultiBind<TInterface>().To<TImpl>().AsSingleton()` — collection binding; injected as `IEnumerable<TInterface>`.

Example (illustrative, exact binder API to confirm):

```csharp
[Context("Game")]
public class TimberBridgeConfigurator : IConfigurator {
  public void Configure(IContainerDefinition containerDefinition) {
    containerDefinition.Bind<HttpServerService>().AsSingleton();
    // registering as ILoadableSingleton/IUpdatableSingleton is typically done via
    // MultiBind or a helper; confirm the exact registration API.  [NEEDS-DECOMPILE]
  }
}
```

**[NEEDS-DECOMPILE]**: exact binder type name/method for `Configure` (wiki shows
`Bind<>().AsSingleton()` shape but not the full signature), and the helper used to
register a class as a lifecycle singleton (`ILoadableSingleton`/`IUpdatableSingleton`)
so the game invokes `Load`/`UpdateSingleton`.

### Constructor injection **[WIKI]**

"When a bound object is instantiated, it automatically receives (is injected with) all
dependencies defined in its constructor." So TimberBridge's server singleton simply
declares the game services it needs as constructor parameters, and Bindito supplies
them — **provided those services are themselves bound in the same/parent context.**

---

## 4. Singleton lifecycle (our main-thread hook)

Two lifecycle interfaces matter **[WIKI]**:

| Interface | Method | Semantics |
|---|---|---|
| `ILoadableSingleton` | `Load()` | Called once when the scene loads. **Respects DI dependency order** — runs only after all its dependencies' `Load()` have run. |
| `IUpdatableSingleton` | `UpdateSingleton()` | Called **every frame** on the Unity main thread. |

Also referenced: `IAwakableComponent.Awake` (component-level). **[WIKI]**

**This is the backbone of TimberBridge's threading design:**

- Implement `ILoadableSingleton.Load()` → start the background `HttpListener` thread and initialize the request queue.
- Implement `IUpdatableSingleton.UpdateSingleton()` → drain the queue and execute all game reads/writes here, guaranteeing they run on the main thread.

Because `Load()` honors dependency order, injected game services are guaranteed
initialized before we start serving requests.

---

## 5. Entity / component model (reading world state)

- World objects are **entities**; each is composed of **components**. **[WIKI]**
- Custom components inherit from **`BaseComponent`**. **[WIKI]**
- Lifecycle interfaces on components **[WIKI]**:

| Interface | Method(s) |
|---|---|
| `IInitializableEntity` | `Initialize()` |
| `IFinishedStateListener` | `OnEnterFinishedState()`, `OnExitFinishedState()` (fires when a building finishes construction) |
| `IDeletableEntity` | `Delete()` |
| `IPersistentEntity` | `Save(...)`, `Load(...)` |

- **Specs** are data records: must be a `record`, inherit from `ComponentSpec`, serialized properties use `[Serialize]`, collections use `ImmutableArray`. **Decoration** adds components to an entity during creation based on its Specs. **[WIKI]**

### Enumerating world objects (read path for the digested state)

The wiki does not name the registry used to iterate all entities/components.
**[NEEDS-DECOMPILE]**: the entity registry service (community mods use something like
`EntityComponentRegistry` / `EntityRegistry` with `GetEnabled<TComponent>()`-style
queries). The intended read pattern for TimberBridge:

1. Inject the registry + typed component-collection services.
2. In `UpdateSingleton()`, query components by type (e.g. all `Building`/stock
   components, all beaver/`Character` components) and project into the digested state.

Confirm the exact registry type, its query methods, and which component types expose
the fields we need (see §9).

---

## 6. Harmony

- Harmony is **NOT bundled** with Timberborn. It is a **separate mod dependency** (`eMkaQQ/timberborn-harmony`, "Harmony" on mod.io / Steam Workshop). **[COMMUNITY]**
- It targets **.NET Framework 4.8** (Harmony-Fat package). **[COMMUNITY]** Our mod DLL should target the same runtime the game uses (Mono / .NET Framework 4.8-class). **[NEEDS-DECOMPILE — confirm scripting backend/TFM from ProjectVersion + a game DLL.]**
- To use it, declare Harmony in `manifest.json` `RequiredMods` (and on the upload site). **[COMMUNITY]**
- Apply patches **once**, from `IModStarter.StartMod`, with `new Harmony("<id>").PatchAll();`. Patch classes use `[HarmonyPatch]` + `[HarmonyPrefix]`/`[HarmonyPostfix]`. **[COMMUNITY]**

**Do we need it?** For TimberBridge's **read-only observation and most actions, no.**
DI + component enumeration + calling existing game services covers observe/act. Reach
for Harmony only when we must:

- Intercept a game method that has no public service entry point (e.g. hook a tick/event we can't reach via `IUpdatableSingleton`).
- Suppress or alter built-in UI/input that fights our automation.

Recommendation: **ship v1 without Harmony** to avoid the extra dependency and the
fragility of method-signature patches against a version we pin. Add it surgically if a
required action has no DI-reachable API.

---

## 7. Threading model (CRITICAL)

**Constraint:** Unity and game APIs are **not thread-safe** and must be touched only on
the **main thread**. `System.Net.HttpListener` callbacks run on **background/thread-pool
threads**. Touching entities/components/services from those threads is undefined
behavior (crashes, torn reads, corrupted saves).

### Recommended pattern (producer/consumer across the frame boundary)

```
[HttpListener bg thread]                    [Unity main thread — UpdateSingleton()]
  accept request                              each frame:
  parse HTTP + JSON                             while (queue.TryDequeue(job)):
  build a "job" (delegate + TaskCompletion)       job.result = job.Execute();   // game reads/writes HERE
  enqueue(job)  ---------------------------->      job.completion.SetResult()
  await job.completion (blocks bg thread)     (bg thread wakes, serializes JSON, writes HTTP response)
```

- **Queue:** `ConcurrentQueue<Job>` (or `BlockingCollection`). The bg thread only ever enqueues + waits; it never touches game state.
- **Main thread:** in `UpdateSingleton()`, drain the queue and run each job's game logic. Because this executes inside the game's frame, all reads/writes are safe and see a consistent world.
- **Completion signaling:** each job carries a `TaskCompletionSource`/`ManualResetEventSlim`. Main thread sets it after executing; the bg thread resumes, serializes the result, and sends the HTTP response.
- **This is the standard Unity "main-thread dispatcher" pattern** (cf. PimDeWitte / gustavopsantos `UnityMainThreadDispatcher`), specialized to our `IUpdatableSingleton` hook instead of a `MonoBehaviour.Update`. **[COMMUNITY]**

### Concurrency risks to design against

1. **Deadlock / stall:** if the game pauses or the scene unloads while a bg thread awaits a job, it can hang. Bound every wait with a **timeout**; on scene teardown (`Delete`/main-menu transition) fail all pending jobs.
2. **Work budget:** draining an unbounded queue in one frame stalls the game (hitches, slow ticks). **Cap jobs/frame** or cap wall-time per frame; let excess spill to the next frame.
3. **Writes during simulation:** mutations (place/demolish/set-speed) must happen at a safe point in the frame. Executing inside `UpdateSingleton()` is the safe window, but confirm ordering vs. the game's own tick/save. **[NEEDS-DECOMPILE]**
4. **Lifecycle:** start the listener in `Load()`, and **stop the listener + join the thread** on scene teardown (implement `IDeletableEntity`/an unload hook) so a stale listener doesn't survive into the main menu or a new run.
5. **HttpListener specifics:** on Windows, `HttpListener` may need a URL ACL for non-loopback prefixes. Bind to `http://localhost:<port>/` (loopback) and reach it over the SSH tunnel — loopback avoids ACL/firewall friction.

---

## 8. Debugging

### Log locations **[WIKI]**

| OS | Path |
|---|---|
| Windows | `C:\Users\<user>\AppData\LocalLow\Mechanistry\Timberborn\` |
| macOS | `~/Library/Logs/Mechanistry/Timberborn/` |

(The Unity `Player.log` lives in the Windows `LocalLow\Mechanistry\Timberborn` folder.)

### Logging from a mod **[WIKI]**

Use `Debug.Log` (also `Debug.LogWarning`/`Debug.LogError`) — writes to the in-game
console and the player log. Prefix all TimberBridge lines (e.g. `[TimberBridge]`) for grep.

### In-game dev tools **[WIKI]**

- Developer console: **`Alt + ~`**.
- Dev Mode: **`Shift + Alt + Z`** — dev command panels, entity inspection, time control shortcuts.
- Debug Mode: **`Shift + Alt + X`** — system diagnostic panels + object debugger (inspect entity field values). **Extremely useful for discovering which components/fields hold the state we need to expose.**
- Blueprint Viewer: in the entity panel when dev mode is on.

### Experimental modding API — stability implications

The modding project pins Unity to the version in
`ProjectSettings/ProjectVersion.txt`, and requires the `-disable-assembly-updater`
Unity launch arg. **[WIKI]** The modding API historically tracks the game's
**Experimental branch** and internal (`Timberborn.*`) types are **not a stable contract** —
private fields, method signatures, and service shapes can change between builds.

**Implication for pinning to v1.0.13.1 Stable:** build and validate everything against
the exact installed build. **Any game update can break** component field access,
service signatures, and especially Harmony patches. Treat the digested-state extraction
and any Harmony hooks as the most fragile surface, and add a startup self-check that
logs loudly if an expected service/type is missing.

---

## 9. Services we'll likely need (capability → assembly map)

Confirmed-present assemblies from the install's `Managed/` folder are noted; the
**service/type names inside them are [NEEDS-DECOMPILE]** unless a wiki reference exists.
The wiki names only a few concrete services (e.g. UI: `UILayout`, `PanelStack`,
`DialogBoxShower`); everything game-state related must be confirmed by decompilation.

| Capability (for digested state / actions) | Likely assembly | Likely service/type | Confidence |
|---|---|---|---|
| Read map / terrain grid (heights, coords) | `Timberborn.BlockSystem` | `BlockService` / terrain map service | **[NEEDS-DECOMPILE]** |
| Enumerate world objects / block objects | `Timberborn.BlockSystem`, `Timberborn.BlockObjectTools` | entity/block-object registry | **[NEEDS-DECOMPILE]** |
| Read water depth / contamination | water assembly (`Timberborn.WaterSystem` / `WaterContamination*`) | water-map / contamination service | **[NEEDS-DECOMPILE]** (assembly family present) |
| Read weather / drought & badtide forecast | weather assembly (`Timberborn.Weather*` / `Timberborn.GameCycleSystem`) | weather / hazardous-weather service | **[NEEDS-DECOMPILE]** |
| Cycle (wet/dry period) & day counter | `Timberborn.GameCycleSystem` | game-cycle / day-tracker service | **[NEEDS-DECOMPILE]** (assembly named) |
| Enumerate beavers & their needs | `Timberborn.Beavers`, plus needs assembly (`Timberborn.Needs*`) | character/beaver components + needs manager | **[NEEDS-DECOMPILE]** (Beavers assembly present) |
| Beaver movement / pathing state | `Timberborn.CharacterMovementSystem` | movement/pathfinding components | **[NEEDS-DECOMPILE]** (assembly present) |
| Enumerate buildings & their state | `Timberborn.Buildings` | `Building` component + building registry | **[NEEDS-DECOMPILE]** (assembly present) |
| Read stocks / inventories / goods | `Timberborn.GameGoods` | goods/inventory service | **[NEEDS-DECOMPILE]** (assembly present) |
| Districts (population, distribution) | `Timberborn.GameDistricts` | district center / district service | **[NEEDS-DECOMPILE]** (assembly present) |
| Place / demolish buildings | `Timberborn.BuildingTools`, `Timberborn.BlockObjectTools` | placement/tool + block-object placer/deleter | **[NEEDS-DECOMPILE]** (assemblies present) |
| Area tools: cut-trees / plant / clear | `Timberborn.BlockObjectTools` (+ forestry assembly) | area-tool / forester tool services | **[NEEDS-DECOMPILE]** |
| Set building/work priorities | `Timberborn.Buildings` (+ workplaces assembly) | priority / workplace service | **[NEEDS-DECOMPILE]** |
| Control game speed / pause | speed assembly (`Timberborn.GameScene*` / `Timberborn.TimeSystem`) | speed-manager / time service | **[NEEDS-DECOMPILE]** |
| Save / load | save-system assembly (`Timberborn.GameSaveRepositorySystem` / `SaveSystem`) | game-saver / save-repository service | **[NEEDS-DECOMPILE]** |
| Enumerate entities generically | core scene assembly | entity/component registry (`EntityComponentRegistry`?) | **[NEEDS-DECOMPILE]** (see §5) |
| UI (only if we surface status in-game) | UI assembly | `UILayout`, `PanelStack`, `DialogBoxShower` | **[WIKI]** |

### MUST CONFIRM VIA DECOMPILATION (consolidated)

1. Bindito binder API: exact `Configure` signature/binder type, and the registration call that makes a class an `ILoadableSingleton` **and** `IUpdatableSingleton` the game will drive.
2. The generic entity/component registry type + its query methods (§5) — the backbone of the read path.
3. Water depth/contamination service + its map API and coordinate system.
4. Weather/badtide (hazardous weather) forecast service — field names for "days until next drought/badtide."
5. Game-cycle service: current cycle phase, day index, temperature — for the "cycle" concept.
6. Beaver enumeration + needs model (which component holds hunger/thirst/wellbeing; how to read population).
7. Building enumeration + inventory/stock component (goods amounts per building and global).
8. District service (population, worker distribution, migration).
9. Placement/demolish API (what a tool call needs: prefab/spec id, coordinates, orientation) and whether it must run through a "tool" or a lower-level placer.
10. Area tools (cut/plant/clear) invocation surface.
11. Priority/workplace-assignment API.
12. Game speed / pause service (set speed 0..N, toggle pause).
13. Save/load service (trigger a named save; load a save) and whether load forces a scene reload (which tears down our singleton — reconnect logic needed).
14. Target framework / scripting backend of the game assemblies, to match our DLL's TFM (§6).

---

## Sources

Official wiki (read):

- https://github.com/mechanistry/timberborn-modding/wiki/Quick-start
- https://github.com/mechanistry/timberborn-modding/wiki/Coding-basics
- https://github.com/mechanistry/timberborn-modding/wiki/Timberborn-architecture
- https://github.com/mechanistry/timberborn-modding/wiki/Mod-directory-structure
- https://github.com/mechanistry/timberborn-modding/wiki/Mod-Builder
- https://github.com/mechanistry/timberborn-modding/wiki/Mod-templates
- https://github.com/mechanistry/timberborn-modding/wiki/Mod-management
- https://github.com/mechanistry/timberborn-modding/wiki/Debugging
- https://github.com/mechanistry/timberborn-modding/wiki/Unity-setup
- https://github.com/mechanistry/timberborn-modding/wiki/User-interface
- https://github.com/mechanistry/timberborn-modding (README)

Community:

- https://github.com/eMkaQQ/timberborn-harmony (Harmony wrapper mod, .NET 4.8, required-mod pattern)
- https://datvm.github.io/TimberbornMods/ModdingGuide/mod-settings-and-harmony.html (Harmony in `IModStarter.StartMod`, `PatchAll`)
- https://github.com/PimDeWitte/UnityMainThreadDispatcher and https://github.com/gustavopsantos/UnityMainThreadDispatcher (main-thread dispatcher pattern)
- https://discussions.unity.com/t/httplistener-in-its-own-thread/819682 (HttpListener on its own thread in Unity)
