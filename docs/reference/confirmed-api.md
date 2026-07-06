# Confirmed game API surface — Timberborn v1.0.13.1

Reflected from the installed assemblies at `F:\SteamLibrary\steamapps\common\Timberborn\Timberborn_Data\Managed` (ReflectionOnlyLoad). These are the real service/type names and signatures the TimberBridge mod binds against. `CONFIRMED` = seen via reflection; `GAP` = needs a decompile pass (ILSpy) to nail the exact member.

Internal `Timberborn.*` types are not a stable public contract — pin to this build and add a startup self-check that logs loudly if a service is missing.

## Time & speed — `Timberborn.TimeSystem`
| Type | Members we use |
|---|---|
| `IDayNightCycle` | `JumpTimeInHours(float)`, `SetTimeToNextDay()`, `HoursToNextStartOf(TimeOfDay)`, `DayNumberHoursFromNow(float)`, `TicksToHours`/`HoursToTicks` |
| `SpeedManager` | `ChangeSpeed(float)`, `ChangeAndLockSpeed(float)`, `UnlockSpeed()`, `ChangeSpeedScale(float)` |
| `TimeFastForwarder` | `JumpToNextDaytime()` |
| `ITimeTrigger` / `ITimeTriggerFactory` | `Pause()`, `Resume()`, `FastForwardProgress(float)`; `Create(Action, float)` |
| enum `TimeOfDay` | `Daytime`, `Nighttime` |

Pause = `ChangeSpeed(0)` / speed control = `ChangeSpeed(n)`. Bounded time advance = `IDayNightCycle.JumpTimeInHours(h)`.

## Cycle — `Timberborn.GameCycleSystem`
- `GameCycleService` — current cycle/day state.
- Events: `CycleStartedEvent`, `CycleDayStartedEvent`, `CycleEndedEvent`, `DaytimeStartEvent`.
- `ICycleDuration.SetForCycle(int)`.

## Weather & hazards — `Timberborn.WeatherSystem`, `Timberborn.HazardousWeatherSystem`
| Type | Members we use |
|---|---|
| `WeatherService` | `NextDayIsHazardousWeather() : bool` |
| `TemperateWeatherDurationService` | `GenerateDuration()`, `SetForCycle(int)` |
| `DroughtWeather` | `GetDurationAtCycle(int) : int` |
| `BadtideWeather` | `CanOccurAtCycle(int) : bool`, `GetDurationAtCycle(int) : int` |
| `HazardousWeatherService` | `SetForCycle(int)`, `StartHazardousWeather()`, `EndHazardousWeather()` |
| `HazardousWeatherHistory` | `TryGetPreviousHazardousWeatherData(out ...)`, `GetCyclesCount(string)` |
| `IHazardousWeather` | `GetDurationAtCycle(int)` |

This is the **forecast**: for the coming cycle `c+1`, `DroughtWeather.GetDurationAtCycle(c+1)` / `BadtideWeather.GetDurationAtCycle(c+1)` yield exact hazard lengths — the number the whole water/food storage math keys off.

## Water — `Timberborn.WaterSystem`
| Type | Members we use |
|---|---|
| `IThreadSafeWaterMap` | `WaterDepth(Vector3Int) : float`, `ColumnContamination(Vector3Int) : float`, `WaterFlowDirection(Vector3Int) : Vector2`, `CellIsUnderwater(Vector3Int) : bool`, `IsWaterOnAnyHeight(Vector2Int) : bool`, `ColumnFloor/ColumnCeiling` |
| `IWaterService` | `AddCleanWater/RemoveCleanWater(Vector3Int,float)`, `AddContaminatedWater`, obstacle & inflow limiters, `AddDirectionLimiter(Vector3Int, FlowDirection)` |
| enum `FlowDirection` | `Any, Bottom, Left, Top, Right` |

`IThreadSafeWaterMap` is explicitly thread-safe → water reads can happen off the Unity main thread. `IWaterService` mutators are main-thread only.

## Soil / moisture — `Timberborn.SoilMoistureSystem` (+ `SoilContaminationSystem`)
- `ISoilMoistureService.SoilIsMoist(Vector3Int) : bool`, `SoilMoisture(int) : float` — farmable land.
- `SoilContaminationSystem` — badtide soil contamination (GAP: exact members).

## Beaver needs & wellbeing — `Timberborn.NeedSystem` (+ `Wellbeing`)
| Type | Members we use |
|---|---|
| `NeedManager` (per beaver) | `GetNeedPoints(string) : float`, `NeedPointsToMax(string) : float`, `NeedIsInCriticalState(string) : bool`, `NeedIsBelowWarningThreshold(string) : bool`, `NeedIsAtMinimumPoints(string) : bool`, `GetNeedWellbeing(string) : int`, `GetNeedSpec(string) : NeedSpec` |

Needs are addressed by string key (e.g. `"Thirst"`, `"Hunger"`, `"Sleep"`) — confirm exact keys from `NeedSpecs` at runtime. Aggregate distress = count beavers whose `NeedIsBelowWarningThreshold`/`NeedIsInCriticalState` per key.

## Entity read backbone — `Timberborn.EntitySystem` (CONFIRMED — resolves the biggest unknown)
| Type | Members we use |
|---|---|
| `EntityComponentRegistry` | `GetAll() : IEnumerable`, `GetEnabled() : IEnumerable`, `Register`/`Unregister` |
| `EntityService` | `Instantiate(Blueprint)`, `Delete(BaseComponent)` |
| `EntityRegistry` | `GetEntity(Guid)` |
| `RegisteredComponentService` | `GetRegisterableTypes(Type)` |
| Events | `EntityCreatedEvent`, `EntityDeletedEvent`, `EntityInitializedEvent` |

The read path: inject `EntityComponentRegistry`, enumerate `GetEnabled()`, filter by component type (beavers, buildings, stockpiles). Entity/lifecycle events feed `/events`. A typed `GetEnabled<T>()` generic almost certainly exists — confirm on decompile.

## Placement & blocks — `Timberborn.BlockSystem`
| Type | Members we use |
|---|---|
| `BlockObjectFactory` | `CreateUnfinished(BlockObjectSpec, Placement)`, `CreateFinished(BlockObjectSpec, Placement)`, `CreateAsPreview(...)` |
| `BlockValidator` | `BlocksValid(BlockObjectSpec, Placement) : bool`, `BlocksAlmostValid(...)` |
| `BlockObjectValidationService` | `IsValid(BlockObject)`, `AreValid(..., out string)` |
| `IBlockService` | `AnyObjectAt(Vector3Int) : bool` |
| `Blocks` | `GetOccupiedCoordinates()`, `GetAllCoordinates()` |
| `AreaIterator` | `GetRectangle/GetLine/GetCuboid(Vector3Int, Vector3Int, int)` |
| enums | `BlockObjectLayout{Single,Rectangle,Line,Half,SideLine,TwoSegmentLine}`, `Orientation`, `Placement` |

`BlockValidator.BlocksValid(spec, placement)` is the **teaching-error engine**: validate before `CreateFinished`, and on failure scan neighboring `Placement`s for the nearest valid tile to return as a suggestion.

## Goods, inventory, stockpiles — GAP
- `Timberborn.GameGoods` is data-only (no public top-level services via reflection).
- `Timberborn.InventorySystem` exposed no public top-level types; the `Inventory` component likely lives in `Timberborn.Inventories`/`Timberborn.Goods`. **Confirm on decompile** — this is how `/state` reads per-building and global stock (water/food/logs/planks days-remaining).
- Stockpile services: `Timberborn.GameStockpiles`, `Timberborn.Stockpiles`, `Timberborn.StockpilePrioritySystem`.

## Districts — GAP
- `Timberborn.GameDistricts` exposed no public top-level types via reflection. District center + population aggregation live here or in a sibling assembly. **Confirm on decompile** (population totals, worker distribution feed `/state`).

## Save / load — `Timberborn.SaveSystem` + repository assemblies
- `SaveWriter.WriteToSaveStream(Stream, bool)`, `SaveReader.ReadFromSaveStream(...)` (low-level streams).
- Slot-level save/load: `Timberborn.GameSaveRepositorySystem`, `Timberborn.GameSaveRuntimeSystem` (GAP: exact API).
- **Risk:** loading a save almost certainly triggers a scene reload, tearing down the `[Context("Game")]` singleton that hosts the HTTP server. The bridge must stop/rebind cleanly and the agent must reconnect. Validate early (this gates the `save`/`load` actions used as the agent's rollback guardrail).

## How to extend this
Regenerate/expand with the reflection helper in `scratchpad` (EncodedCommand ReflectionOnly dump). During Phase 0 the bridge itself should expose a one-shot `/blueprints` dump (enumerate `BlockObjectSpec`/`ComponentSpec` via the registry) to export exact costs/sizes/outputs and reconcile the KB's `(v?)` numbers against ground truth.
