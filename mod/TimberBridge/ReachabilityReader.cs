using System;
using Timberborn.BlockSystem;          // BlockObject                    (CONFIRMED)
using Timberborn.Buildings;            // DistrictBuilding               (CONFIRMED)
using Timberborn.Coordinates;          // CoordinateSystem (static)      (CONFIRMED)
using Timberborn.GameDistricts;        // DistrictCenterRegistry         (CONFIRMED)
using Timberborn.Navigation;           // IDistrictService               (CONFIRMED)
using UnityEngine;                     // Vector3Int / Vector3

namespace TimberBridge {

  // Game-truth reachability: the same navigation the game uses to decide whether
  // beavers/builders can reach a tile. Every method here runs on the Unity main
  // thread (called from StateReader/MapReader, both dispatched to main thread), so
  // touching IDistrictService is safe.
  //
  // KEY API (CONFIRMED via decompile of Timberborn.Navigation.dll):
  //   IDistrictService.IsOnInstantDistrictRoadSpill(Vector3 position) -> bool
  //     Internally: _nodeIdService.Contains(pos) &&
  //                 _instantDistrictMap.NodeHasAnyDistrictRoadSpillFlowField(WorldToId(pos)).
  //     NodeHasAnyDistrictRoadSpillFlowField iterates EVERY district's road-spill
  //     flow field, so this is a global "is this tile on ANY district's road or
  //     road-spill" test — exactly the pre-build reachability the planner needs.
  //     No per-district loop required.
  //
  // COORD CHOICE (CONFIRMED): WorldToId does WorldToGridInt(pos + (0,0.1,0)) then
  //   floors. Timberborn addresses nav nodes at tile CENTERS
  //   (NavigationCoordinateSystem.GridToWorld == CoordinateSystem.GridToWorldCentered).
  //   We therefore feed GridToWorldCentered(tile) = (x+0.5, z, y+0.5): dead-center
  //   of the tile, which floors back cleanly to the tile and matches how the game
  //   itself generates/queries nav-node positions. (Plain GridToWorld sits on the
  //   corner and would also floor to the tile, but centered is the faithful choice
  //   and avoids any floating-point edge ambiguity.)
  public class ReachabilityReader {

    private readonly IDistrictService _districtService;
    private readonly DistrictCenterRegistry _districtCenterRegistry;

    public ReachabilityReader(IDistrictService districtService,
                              DistrictCenterRegistry districtCenterRegistry) {
      _districtService = districtService;
      _districtCenterRegistry = districtCenterRegistry;
    }

    // How high above z=0 to probe when a caller gives a column (x,y) without a
    // known surface height. Road-spill nav nodes live at the terrain height the road
    // sits on, and NodeIdService indexes nodes by full 3D coord (x,y,z), so a tile at
    // z=0 only matches ground-level roads. Timberborn maps cap terrain height at ~16;
    // 32 covers stacked platforms with margin. Out-of-range z fails Contains() and is
    // simply skipped (returns false), so this never throws.
    private const int VerticalProbeCeiling = 32;

    // PRE-BUILD test: can beavers/builders reach this grid tile? True if the tile's
    // world center is on any district's road or road-spill.
    //   - If tile.z > 0 (caller supplied a surface height, e.g. an object's own z),
    //     test exactly that cell — game-faithful.
    //   - If tile.z == 0 (a bare column from a /map scan), probe upward through the
    //     height column and report reachable if ANY height is on a road-spill node.
    // Wrapped so a single bad coordinate never throws through a grid scan (defaults to
    // unreachable).
    public bool IsTileReachable(Vector3Int tile) {
      try {
        if (tile.z > 0) {
          return IsCellOnRoadSpill(tile);
        }
        for (int z = 0; z < VerticalProbeCeiling; z++) {
          if (IsCellOnRoadSpill(new Vector3Int(tile.x, tile.y, z))) return true;
        }
        return false;
      } catch {
        return false;
      }
    }

    private bool IsCellOnRoadSpill(Vector3Int cell) {
      Vector3 world = CoordinateSystem.GridToWorldCentered(cell);
      return _districtService.IsOnInstantDistrictRoadSpill(world);
    }

    // TIGHT ROAD test (NOT the spill): is this grid tile ON the actual district-road
    // network — i.e. a district-center road node or a placed Path connected to it?
    // This is the network a FINISHED building must touch to be staffed/reachable; the
    // road-spill test above is the wide builder radius and is WRONG for this purpose.
    //
    // CONFIRMED (decompile of Timberborn.GameDistricts.dll + Timberborn.Navigation.dll):
    //   The game decides a finished building's DistrictBuilding.InstantDistrict via
    //   DistrictBuilding.ShouldBeAssignedToInstantDistrict ->
    //   DistrictCenter.AccessibleIsOnInstantDistrictRoad ->
    //   IDistrictService.IsOnInstantDistrictRoad(District, Vector3) ->
    //   InstantDistrictMap.RoadNodeIsOccupiedByDistrict(district, WorldToId(pos)).
    //   RoadNodeIsOccupiedByDistrict consults _districtsOnRoads, the map of ROAD nodes
    //   (DC road + placed Path) to owning district. It does NOT touch any road-SPILL
    //   flow field. So this is exactly the tight road network the finished building
    //   needs, and it mirrors the game's own InstantDistrict != null decision.
    //
    //   DistrictCenter exposes the same test publicly as
    //   DistrictCenter.IsOnInstantDistrictRoad(Vector3 start) (delegates to the
    //   IDistrictService overload against its own District). We iterate all finished
    //   district centers and return true if the tile is on ANY of their road networks.
    //
    // COORD CHOICE (CONFIRMED): same as IsCellOnRoadSpill — WorldToId floors
    //   GridToWorldCentered(tile), and NavigationCoordinateSystem.GridToWorld ==
    //   CoordinateSystem.GridToWorldCentered, so the tile center is the faithful probe.
    public bool IsTileOnDistrictRoad(Vector3Int tile) {
      try {
        Vector3 world = CoordinateSystem.GridToWorldCentered(tile);
        foreach (DistrictCenter dc in _districtCenterRegistry.FinishedDistrictCenters) {
          if (dc != null && dc.IsOnInstantDistrictRoad(world)) return true;
        }
        return false;
      } catch {
        return false;
      }
    }

    // Game-truth for an EXISTING object. Buildings carry a DistrictBuilding whose
    // InstantDistrict != null is the same signal behind the in-game
    // "Unconnected"/"Unreachable" warning. Objects with NO DistrictBuilding (paths /
    // walkables) are not district members — the correct test is road-spill
    // membership of their own tile (a floating path not connected to a DC is NOT on
    // any district road-spill). This replaces the buggy `db == null` shortcut, which
    // reported such paths as reachable unconditionally.
    public bool IsObjectReachable(BlockObject block) {
      try {
        if (block == null) return false;
        var db = block.GetComponent<DistrictBuilding>();
        if (db != null) {
          return db.InstantDistrict != null;
        }
        return IsTileReachable(block.Coordinates);
      } catch {
        return false;
      }
    }

    // Row-major reachability grid for /map: index = row*width + col;
    // tile x = originX + col, tile y = originZ + row. 1 = reachable, 0 = not.
    // Per-tile calls are individually guarded (IsTileReachable never throws), so one
    // bad cell cannot take down the whole grid — it defaults to 0/unreachable.
    public int[] ReachabilityGrid(int originX, int originZ, int width, int height) {
      var grid = new int[width * height];
      for (int row = 0; row < height; row++) {
        for (int col = 0; col < width; col++) {
          var tile = new Vector3Int(originX + col, originZ + row, 0);
          grid[row * width + col] = IsTileReachable(tile) ? 1 : 0;
        }
      }
      return grid;
    }

  }

}
