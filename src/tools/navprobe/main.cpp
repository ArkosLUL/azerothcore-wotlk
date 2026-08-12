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

#include "IVMapMgr.h"
#include "NavData.h"
#include "NavQuery.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using namespace navprobe;

namespace
{
    char const* USAGE =
        "navprobe - offline navmesh and height probe\n"
        "\n"
        "  navprobe --map ID [--data DIR] <command> [flags]\n"
        "\n"
        "Commands:\n"
        "  coverage                       navmesh params, tile counts, .map and vmap presence\n"
        "  point X Y Z                    on-mesh test, snapped Z, full height breakdown\n"
        "  path X1 Y1 Z1 X2 Y2 Z2         PathType mask, poly count, waypoints, length\n"
        "  ring X Y Z RADIUS HEADINGS     pass/fail table around a centre\n"
        "\n"
        "Profile flags (default: ground player):\n"
        "  --creature                     use the creature branch of CreateFilter\n"
        "  --can-fly                      CanFly: shortcuts out of mesh holes and far-from-poly\n"
        "  --no-swim                      clear CanSwim\n"
        "  --falling                      IsFalling: allows a downhill shortcut\n"
        "  --hover H                      hover height added by UpdateAllowedPositionZ\n"
        "  --collision H                  collision height (default 2.03128)\n"
        "  --nav MASK                     override the filter include flags, e.g. 0x01\n"
        "\n"
        "Path flags:\n"
        "  --straight                     findStraightPath instead of the smooth path\n"
        "  --limit D                      SetPathLengthLimit in yards\n"
        "\n"
        "Output:\n"
        "  --format table|json            default table\n"
        "\n"
        "Every tile on disk is preloaded, so answers are \"reachable in principle\". The server only\n"
        "holds tiles for grids it has loaded, so an in-game .mmap query can legitimately differ.\n"
        "Corridor reuse, raycast mode, the slope gate and liquid are not modelled - see README.md.\n";

    bool ParseFloat(char const* s, float& out)
    {
        char* end = nullptr;
        out = std::strtof(s, &end);
        return end && *end == '\0';
    }

    char const* HeightText(float h, char* buf, std::size_t size, char const* absent = "none")
    {
        if (h <= INVALID_HEIGHT)
            snprintf(buf, size, "%s", absent);
        else
            snprintf(buf, size, "%.3f", h);

        return buf;
    }

    struct Options
    {
        std::string dataDir = "/azerothcore/env/dist/data";
        uint32 mapId = 0;
        bool haveMap = false;
        bool json = false;
        bool straight = false;
        float limit = 0.0f;
        UnitProfile profile;
        std::string command;
        std::vector<float> coords;
    };

    void PrintHeightBreakdown(HeightData const& height, UnitProfile const& profile, float x, float y, float z,
                              bool json)
    {
        char b1[32], b2[32], b3[32];
        float const gridZ = height.GetGridHeight(x, y);
        float const vmapZ = height.GetVmapHeight(x, y, z + std::max(profile.collisionHeight, Z_OFFSET_FIND_HEIGHT));
        float const mapZ = height.GetMapHeightForUnit(profile, x, y, z);
        float groundZ = INVALID_HEIGHT;
        float const settled = height.UpdateAllowedPositionZ(profile, x, y, z, &groundZ);

        if (json)
        {
            printf("  \"terrainZ\": %s,\n", HeightText(gridZ, b1, sizeof(b1), "null"));
            printf("  \"vmapZ\": %s,\n", HeightText(vmapZ, b2, sizeof(b2), "null"));
            printf("  \"mapHeight\": %s,\n", HeightText(mapZ, b3, sizeof(b3), "null"));
            printf("  \"updateAllowedPositionZ\": %.3f,\n", settled);
            printf("  \"zDelta\": %.3f,\n", settled - z);
            return;
        }

        printf("  terrain (.map)          %s\n", HeightText(gridZ, b1, sizeof(b1)));
        printf("  vmap (WMO/M2)           %s\n", HeightText(vmapZ, b2, sizeof(b2)));
        printf("  Map::GetHeight          %s\n", HeightText(mapZ, b3, sizeof(b3)));
        printf("  UpdateAllowedPositionZ  %.3f  (dz %+.3f)\n", settled, settled - z);

        if (std::fabs(settled - z) > 1.0f)
            printf("  ** Z would be rewritten by %.1f yards **\n", settled - z);
    }
}

int main(int argc, char** argv)
{
    Options opt;

    for (int i = 1; i < argc; ++i)
    {
        std::string const arg = argv[i];
        auto next = [&](char const* what) -> char const*
        {
            if (i + 1 >= argc)
            {
                printf("error: %s needs a value\n", what);
                std::exit(1);
            }
            return argv[++i];
        };

        if (arg == "--help" || arg == "-h")
        {
            printf("%s", USAGE);
            return 0;
        }
        else if (arg == "--data")
            opt.dataDir = next("--data");
        else if (arg == "--map")
        {
            opt.mapId = uint32(std::atoi(next("--map")));
            opt.haveMap = true;
        }
        else if (arg == "--format")
            opt.json = std::string(next("--format")) == "json";
        else if (arg == "--creature")
            opt.profile.isCreature = true;
        else if (arg == "--can-fly")
            opt.profile.canFly = true;
        else if (arg == "--no-swim")
            opt.profile.canSwim = false;
        else if (arg == "--falling")
            opt.profile.isFalling = true;
        else if (arg == "--hover")
            ParseFloat(next("--hover"), opt.profile.hoverHeight);
        else if (arg == "--collision")
            ParseFloat(next("--collision"), opt.profile.collisionHeight);
        else if (arg == "--nav")
            opt.profile.navFlagsOverride = uint16(std::strtoul(next("--nav"), nullptr, 0));
        else if (arg == "--straight")
            opt.straight = true;
        else if (arg == "--limit")
            ParseFloat(next("--limit"), opt.limit);
        else if (arg == "--slope-check")
            printf("note: --slope-check is not ported (it needs liquid data); ignoring\n");
        else if (arg.rfind("--", 0) == 0)
        {
            printf("error: unknown flag %s\n", arg.c_str());
            return 1;
        }
        else if (opt.command.empty())
            opt.command = arg;
        else
        {
            float value;
            if (!ParseFloat(arg.c_str(), value))
            {
                printf("error: %s is not a number\n", arg.c_str());
                return 1;
            }
            opt.coords.push_back(value);
        }
    }

    if (opt.command.empty() || !opt.haveMap)
    {
        printf("%s", USAGE);
        return 1;
    }

    NavMesh mesh;
    std::string error;
    bool const meshLoaded = mesh.Load(opt.dataDir, opt.mapId, error);

    HeightData height;
    height.Load(opt.dataDir, opt.mapId);

    if (!meshLoaded && opt.command != "coverage")
    {
        printf("error: %s\n", error.c_str());
        return 2;
    }

    if (opt.command == "coverage")
    {
        if (opt.json)
        {
            printf("{\n");
            printf("  \"map\": %u,\n", opt.mapId);
            printf("  \"navmesh\": %s,\n", meshLoaded ? "true" : "false");
            printf("  \"maxTiles\": %d,\n", mesh.HasParams() ? mesh.Params().maxTiles : 0);
            printf("  \"tileFiles\": %u,\n", mesh.TileFilesFound());
            printf("  \"tilesLoaded\": %u,\n", mesh.TilesLoaded());
            printf("  \"mapFiles\": %u,\n", height.GridFilesFound());
            printf("  \"vmapTileFiles\": %u,\n", height.VmapFilesFound());
            printf("  \"vmapTree\": %s,\n", height.HasVmapTree() ? "true" : "false");
            printf("  \"vmapTilesLoaded\": %u\n", height.VmapTilesLoaded());
            printf("}\n");
            return 0;
        }

        printf("map %03u  data %s\n\n", opt.mapId, opt.dataDir.c_str());

        if (!meshLoaded)
            printf("navmesh      NOT LOADED: %s\n", error.c_str());
        else
        {
            dtNavMeshParams const& p = mesh.Params();
            printf("params       orig (%g, %g, %g)  tile %.4f x %.4f\n", p.orig[0], p.orig[1], p.orig[2],
                   p.tileWidth, p.tileHeight);
            // maxPolys is 1 << DT_POLY_BITS, which overflows the int Detour stores it in.
            printf("             maxTiles %d  maxPolys %u\n", p.maxTiles, uint32(p.maxPolys));
            printf("mmtiles      %u on disk, %u loaded\n", mesh.TileFilesFound(), mesh.TilesLoaded());
        }

        if (meshLoaded && mesh.TilesLoaded() == 0)
            printf("             ** no navmesh tiles: every path here short-circuits to a shortcut\n"
                   "                and reports PATHFIND_NORMAL|PATHFIND_NOT_USING_PATH (0x11) **\n");

        if (!mesh.TileCoords().empty())
        {
            int minX = 1 << 30, maxX = -1, minY = 1 << 30, maxY = -1;
            for (auto const& tile : mesh.TileCoords())
            {
                minX = std::min(minX, tile.first);
                maxX = std::max(maxX, tile.first);
                minY = std::min(minY, tile.second);
                maxY = std::max(maxY, tile.second);
            }
            printf("             tile span x [%d..%d]  y [%d..%d]\n", minX, maxX, minY, maxY);
        }

        printf("maps         %u .map grid tiles\n", height.GridFilesFound());
        // A grid with no .vmtile still registers as loaded - the tree tracks empty tiles too.
        printf("vmaps        %u .vmtile on disk, tree %s, %u grid tiles registered\n",
               height.VmapFilesFound(), height.HasVmapTree() ? "present" : "MISSING",
               height.VmapTilesLoaded());

        if (!height.HasVmapTree())
            printf("             ** no vmap tree: WMO/M2 collision is unavailable, so ground height\n"
                   "                comes from the raw .map surface alone **\n");

        for (std::string const& warning : mesh.Warnings())
            printf("warning      %s\n", warning.c_str());

        return 0;
    }

    if (opt.command == "point")
    {
        if (opt.coords.size() != 3)
        {
            printf("error: point needs X Y Z\n");
            return 1;
        }

        G3D::Vector3 const p(opt.coords[0], opt.coords[1], opt.coords[2]);
        NavQuery query(mesh, height, opt.profile);

        float point[VERTEX_SIZE] = { p.y, p.z, p.x };
        float closest[VERTEX_SIZE] = { 0.0f, 0.0f, 0.0f };
        float dist = 0.0f;
        bool const haveTile = query.HaveTile(p);
        dtPolyRef const poly = haveTile ? query.GetPolyByLocation(point, &dist, closest) : INVALID_POLYREF;

        int gx, gy;
        ComputeGridCoord(p.x, p.y, gx, gy);

        if (opt.json)
        {
            printf("{\n");
            printf("  \"map\": %u,\n", opt.mapId);
            printf("  \"x\": %.3f, \"y\": %.3f, \"z\": %.3f,\n", p.x, p.y, p.z);
            printf("  \"grid\": [%d, %d],\n", gx, gy);
            printf("  \"haveTile\": %s,\n", haveTile ? "true" : "false");
            printf("  \"onMesh\": %s,\n", poly != INVALID_POLYREF ? "true" : "false");
            printf("  \"polyRef\": %llu,\n", (unsigned long long)poly);
            if (poly != INVALID_POLYREF)
            {
                printf("  \"distToPoly\": %.3f,\n", dist);
                printf("  \"meshZ\": %.3f,\n", closest[1]);
            }
            PrintHeightBreakdown(height, opt.profile, p.x, p.y, p.z, true);
            printf("  \"profile\": \"%s\"\n", opt.profile.Describe().c_str());
            printf("}\n");
            return 0;
        }

        printf("map %03u  point (%.3f, %.3f, %.3f)  grid [%d,%d]\n", opt.mapId, p.x, p.y, p.z, gx, gy);
        printf("profile      %s\n\n", opt.profile.Describe().c_str());

        printf("navmesh\n");
        printf("  tile loaded           %s\n", haveTile ? "yes" : "NO");
        if (!haveTile)
            printf("  -> CalculatePath short-circuits to BuildShortcut here\n");
        else if (poly == INVALID_POLYREF)
            printf("  nearest poly          NONE within {3,50,3} - OFF MESH\n");
        else
        {
            printf("  nearest poly          0x%llx\n", (unsigned long long)poly);
            printf("  distance to poly      %.3f%s\n", dist, dist > 7.0f ? "  (> 7.0, FARFROMPOLY)" : "");
            printf("  mesh surface Z        %.3f  (dz %+.3f)\n", closest[1], closest[1] - p.z);
        }

        printf("\nheight\n");
        PrintHeightBreakdown(height, opt.profile, p.x, p.y, p.z, false);

        if (!height.HasVmapTree())
            printf("  (no vmaps on this map - terrain is the only surface)\n");

        printf("\nliquid is not modelled offline, and GameObject collision needs a live server.\n");
        return 0;
    }

    if (opt.command == "path")
    {
        if (opt.coords.size() != 6)
        {
            printf("error: path needs X1 Y1 Z1 X2 Y2 Z2\n");
            return 1;
        }

        G3D::Vector3 const start(opt.coords[0], opt.coords[1], opt.coords[2]);
        G3D::Vector3 const end(opt.coords[3], opt.coords[4], opt.coords[5]);

        NavQuery query(mesh, height, opt.profile);
        query.SetUseStraightPath(opt.straight);
        if (opt.limit > 0.0f)
            query.SetPathLengthLimit(opt.limit);

        if (!query.CalculatePath(start, end))
        {
            printf("error: coordinates are outside the map\n");
            return 1;
        }

        uint32 const type = query.GetPathType();

        if (opt.json)
        {
            printf("{\n");
            printf("  \"map\": %u,\n", opt.mapId);
            printf("  \"type\": %u,\n", type);
            printf("  \"typeHex\": \"0x%02x\",\n", type);
            printf("  \"typeNames\": \"%s\",\n", DescribePathType(type).c_str());
            printf("  \"polyLength\": %u,\n", query.GetPolyLength());
            printf("  \"points\": %zu,\n", query.GetPath().size());
            printf("  \"length\": %.3f,\n", query.GetPathLength());
            printf("  \"distToStartPoly\": %.3f,\n", query.GetDistToStartPoly());
            printf("  \"distToEndPoly\": %.3f,\n", query.GetDistToEndPoly());
            printf("  \"actualEnd\": [%.3f, %.3f, %.3f],\n", query.GetActualEndPosition().x,
                   query.GetActualEndPosition().y, query.GetActualEndPosition().z);
            printf("  \"waypoints\": [\n");
            for (std::size_t i = 0; i < query.GetPath().size(); ++i)
            {
                G3D::Vector3 const& v = query.GetPath()[i];
                printf("    [%.3f, %.3f, %.3f]%s\n", v.x, v.y, v.z,
                       i + 1 < query.GetPath().size() ? "," : "");
            }
            printf("  ]\n}\n");
            return 0;
        }

        printf("map %03u  %s\n", opt.mapId, opt.straight ? "findStraightPath" : "smooth path");
        printf("profile      %s\n\n", opt.profile.Describe().c_str());
        printf("from         (%.3f, %.3f, %.3f)\n", start.x, start.y, start.z);
        printf("to           (%.3f, %.3f, %.3f)\n", end.x, end.y, end.z);
        printf("actual end   (%.3f, %.3f, %.3f)\n\n", query.GetActualEndPosition().x,
               query.GetActualEndPosition().y, query.GetActualEndPosition().z);

        printf("TYPE         0x%02x  %s\n", type, DescribePathType(type).c_str());
        printf("polys        %u from findPath\n", query.GetFindPathPolys());
        printf("points       %zu\n", query.GetPath().size());
        printf("length       %.2f yards\n", query.GetPathLength());
        printf("start poly   %.3f away%s\n", query.GetDistToStartPoly(),
               query.GetDistToStartPoly() > 7.0f ? "  (FARFROMPOLY)" : "");
        printf("end poly     %.3f away%s\n", query.GetDistToEndPoly(),
               query.GetDistToEndPoly() > 7.0f ? "  (FARFROMPOLY)" : "");

        if (!query.GetNote().empty())
            printf("\nnote: %s\n", query.GetNote().c_str());

        if (type & PATHFIND_NOT_USING_PATH)
            printf("\n0x10 is set: this is a straight line, not a navmesh path. Read the type as a\n"
                   "mask - 0x%02x equals neither PATHFIND_NORMAL nor PATHFIND_INCOMPLETE.\n", type);

        printf("\nwaypoints\n");
        for (std::size_t i = 0; i < query.GetPath().size(); ++i)
        {
            G3D::Vector3 const& v = query.GetPath()[i];
            printf("  %3zu  %10.3f %10.3f %10.3f\n", i, v.x, v.y, v.z);
        }

        return 0;
    }

    if (opt.command == "ring")
    {
        if (opt.coords.size() != 5)
        {
            printf("error: ring needs X Y Z RADIUS HEADINGS\n");
            return 1;
        }

        float const cx = opt.coords[0];
        float const cy = opt.coords[1];
        float const cz = opt.coords[2];
        float const radius = opt.coords[3];
        int const headings = int(opt.coords[4]);

        if (headings <= 0)
        {
            printf("error: HEADINGS must be positive\n");
            return 1;
        }

        NavQuery query(mesh, height, opt.profile);
        int onMesh = 0;

        if (opt.json)
            printf("{\n  \"map\": %u,\n  \"points\": [\n", opt.mapId);
        else
        {
            printf("map %03u  ring around (%.3f, %.3f, %.3f)  radius %.2f  %d headings\n",
                   opt.mapId, cx, cy, cz, radius, headings);
            printf("profile      %s\n\n", opt.profile.Describe().c_str());
            printf("  deg          x          y          z   tile   poly    dist    meshZ   settledZ\n");
        }

        for (int i = 0; i < headings; ++i)
        {
            float const angle = float(2.0 * M_PI * i / headings);
            float const x = cx + radius * std::cos(angle);
            float const y = cy + radius * std::sin(angle);
            G3D::Vector3 const p(x, y, cz);

            float point[VERTEX_SIZE] = { p.y, p.z, p.x };
            float closest[VERTEX_SIZE] = { 0.0f, 0.0f, 0.0f };
            float dist = 0.0f;
            bool const haveTile = query.HaveTile(p);
            dtPolyRef const poly = haveTile ? query.GetPolyByLocation(point, &dist, closest) : INVALID_POLYREF;
            float const settled = height.UpdateAllowedPositionZ(opt.profile, x, y, cz);

            if (poly != INVALID_POLYREF)
                ++onMesh;

            if (opt.json)
            {
                printf("    { \"deg\": %.1f, \"x\": %.3f, \"y\": %.3f, \"z\": %.3f, \"tile\": %s, "
                       "\"onMesh\": %s, \"distToPoly\": %.3f, \"settledZ\": %.3f }%s\n",
                       angle * 180.0f / float(M_PI), x, y, cz, haveTile ? "true" : "false",
                       poly != INVALID_POLYREF ? "true" : "false", dist, settled,
                       i + 1 < headings ? "," : "");
                continue;
            }

            char distText[16] = "       -";
            char meshText[16] = "       -";
            if (poly != INVALID_POLYREF)
            {
                snprintf(distText, sizeof(distText), "%8.2f", dist);
                snprintf(meshText, sizeof(meshText), "%8.3f", closest[1]);
            }

            printf("%5.0f %10.3f %10.3f %10.3f %6s %6s %s %s %10.3f\n",
                   angle * 180.0f / float(M_PI), x, y, cz,
                   haveTile ? "yes" : "NO",
                   poly != INVALID_POLYREF ? "yes" : "OFF",
                   distText, meshText, settled);
        }

        if (opt.json)
            printf("  ],\n  \"onMesh\": %d,\n  \"total\": %d\n}\n", onMesh, headings);
        else
            printf("\n%d/%d on mesh\n", onMesh, headings);

        return 0;
    }

    printf("error: unknown command %s\n\n%s", opt.command.c_str(), USAGE);
    return 1;
}
