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

#include "NavData.h"
#include "DetourAlloc.h"
#include "IVMapMgr.h"
#include "MMapMgr.h"
#include "MapDefines.h"
#include "MapTree.h"
#include "StringFormat.h"
#include "VMapMgr2.h"
#include <G3D/Vector3.h>
#include <G3D/g3dmath.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>

namespace navprobe
{
namespace
{
    // Mirrors the map file structs in src/server/game/Grids/GridTerrainData.h, which a tool cannot
    // include. Keep in step with it.
    union MapMagic
    {
        char asChar[4];
        uint32 asUInt;
    };

    constexpr uint32 MAP_VERSION_MAGIC    = 9;
    constexpr uint32 MAP_HEIGHT_NO_HEIGHT = 0x0001;
    constexpr uint32 MAP_HEIGHT_AS_INT16  = 0x0002;
    constexpr uint32 MAP_HEIGHT_AS_INT8   = 0x0004;

    constexpr int CENTER_GRID_ID = MAX_NUMBER_OF_GRIDS / 2;

    MapMagic const MapsMagic   = { { 'M', 'A', 'P', 'S' } };
    MapMagic const HeightMagic = { { 'M', 'H', 'G', 'T' } };

    struct MapFileHeader
    {
        uint32 mapMagic;
        uint32 versionMagic;
        uint32 buildMagic;
        uint32 areaMapOffset;
        uint32 areaMapSize;
        uint32 heightMapOffset;
        uint32 heightMapSize;
        uint32 liquidMapOffset;
        uint32 liquidMapSize;
        uint32 holesOffset;
        uint32 holesSize;
    };

    struct MapHeightHeader
    {
        uint32 fourcc;
        uint32 flags;
        float gridHeight;
        float gridMaxHeight;
    };

    uint16 const holetab_h[4] = { 0x1111, 0x2222, 0x4444, 0x8888 };
    uint16 const holetab_v[4] = { 0x000F, 0x00F0, 0x0F00, 0xF000 };

    template<typename T>
    bool ReadInto(std::ifstream& in, std::vector<float>& out, std::size_t count, float multiplier, float base)
    {
        std::vector<T> raw(count);
        if (!in.read(reinterpret_cast<char*>(raw.data()), count * sizeof(T)))
            return false;

        out.resize(count);
        for (std::size_t i = 0; i < count; ++i)
            out[i] = float(raw[i]) * multiplier + base;

        return true;
    }
}

uint16 UnitProfile::IncludeFlags() const
{
    if (navFlagsOverride)
        return navFlagsOverride;

    // PathGenerator::CreateFilter
    uint16 flags = 0;
    if (isCreature)
    {
        if (canWalk)
            flags |= NAV_GROUND;

        // creatures don't take environmental damage
        if (canEnterWater)
            flags |= (NAV_WATER | NAV_MAGMA);
    }
    else
        flags |= (NAV_GROUND | NAV_WATER | NAV_MAGMA);

    return flags;
}

std::string UnitProfile::Describe() const
{
    std::string out = isCreature ? "creature" : "player";
    if (canFly)
        out += " +fly";
    if (canSwim)
        out += " +swim";
    if (isFalling)
        out += " +falling";
    if (hoverHeight > 0.0f)
        out += Acore::StringFormat(" +hover {:.2f}", hoverHeight);

    return out + Acore::StringFormat(" nav 0x{:02x}", IncludeFlags());
}

void ComputeGridCoord(float x, float y, int& gx, int& gy)
{
    // Acore::ComputeGridCoord
    gx = std::max<int>(0, int(CENTER_GRID_ID - x / SIZE_OF_GRIDS));
    gy = std::max<int>(0, int(CENTER_GRID_ID - y / SIZE_OF_GRIDS));
}

/////////////////////////////// NavMesh ///////////////////////////////

NavMesh::~NavMesh()
{
    if (_query)
        dtFreeNavMeshQuery(_query);
    if (_mesh)
        dtFreeNavMesh(_mesh);
}

bool NavMesh::Load(std::string const& dataDir, uint32 mapId, std::string& error)
{
    // Match the server's allocator so DT_TILE_FREE_DATA releases what dtAlloc handed us.
    dtAllocSetCustom(dtCustomAlloc, dtCustomFree);

    std::string const paramsFile = Acore::StringFormat(MMAP::MAP_FILE_NAME_FORMAT, dataDir, mapId);

    FILE* file = fopen(paramsFile.c_str(), "rb");
    if (!file)
    {
        error = Acore::StringFormat("no mmap param file at {}", paramsFile);
        return false;
    }

    std::size_t const read = fread(&_params, sizeof(dtNavMeshParams), 1, file);
    fclose(file);
    if (read != 1)
    {
        error = Acore::StringFormat("could not read dtNavMeshParams from {}", paramsFile);
        return false;
    }

    _hasParams = true;

    _mesh = dtAllocNavMesh();
    if (!_mesh)
    {
        error = "dtAllocNavMesh failed";
        return false;
    }

    if (DT_SUCCESS != _mesh->init(&_params))
    {
        error = Acore::StringFormat("dtNavMesh::init failed for map {:03}", mapId);
        return false;
    }

    std::string const mmapsDir = dataDir + "/mmaps";
    std::string const prefix = Acore::StringFormat("{:03}", mapId);

    std::error_code ec;
    std::vector<std::string> tileFiles;
    for (auto const& entry : std::filesystem::directory_iterator(mmapsDir, ec))
    {
        std::string const name = entry.path().filename().string();
        if (name.size() != 14 || name.compare(0, 3, prefix) != 0 || name.compare(7, 7, ".mmtile") != 0)
            continue;

        tileFiles.push_back(entry.path().string());
    }

    if (ec)
    {
        error = Acore::StringFormat("cannot list {}: {}", mmapsDir, ec.message());
        return false;
    }

    std::sort(tileFiles.begin(), tileFiles.end());
    _tileFilesFound = uint32(tileFiles.size());

    bool orderReported = false;
    for (std::string const& path : tileFiles)
    {
        std::string const name = std::filesystem::path(path).filename().string();
        int const nameA = std::stoi(name.substr(3, 2));
        int const nameB = std::stoi(name.substr(5, 2));

        FILE* tf = fopen(path.c_str(), "rb");
        if (!tf)
        {
            _warnings.push_back(Acore::StringFormat("{}: cannot open", name));
            continue;
        }

        MmapTileHeader header;
        if (fread(&header, sizeof(MmapTileHeader), 1, tf) != 1 || header.mmapMagic != MMAP_MAGIC)
        {
            _warnings.push_back(Acore::StringFormat("{}: bad header", name));
            fclose(tf);
            continue;
        }

        if (header.mmapVersion != MMAP_VERSION || header.dtVersion != DT_NAVMESH_VERSION)
        {
            _warnings.push_back(Acore::StringFormat("{}: generator v{} dt v{}, expected v{} / v{}",
                name, header.mmapVersion, header.dtVersion, MMAP_VERSION, DT_NAVMESH_VERSION));
            fclose(tf);
            continue;
        }

        unsigned char* data = static_cast<unsigned char*>(dtAlloc(header.size, DT_ALLOC_PERM));
        if (!data || fread(data, header.size, 1, tf) != 1)
        {
            _warnings.push_back(Acore::StringFormat("{}: truncated tile data", name));
            dtFree(data);
            fclose(tf);
            continue;
        }
        fclose(tf);

        dtMeshHeader const* meshHeader = reinterpret_cast<dtMeshHeader const*>(data);
        int const tileX = meshHeader->x;
        int const tileY = meshHeader->y;

        // mmaps_generator writes the name as (mapId, tileY, tileX) while MMapMgr reads it as
        // (mapId, x, y). Detour places a tile from its own header either way, so scanning the
        // directory sidesteps the question - this only reports which way round the names are.
        if (!orderReported && (nameA != tileX || nameB != tileY))
        {
            _warnings.push_back(Acore::StringFormat(
                "tile names are (y,x): {} carries header [{},{}]", name, tileX, tileY));
            orderReported = true;
        }

        dtTileRef ref = 0;
        if (dtStatusFailed(_mesh->addTile(data, header.size, DT_TILE_FREE_DATA, 0, &ref)))
        {
            _warnings.push_back(Acore::StringFormat("{}: addTile failed", name));
            dtFree(data);
            continue;
        }

        _tileCoords.emplace_back(tileX, tileY);
        ++_tilesLoaded;
    }

    _query = dtAllocNavMeshQuery();
    if (!_query || DT_SUCCESS != _query->init(_mesh, 1024))
    {
        error = "dtNavMeshQuery::init failed";
        return false;
    }

    return true;
}

/////////////////////////////// GridTile ///////////////////////////////

bool GridTile::Load(std::string const& fileName)
{
    std::ifstream in(fileName, std::ios::binary);
    if (!in)
        return false;

    MapFileHeader header;
    if (!in.read(reinterpret_cast<char*>(&header), sizeof(header)))
        return false;

    if (header.mapMagic != MapsMagic.asUInt || header.versionMagic != MAP_VERSION_MAGIC)
        return false;

    in.seekg(header.heightMapOffset);

    MapHeightHeader heightHeader;
    if (!in.read(reinterpret_cast<char*>(&heightHeader), sizeof(heightHeader)) ||
        heightHeader.fourcc != HeightMagic.asUInt)
        return false;

    _heightFlags = heightHeader.flags;
    _gridHeight = heightHeader.gridHeight;

    if (!(heightHeader.flags & MAP_HEIGHT_NO_HEIGHT))
    {
        // Whatever the on-disk encoding, flatten to absolute floats. The multiplier factors straight
        // out of the triangle solve, so this is exact and leaves one interpolation instead of three.
        std::size_t const v9Count = 129 * 129;
        std::size_t const v8Count = 128 * 128;

        if (heightHeader.flags & MAP_HEIGHT_AS_INT16)
        {
            float const mult = (heightHeader.gridMaxHeight - heightHeader.gridHeight) / 65535.0f;
            if (!ReadInto<uint16>(in, _v9, v9Count, mult, _gridHeight) ||
                !ReadInto<uint16>(in, _v8, v8Count, mult, _gridHeight))
                return false;
        }
        else if (heightHeader.flags & MAP_HEIGHT_AS_INT8)
        {
            float const mult = (heightHeader.gridMaxHeight - heightHeader.gridHeight) / 255.0f;
            if (!ReadInto<uint8>(in, _v9, v9Count, mult, _gridHeight) ||
                !ReadInto<uint8>(in, _v8, v8Count, mult, _gridHeight))
                return false;
        }
        else
        {
            if (!ReadInto<float>(in, _v9, v9Count, 1.0f, 0.0f) ||
                !ReadInto<float>(in, _v8, v8Count, 1.0f, 0.0f))
                return false;
        }

        _hasHeightmap = true;
    }

    if (header.holesSize == sizeof(_holes))
    {
        in.seekg(header.holesOffset);
        if (in.read(reinterpret_cast<char*>(_holes.data()), sizeof(_holes)))
            _hasHoles = true;
    }

    return true;
}

bool GridTile::IsHole(int row, int col) const
{
    if (!_hasHoles)
        return false;

    int const cellRow = row / 8; // 8 squares per cell
    int const cellCol = col / 8;
    int const holeRow = row % 8 / 2;
    int const holeCol = (col - (cellCol * 8)) / 2;

    uint16 const hole = _holes[cellRow * 16 + cellCol];

    return (hole & holetab_h[holeCol] & holetab_v[holeRow]) != 0;
}

float GridTile::GetHeight(float x, float y) const
{
    if (!_hasHeightmap)
        return _gridHeight; // GridTerrainData::getHeightFromFlat

    x = MAP_RESOLUTION * (32 - x / SIZE_OF_GRIDS);
    y = MAP_RESOLUTION * (32 - y / SIZE_OF_GRIDS);

    int x_int = int(x);
    int y_int = int(y);
    x -= x_int;
    y -= y_int;
    x_int &= (MAP_RESOLUTION - 1);
    y_int &= (MAP_RESOLUTION - 1);

    if (IsHole(x_int, y_int))
        return INVALID_HEIGHT;

    // h5 comes from the v8 grid, h1-h4 from v9. Pick the triangle, then solve h = a*x + b*y + c.
    float a, b, c;
    if (x + y < 1)
    {
        if (x > y)
        {
            float const h1 = _v9[(x_int) * 129 + y_int];
            float const h2 = _v9[(x_int + 1) * 129 + y_int];
            float const h5 = 2 * _v8[x_int * 128 + y_int];
            a = h2 - h1;
            b = h5 - h1 - h2;
            c = h1;
        }
        else
        {
            float const h1 = _v9[x_int * 129 + y_int];
            float const h3 = _v9[x_int * 129 + y_int + 1];
            float const h5 = 2 * _v8[x_int * 128 + y_int];
            a = h5 - h1 - h3;
            b = h3 - h1;
            c = h1;
        }
    }
    else
    {
        if (x > y)
        {
            float const h2 = _v9[(x_int + 1) * 129 + y_int];
            float const h4 = _v9[(x_int + 1) * 129 + y_int + 1];
            float const h5 = 2 * _v8[x_int * 128 + y_int];
            a = h2 + h4 - h5;
            b = h4 - h2;
            c = h5 - h4;
        }
        else
        {
            float const h3 = _v9[(x_int) * 129 + y_int + 1];
            float const h4 = _v9[(x_int + 1) * 129 + y_int + 1];
            float const h5 = 2 * _v8[x_int * 128 + y_int];
            a = h4 - h3;
            b = h3 + h4 - h5;
            c = h5 - h4;
        }
    }

    return a * x + b * y + c;
}

/////////////////////////////// HeightData ///////////////////////////////

HeightData::~HeightData()
{
    delete _tree;
}

bool HeightData::Load(std::string const& dataDir, uint32 mapId)
{
    _dataDir = dataDir;
    _mapId = mapId;

    std::string const prefix = Acore::StringFormat("{:03}", mapId);
    std::error_code ec;

    // Count what is on disk before loading any of it, so "no vmaps at all" reads differently from
    // "vmaps present but the tree refused them".
    std::vector<std::pair<int, int>> gridTiles;
    for (auto const& entry : std::filesystem::directory_iterator(dataDir + "/maps", ec))
    {
        std::string const name = entry.path().filename().string();
        if (name.size() != 11 || name.compare(0, 3, prefix) != 0 || name.compare(7, 4, ".map") != 0)
            continue;

        gridTiles.emplace_back(std::stoi(name.substr(3, 2)), std::stoi(name.substr(5, 2)));
    }
    _gridFilesFound = uint32(gridTiles.size());

    for (auto const& entry : std::filesystem::directory_iterator(dataDir + "/vmaps", ec))
    {
        std::string const name = entry.path().filename().string();
        if (name.size() > 7 && name.compare(0, 3, prefix) == 0 &&
            name.compare(name.size() - 7, 7, ".vmtile") == 0)
            ++_vmapFilesFound;
    }

    _tree = new VMAP::StaticMapTree(mapId, dataDir + "/vmaps");
    if (!_tree->InitMap(VMAP::VMapMgr2::getMapFileName(mapId)))
    {
        delete _tree;
        _tree = nullptr;
    }
    else
    {
        for (auto const& tile : gridTiles)
            if (_tree->LoadMapTile(tile.first, tile.second))
                ++_vmapTilesLoaded;
    }

    return true;
}

GridTile const* HeightData::TileAt(float x, float y) const
{
    int gx, gy;
    ComputeGridCoord(x, y, gx, gy);

    uint32 const key = uint32(gx) << 8 | uint32(gy);
    auto it = _gridTiles.find(key);
    if (it != _gridTiles.end())
        return it->second.get();

    auto tile = std::make_unique<GridTile>();
    if (!tile->Load(Acore::StringFormat("{}/maps/{:03}{:02}{:02}.map", _dataDir, _mapId, gx, gy)))
        tile.reset();
    else
        ++_gridTilesLoaded;

    return (_gridTiles[key] = std::move(tile)).get();
}

float HeightData::GetGridHeight(float x, float y) const
{
    GridTile const* tile = TileAt(x, y);
    return tile ? tile->GetHeight(x, y) : INVALID_HEIGHT;
}

float HeightData::GetVmapHeight(float x, float y, float z, float maxSearchDist) const
{
    if (!_tree)
        return VMAP_INVALID_HEIGHT_VALUE;

    G3D::Vector3 const pos = VMAP::VMapMgr2::convertPositionToInternalRep(x, y, z);
    float const height = _tree->getHeight(pos, maxSearchDist);
    if (height >= G3D::finf())
        return VMAP_INVALID_HEIGHT_VALUE;

    return height;
}

float HeightData::GetMapHeight(float x, float y, float z, bool checkVmap, float maxSearchDist) const
{
    // Map::GetHeight, minus GetDynamicTree() - GameObject collision is live server state.
    float mapHeight = VMAP_INVALID_HEIGHT_VALUE;
    float const gridHeight = GetGridHeight(x, y);
    if (z >= gridHeight - GROUND_HEIGHT_TOLERANCE)
        mapHeight = gridHeight;

    float vmapHeight = VMAP_INVALID_HEIGHT_VALUE;
    if (checkVmap)
        vmapHeight = GetVmapHeight(x, y, z, maxSearchDist);

    if (vmapHeight > INVALID_HEIGHT)
    {
        if (mapHeight > INVALID_HEIGHT)
        {
            if (vmapHeight > mapHeight || std::fabs(mapHeight - z) > std::fabs(vmapHeight - z))
                return vmapHeight;

            return mapHeight;
        }

        return vmapHeight;
    }

    return mapHeight;
}

float HeightData::GetMapHeightForUnit(UnitProfile const& profile, float x, float y, float z) const
{
    // WorldObject::GetMapHeight bumps Z before the lookup.
    if (z != MAX_HEIGHT)
        z += std::max(profile.collisionHeight, Z_OFFSET_FIND_HEIGHT);

    return GetMapHeight(x, y, z);
}

float HeightData::UpdateAllowedPositionZ(UnitProfile const& profile, float x, float y, float z, float* groundZ) const
{
    // WorldObject::UpdateAllowedPositionZ. Liquid is not modelled offline, so the swim branch
    // collapses into the ground branch - the point command prints that caveat.
    if (profile.canFly)
    {
        float const ground_z = GetMapHeightForUnit(profile, x, y, z) + profile.hoverHeight;
        if (z < ground_z)
            z = ground_z;

        if (groundZ)
            *groundZ = ground_z;

        return z;
    }

    float ground_z = GetMapHeightForUnit(profile, x, y, z);
    float max_z = ground_z;

    if (max_z > INVALID_HEIGHT)
    {
        max_z += profile.hoverHeight;
        ground_z += profile.hoverHeight;

        if (z > max_z)
            z = max_z;
        else if (z < ground_z)
            z = ground_z;
    }

    if (groundZ)
        *groundZ = ground_z;

    return z;
}
}
