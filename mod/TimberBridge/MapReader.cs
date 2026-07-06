using System.Globalization;
using System.Text;
using Timberborn.BlockSystem;          // IBlockService                 (CONFIRMED)
using Timberborn.GameDistricts;        // DistrictCenterRegistry, DistrictCenter (CONFIRMED)
using Timberborn.MapStateSystem;       // MapSize                       (CONFIRMED)
using Timberborn.SoilMoistureSystem;   // ISoilMoistureService          (CONFIRMED)
using Timberborn.TerrainSystem;        // ITerrainService               (CONFIRMED)
using Timberborn.WaterSystem;          // IThreadSafeWaterMap           (CONFIRMED)
using UnityEngine;                     // Vector3Int / Vector2Int

namespace TimberBridge
{
    // NOTE ON AXES (CONFIRMED): Timberborn's horizontal plane is Vector3Int.x / .y.
    // Vector3Int.z is the VERTICAL height. The API "(x,z)" column maps to game (x, y).
    // Every per-cell query is taken at the surface height for that column.
    public sealed class MapReader
    {
        private readonly MapSize _mapSize;                          // CONFIRMED: Bind<MapSize>().AsSingleton()
        private readonly ITerrainService _terrainService;          // CONFIRMED: Bind<ITerrainService>().To<TerrainService>().AsSingleton()
        private readonly IThreadSafeWaterMap _waterMap;            // CONFIRMED: Bind<IThreadSafeWaterMap>().ToExisting<ThreadSafeWaterMap>()
        private readonly ISoilMoistureService _soilMoistureService;// CONFIRMED: Bind<ISoilMoistureService>().To<SoilMoistureService>().AsSingleton()
        private readonly IBlockService _blockService;              // CONFIRMED: Bind<IBlockService>().ToExisting<BlockService>()
        private readonly DistrictCenterRegistry _districtCenterRegistry; // CONFIRMED: Bind<DistrictCenterRegistry>().AsSingleton()
        private readonly ReachabilityReader _reachability;         // game-truth road-spill reachability per tile

        public MapReader(
            MapSize mapSize,
            ITerrainService terrainService,
            IThreadSafeWaterMap waterMap,
            ISoilMoistureService soilMoistureService,
            IBlockService blockService,
            DistrictCenterRegistry districtCenterRegistry,
            ReachabilityReader reachability)
        {
            _mapSize = mapSize;
            _terrainService = terrainService;
            _waterMap = waterMap;
            _soilMoistureService = soilMoistureService;
            _blockService = blockService;
            _districtCenterRegistry = districtCenterRegistry;
            _reachability = reachability;
        }

        // CONFIRMED: MapSize.TerrainSize2D : Vector2Int -> (width, depth). .z (height) from TerrainSize.
        // Returns the coordinate of the main (first finished) DistrictCenter, or map centre if none.
        // CONFIRMED: DistrictCenterRegistry.FinishedDistrictCenters : ReadOnlyList<DistrictCenter>
        //            DistrictCenter.CenterCoordinates : Vector3Int (= PositionedEntrance.DoorstepCoordinates)
        public bool TryGetMainDistrictCenter(out Vector3Int coordinates)
        {
            var centers = _districtCenterRegistry.FinishedDistrictCenters;
            if (centers.Count > 0)
            {
                coordinates = centers[0].CenterCoordinates;
                return true;
            }
            var s = _mapSize.TerrainSize2D;
            coordinates = new Vector3Int(s.x / 2, s.y / 2, 0);
            return false;
        }

        // Default view: a window centered on the district center.
        public string ReadJson()
        {
            Vector3Int dc;
            TryGetMainDistrictCenter(out dc);
            return ReadMapJson(dc.x, dc.y, 15);
        }

        // Square window [cx-radius, cx+radius] x [cz-radius, cz+radius], clamped to map bounds.
        // Emits compact row-major JSON arrays. "cz" is the game Y axis.
        public string ReadMapJson(int cx, int cz, int radius)
        {
            Vector2Int size2D = _mapSize.TerrainSize2D;               // CONFIRMED
            int height = _mapSize.TerrainSize.z;                      // CONFIRMED: vertical extent
            int topZ = height - 1;                                    // scan-from-top height for surface queries

            int x0 = Mathf.Clamp(cx - radius, 0, size2D.x - 1);
            int x1 = Mathf.Clamp(cx + radius, 0, size2D.x - 1);
            int y0 = Mathf.Clamp(cz - radius, 0, size2D.y - 1);
            int y1 = Mathf.Clamp(cz + radius, 0, size2D.y - 1);

            int w = x1 - x0 + 1;
            int h = y1 - y0 + 1;

            var terrain = new StringBuilder();   // int surface height
            var water = new StringBuilder();      // float water depth, rounded to 0.1
            var contam = new StringBuilder();     // float contamination 0..1, rounded to 0.01
            var moist = new StringBuilder();      // bool
            var occupied = new StringBuilder();   // bool

            terrain.Append('[');
            water.Append('[');
            contam.Append('[');
            moist.Append('[');
            occupied.Append('[');

            bool firstCell = true;
            // Row-major: outer loop over game Y (=cz axis), inner over X.
            for (int y = y0; y <= y1; y++)
            {
                for (int x = x0; x <= x1; x++)
                {
                    if (!firstCell)
                    {
                        terrain.Append(',');
                        water.Append(',');
                        contam.Append(',');
                        moist.Append(',');
                        occupied.Append(',');
                    }
                    firstCell = false;

                    // Surface height for column (x,y): scan down from map top.
                    // CONFIRMED: ITerrainService.GetTerrainHeightBelow(Vector3Int) returns (topSolidZ)+1.
                    int surfaceZ = _terrainService.GetTerrainHeightBelow(new Vector3Int(x, y, topZ));

                    // The cell that sits on top of the surface (where buildings/water live).
                    var surfaceCell = new Vector3Int(x, y, surfaceZ);

                    // CONFIRMED: IThreadSafeWaterMap.WaterDepth(Vector3Int) : float (column depth at that z-body).
                    float depth = _waterMap.WaterDepth(surfaceCell);
                    // CONFIRMED: IThreadSafeWaterMap.ColumnContamination(Vector3Int) : float (0 outside the water column).
                    float contamination = _waterMap.ColumnContamination(surfaceCell);

                    // CONFIRMED: ISoilMoistureService.SoilIsMoist(Vector3Int) : bool (uses coords.z as ceiling ref).
                    bool isMoist = _soilMoistureService.SoilIsMoist(surfaceCell);

                    // CONFIRMED: IBlockService.AnyObjectAt(Vector3Int) : bool (any block object at that 3D cell).
                    bool isOccupied = _blockService.AnyObjectAt(surfaceCell);

                    terrain.Append(surfaceZ);
                    water.Append(Round1(depth));
                    contam.Append(Round2(contamination));
                    moist.Append(isMoist ? "1" : "0");
                    occupied.Append(isOccupied ? "1" : "0");
                }
            }

            terrain.Append(']');
            water.Append(']');
            contam.Append(']');
            moist.Append(']');
            occupied.Append(']');

            // Game-truth reachability per tile (1=on any district road/road-spill, 0=not).
            // Same window and row-major indexing as the arrays above: origin (x0,y0),
            // index = row*w + col, tile x = x0+col, tile y = y0+row.
            int[] reachGrid = _reachability.ReachabilityGrid(x0, y0, w, h);
            var reach = new StringBuilder();
            reach.Append('[');
            for (int i = 0; i < reachGrid.Length; i++)
            {
                if (i > 0) reach.Append(',');
                reach.Append(reachGrid[i]);
            }
            reach.Append(']');

            // Tight district-ROAD membership per tile (1=on the DC road network or a
            // placed Path connected to it, 0=not). Distinct from "reachable" above, which
            // is the wider builder road-spill radius: a building's ACCESS tile must land
            // on THIS grid (not merely be adjacent to it) to be staffed. Same window and
            // row-major indexing as the other arrays.
            int[] roadGrid = _reachability.OnRoadGrid(x0, y0, w, h);
            var onRoad = new StringBuilder();
            onRoad.Append('[');
            for (int i = 0; i < roadGrid.Length; i++)
            {
                if (i > 0) onRoad.Append(',');
                onRoad.Append(roadGrid[i]);
            }
            onRoad.Append(']');

            var sb = new StringBuilder();
            sb.Append('{');
            sb.Append("\"origin\":{\"x\":").Append(x0).Append(",\"z\":").Append(y0).Append('}');
            sb.Append(",\"width\":").Append(w).Append(",\"height\":").Append(h);
            sb.Append(",\"map_size\":{\"x\":").Append(size2D.x).Append(",\"z\":").Append(size2D.y)
              .Append(",\"max_height\":").Append(height).Append('}');
            if (TryGetMainDistrictCenter(out var dc))
                sb.Append(",\"district_center\":{\"x\":").Append(dc.x).Append(",\"z\":").Append(dc.y).Append('}');
            sb.Append(",\"terrain_height\":").Append(terrain);
            sb.Append(",\"water_depth\":").Append(water);
            sb.Append(",\"contamination\":").Append(contam);
            sb.Append(",\"moist\":").Append(moist);
            sb.Append(",\"occupied\":").Append(occupied);
            sb.Append(",\"reachable\":").Append(reach);
            sb.Append(",\"on_road\":").Append(onRoad);
            sb.Append('}');
            return sb.ToString();
        }

        // Invariant-culture rounding so JSON always uses '.' decimal separators.
        private static string Round1(float v)
            => (Mathf.Round(v * 10f) / 10f).ToString("0.#", CultureInfo.InvariantCulture);

        private static string Round2(float v)
            => (Mathf.Round(v * 100f) / 100f).ToString("0.##", CultureInfo.InvariantCulture);
    }
}
