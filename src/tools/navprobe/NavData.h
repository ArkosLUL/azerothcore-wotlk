/*
 * This file is part of the AzerothCore Project. See AUTHORS file for Copyright information
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
 * FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
 * more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program. If not, see <http://www.gnu.org/licenses/>.
 */

#ifndef _NAVPROBE_NAVDATA_H
#define _NAVPROBE_NAVDATA_H

#include "Define.h"
#include "DetourNavMesh.h"
#include "DetourNavMeshQuery.h"
#include <array>
#include <memory>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace VMAP
{
    class StaticMapTree;
}

namespace navprobe
{
    // Mirrors src/server/shared/SharedDefines.h and src/server/game/Grids/GridTerrainData.h, neither of
    // which a tool can include: they live under src/server and drag in the world.
    constexpr float GROUND_HEIGHT_TOLERANCE = 0.05f;
    constexpr float Z_OFFSET_FIND_HEIGHT    = 2.0f;
    constexpr float DEFAULT_COLLISION_HEIGHT = 2.03128f;
    constexpr float DEFAULT_HEIGHT_SEARCH   = 50.0f;
    constexpr float INVALID_HEIGHT          = -100000.0f;
    constexpr float MAX_HEIGHT              = 100000.0f;
    constexpr int   MAP_RESOLUTION          = 128;

    // Movement capabilities every branch in the query and the height chain keys off. Defaults are a
    // ground player, which is what CreateFilter assumes for anything that is not a Creature.
    struct UnitProfile
    {
        bool isCreature     = false;
        bool canFly         = false;
        bool canSwim        = true;
        bool canWalk        = true;
        bool canEnterWater  = true;
        bool isFalling      = false;
        float hoverHeight   = 0.0f;
        float collisionHeight = DEFAULT_COLLISION_HEIGHT;

        // Non-zero replaces whatever IncludeFlags() would have derived.
        uint16 navFlagsOverride = 0;

        [[nodiscard]] uint16 IncludeFlags() const;
        [[nodiscard]] std::string Describe() const;
    };

    // A map's navmesh with every tile on disk preloaded. The server only holds tiles for grids it has
    // loaded, so this answers "reachable in principle", not "reachable right now".
    class NavMesh
    {
    public:
        NavMesh() = default;
        ~NavMesh();

        NavMesh(NavMesh const&) = delete;
        NavMesh& operator=(NavMesh const&) = delete;

        bool Load(std::string const& dataDir, uint32 mapId, std::string& error);

        [[nodiscard]] dtNavMesh const* Mesh() const { return _mesh; }
        [[nodiscard]] dtNavMeshQuery const* Query() const { return _query; }
        [[nodiscard]] dtNavMeshParams const& Params() const { return _params; }
        [[nodiscard]] bool HasParams() const { return _hasParams; }
        [[nodiscard]] uint32 TileFilesFound() const { return _tileFilesFound; }
        [[nodiscard]] uint32 TilesLoaded() const { return _tilesLoaded; }
        [[nodiscard]] std::vector<std::pair<int, int>> const& TileCoords() const { return _tileCoords; }
        [[nodiscard]] std::vector<std::string> const& Warnings() const { return _warnings; }

    private:
        dtNavMesh* _mesh = nullptr;
        dtNavMeshQuery* _query = nullptr;
        dtNavMeshParams _params {};
        bool _hasParams = false;
        uint32 _tileFilesFound = 0;
        uint32 _tilesLoaded = 0;
        std::vector<std::pair<int, int>> _tileCoords;
        std::vector<std::string> _warnings;
    };

    // One .map grid tile's height data, flattened to absolute floats whatever the on-disk encoding was.
    // Mirrors GridTerrainData::LoadHeightData and getHeightFrom*; keep in step with it.
    class GridTile
    {
    public:
        bool Load(std::string const& fileName);

        [[nodiscard]] float GetHeight(float x, float y) const;
        [[nodiscard]] bool HasHeightmap() const { return _hasHeightmap; }
        [[nodiscard]] float FlatHeight() const { return _gridHeight; }
        [[nodiscard]] uint32 HeightFlags() const { return _heightFlags; }

    private:
        [[nodiscard]] bool IsHole(int row, int col) const;

        bool _hasHeightmap = false;
        bool _hasHoles = false;
        uint32 _heightFlags = 0;
        float _gridHeight = INVALID_HEIGHT;
        std::vector<float> _v9;   // 129 * 129
        std::vector<float> _v8;   // 128 * 128
        std::array<uint16, 16 * 16> _holes {};
    };

    // The height half of "would this destination survive?": vmap collision plus raw terrain, combined
    // the way Map::GetHeight combines them, then run through UpdateAllowedPositionZ.
    class HeightData
    {
    public:
        HeightData() = default;
        ~HeightData();

        bool Load(std::string const& dataDir, uint32 mapId);

        [[nodiscard]] bool HasVmapTree() const { return _tree != nullptr; }
        [[nodiscard]] uint32 VmapTilesLoaded() const { return _vmapTilesLoaded; }
        [[nodiscard]] uint32 GridTilesLoaded() const { return _gridTilesLoaded; }
        [[nodiscard]] uint32 GridFilesFound() const { return _gridFilesFound; }
        [[nodiscard]] uint32 VmapFilesFound() const { return _vmapFilesFound; }

        // Raw .map terrain surface. INVALID_HEIGHT when the tile is missing or holed.
        [[nodiscard]] float GetGridHeight(float x, float y) const;
        // WMO/M2 collision surface. VMAP_INVALID_HEIGHT_VALUE when there is nothing there.
        [[nodiscard]] float GetVmapHeight(float x, float y, float z, float maxSearchDist = DEFAULT_HEIGHT_SEARCH) const;
        // Map::GetHeight, minus the dynamic (GameObject) tree, which is server-only state.
        [[nodiscard]] float GetMapHeight(float x, float y, float z, bool checkVmap = true,
                                         float maxSearchDist = DEFAULT_HEIGHT_SEARCH) const;
        // WorldObject::GetMapHeight - the caller-side Z bump the server applies before the lookup.
        [[nodiscard]] float GetMapHeightForUnit(UnitProfile const& profile, float x, float y, float z) const;
        // WorldObject::UpdateAllowedPositionZ. Returns the Z the server would settle on.
        [[nodiscard]] float UpdateAllowedPositionZ(UnitProfile const& profile, float x, float y, float z,
                                                   float* groundZ = nullptr) const;

    private:
        [[nodiscard]] GridTile const* TileAt(float x, float y) const;

        std::string _dataDir;
        uint32 _mapId = 0;
        VMAP::StaticMapTree* _tree = nullptr;
        uint32 _vmapTilesLoaded = 0;
        uint32 _vmapFilesFound = 0;
        uint32 _gridFilesFound = 0;
        // Grid tiles are pulled in on demand from the const height queries.
        mutable uint32 _gridTilesLoaded = 0;
        mutable std::unordered_map<uint32, std::unique_ptr<GridTile>> _gridTiles;
    };

    // gx/gy as ComputeGridCoord derives them, which is also the .map and .vmtile tile index.
    void ComputeGridCoord(float x, float y, int& gx, int& gy);
}

#endif // _NAVPROBE_NAVDATA_H
