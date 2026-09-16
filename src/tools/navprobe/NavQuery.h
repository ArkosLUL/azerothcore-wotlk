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

#ifndef _NAVPROBE_NAVQUERY_H
#define _NAVPROBE_NAVQUERY_H

#include "DetourExtended.h"
#include "NavData.h"
#include <G3D/Vector3.h>
#include <string>
#include <vector>

namespace navprobe
{
    // Mirrors PathGenerator.h. Values are load-bearing: they are what the server reports.
    constexpr uint32 MAX_PATH_LENGTH       = 74;
    constexpr uint32 MAX_POINT_PATH_LENGTH = 74;
    constexpr float SMOOTH_PATH_STEP_SIZE  = 4.0f;
    constexpr float SMOOTH_PATH_SLOP       = 0.3f;
    constexpr int VERTEX_SIZE              = 3;
    constexpr dtPolyRef INVALID_POLYREF    = 0;

    enum PathType
    {
        PATHFIND_BLANK             = 0x00,
        PATHFIND_NORMAL            = 0x01,
        PATHFIND_SHORTCUT          = 0x02,
        PATHFIND_INCOMPLETE        = 0x04,
        PATHFIND_NOPATH            = 0x08,
        PATHFIND_NOT_USING_PATH    = 0x10,
        PATHFIND_SHORT             = 0x20,
        PATHFIND_FARFROMPOLY_START = 0x40,
        PATHFIND_FARFROMPOLY_END   = 0x80,
    };

    // Decodes the mask into flag names. Always read a path type as a mask - PATHFIND_NORMAL |
    // PATHFIND_NOT_USING_PATH is 0x11 and equals neither constant on its own.
    std::string DescribePathType(uint32 type);

    // PathGenerator for a cold query: no prior corridor to reuse, and no live Unit to ask. Corridor
    // reuse, raycast mode and the slope gate are not ported - see README.md.
    class NavQuery
    {
    public:
        NavQuery(NavMesh const& mesh, HeightData const& height, UnitProfile const& profile);

        void SetUseStraightPath(bool useStraightPath) { _useStraightPath = useStraightPath; }
        void SetPathLengthLimit(float distance);

        // false only when the coordinates are not valid map coordinates.
        bool CalculatePath(G3D::Vector3 const& start, G3D::Vector3 const& end);

        [[nodiscard]] bool HaveTile(G3D::Vector3 const& p) const;
        // findNearestPoly with PathGenerator's extents, the {3,50,3} retry included.
        [[nodiscard]] dtPolyRef GetPolyByLocation(float const* point, float* distance, float* closestPoint = nullptr) const;

        [[nodiscard]] uint32 GetPathType() const { return _type; }
        [[nodiscard]] uint32 GetPolyLength() const { return _polyLength; }
        [[nodiscard]] std::vector<G3D::Vector3> const& GetPath() const { return _pathPoints; }
        [[nodiscard]] G3D::Vector3 const& GetStartPosition() const { return _startPosition; }
        [[nodiscard]] G3D::Vector3 const& GetEndPosition() const { return _endPosition; }
        [[nodiscard]] G3D::Vector3 const& GetActualEndPosition() const { return _actualEndPosition; }
        [[nodiscard]] float GetDistToStartPoly() const { return _distToStartPoly; }
        [[nodiscard]] float GetDistToEndPoly() const { return _distToEndPoly; }
        [[nodiscard]] float GetPathLength() const;
        // What findPath returned, before a later BuildShortcut wiped the corridor.
        [[nodiscard]] uint32 GetFindPathPolys() const { return _findPathPolys; }
        [[nodiscard]] std::string const& GetNote() const { return _note; }

    private:
        void Clear();
        void SetStartPosition(G3D::Vector3 const& point) { _startPosition = point; }
        void SetEndPosition(G3D::Vector3 const& point) { _actualEndPosition = point; _endPosition = point; }
        void SetActualEndPosition(G3D::Vector3 const& point) { _actualEndPosition = point; }

        void BuildPolyPath(G3D::Vector3 const& startPos, G3D::Vector3 const& endPos);
        void BuildPointPath(float const* startPoint, float const* endPoint);
        void BuildShortcut();
        void NormalizePath();
        void AddFarFromPolyFlags(bool startFarFromPoly, bool endFarFromPoly);

        [[nodiscard]] bool InRange(G3D::Vector3 const& p1, G3D::Vector3 const& p2, float r, float h) const;
        [[nodiscard]] float Dist3DSqr(G3D::Vector3 const& p1, G3D::Vector3 const& p2) const;
        [[nodiscard]] bool InRangeYZX(float const* v1, float const* v2, float r, float h) const;

        uint32 FixupCorridor(dtPolyRef* path, uint32 npath, uint32 maxPath, dtPolyRef const* visited, uint32 nvisited);
        bool GetSteerTarget(float const* startPos, float const* endPos, float minTargetDist, dtPolyRef const* path,
                            uint32 pathSize, float* steerPos, unsigned char& steerPosFlag, dtPolyRef& steerPosRef);
        dtStatus FindSmoothPath(float const* startPos, float const* endPos, dtPolyRef const* polyPath,
                                uint32 polyPathSize, float* smoothPath, int* smoothPathSize, uint32 maxSmoothPathSize);

        HeightData const& _height;
        UnitProfile _profile;

        dtNavMesh const* _navMesh;
        dtNavMeshQuery const* _navMeshQuery;
        dtQueryFilterExt _filter;

        dtPolyRef _pathPolyRefs[MAX_PATH_LENGTH] {};
        uint32 _polyLength = 0;
        std::vector<G3D::Vector3> _pathPoints;
        uint32 _type = PATHFIND_BLANK;

        bool _useStraightPath = false;
        uint32 _pointPathLimit = MAX_POINT_PATH_LENGTH;

        G3D::Vector3 _startPosition;
        G3D::Vector3 _endPosition;
        G3D::Vector3 _actualEndPosition;

        float _distToStartPoly = 0.0f;
        float _distToEndPoly = 0.0f;
        uint32 _findPathPolys = 0;
        std::string _note;
    };
}

#endif // _NAVPROBE_NAVQUERY_H
