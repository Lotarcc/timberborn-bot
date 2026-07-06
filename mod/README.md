# TimberBridge mod

In-game C# mod that hosts the localhost HTTP/JSON API the agent uses to observe and act. See [../docs/api-contract.md](../docs/api-contract.md) for the endpoint contract and [../docs/reference/modding-api.md](../docs/reference/modding-api.md) for the framework.

**Phase 0 (current):** `/ping` only — proves the mod loads and the tunnel works. Target: Timberborn v1.0.13.1.

## Layout
| File | Role |
|---|---|
| `manifest.json` | mod metadata (Name, Id, Version, MinimumGameVersion) |
| `TimberBridge.csproj` | netstandard2.1 lib referencing the game's `Managed` assemblies |
| `BridgeMod.cs` | `IModStarter` entry point (logs on load) |
| `BridgeConfigurator.cs` | Bindito `[Context("Game")]` configurator; binds the server |
| `BridgeHttpServer.cs` | `ILoadableSingleton`/`IUnloadableSingleton` HTTP listener on `:7744` |

## Build
Requires .NET SDK. Build where the game's `Managed` folder is reachable (the Windows box); override the path if needed:
```
dotnet build TimberBridge.csproj -c Release
# or: dotnet build TimberBridge.csproj -c Release -p:TimberbornManaged="D:\path\to\Timberborn_Data\Managed"
```
Output: `bin/Release/TimberBridge.dll`.

## Deploy
Copy the manifest and DLL into a mod folder the game scans:
```
<UserProfile>\Documents\Timberborn\Mods\TimberBridge\
  manifest.json
  TimberBridge.dll
```
Launch Timberborn once first to create `Documents\Timberborn`. Enable TimberBridge in the in-game Mod Manager if listed, then load a settlement (the server starts in the `Game` context).

## Test
From another machine, tunnel the port and curl `/ping`:
```
ssh -L 7744:127.0.0.1:7744 <windows-host>
curl -s http://127.0.0.1:7744/ping
# {"ok":true,"bridge_version":"0.1.0","game_version":"1.0.13.1","in_game":true}
```
