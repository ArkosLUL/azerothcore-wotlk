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

#include "NavQuery.h"
#include "DetourCommon.h"
#include "MapDefines.h"
#include "StringFormat.h"
#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstring>

namespace navprobe
{
namespace
{
    constexpr float MAP_HALFSIZE = SIZE_OF_GRIDS * MAX_NUMBER_OF_GRIDS / 2.0f;

    bool IsValidMapCoord(float c)
    {
        return std::isfinite(c) && (std::fabs(c) <= MAP_HALFSIZE - 0.5f);
    }

    bool IsValidMapCoord(G3D::Vector3 const& p)
    {
        return IsValidMapCoord(p.x) && IsValidMapCoord(p.y) && IsValidMapCoord(p.z);
    }
}

std::string DescribePathType(uint32 type)
{
    if (type == PATHFIND_BLANK)
        return "PATHFIND_BLANK";

    static std::pair<uint32, char const*> const names[] =
    {
        { PATHFIND_NORMAL,            "PATHFIND_NORMAL" },
        { PATHFIND_SHORTCUT,          "PATHFIND_SHORTCUT" },
        { PATHFIND_INCOMPLETE,        "PATHFIND_INCOMPLETE" },
        { PATHFIND_NOPATH,            "PATHFIND_NOPATH" },
        { PATHFIND_NOT_USING_PATH,    "PATHFIND_NOT_USING_PATH" },
        { PATHFIND_SHORT,             "PATHFIND_SHORT" },
        { PATHFIND_FARFROMPOLY_START, "PATHFIND_FARFROMPOLY_START" },
        { PATHFIND_FARFROMPOLY_END,   "PATHFIND_FARFROMPOLY_END" },
    };

    std::string out;
    for (auto const& entry : names)
        if (type & entry.first)
        {
            if (!out.empty())
                out += "|";
            out += entry.second;
        }

    return out.empty() ? "?" : out;
}

NavQuery::NavQuery(NavMesh const& mesh, HeightData const& height, UnitProfile const& profile)
    : _height(height), _profile(profile), _navMesh(mesh.Mesh()), _navMeshQuery(mesh.Query())
{
    // PathGenerator::CreateFilter. UpdateFilter only ever widens this for a unit standing in liquid,
    // which is not modelled offline.
    _filter.setIncludeFlags(_profile.IncludeFlags());
    _filter.setExcludeFlags(0);
}

void NavQuery::SetPathLengthLimit(float distance)
{
    _pointPathLimit = std::min<uint32>(uint32(distance / SMOOTH_PATH_STEP_SIZE), MAX_POINT_PATH_LENGTH);
}

void NavQuery::Clear()
{
    _polyLength = 0;
    _pathPoints.clear();
}

float NavQuery::GetPathLength() const
{
    float len = 0.0f;
    if (_pathPoints.empty())
        return len;

    G3D::Vector3 prev = _startPosition;
    for (G3D::Vector3 const& point : _pathPoints)
    {
        len += (point - prev).length();
        prev = point;
    }

    return len;
}

bool NavQuery::InRange(G3D::Vector3 const& p1, G3D::Vector3 const& p2, float r, float h) const
{
    G3D::Vector3 d = p1 - p2;
    return (d.x * d.x + d.y * d.y) < r * r && std::fabs(d.z) < h;
}

float NavQuery::Dist3DSqr(G3D::Vector3 const& p1, G3D::Vector3 const& p2) const
{
    return (p1 - p2).squaredLength();
}

bool NavQuery::InRangeYZX(float const* v1, float const* v2, float r, float h) const
{
    float const dx = v2[0] - v1[0];
    float const dy = v2[1] - v1[1]; // elevation
    float const dz = v2[2] - v1[2];
    return (dx * dx + dz * dz) < r * r && std::fabs(dy) < h;
}

void NavQuery::AddFarFromPolyFlags(bool startFarFromPoly, bool endFarFromPoly)
{
    if (startFarFromPoly)
        _type |= PATHFIND_FARFROMPOLY_START;
    if (endFarFromPoly)
        _type |= PATHFIND_FARFROMPOLY_END;
}

bool NavQuery::HaveTile(G3D::Vector3 const& p) const
{
    if (!_navMesh)
        return false;

    int tx = -1, ty = -1;
    float point[VERTEX_SIZE] = { p.y, p.z, p.x };

    _navMesh->calcTileLoc(point, &tx, &ty);

    // calcTileLoc can hand back negative coords; getTileAt would crash on them.
    if (tx < 0 || ty < 0)
        return false;

    return (_navMesh->getTileAt(tx, ty, 0) != nullptr);
}

dtPolyRef NavQuery::GetPolyByLocation(float const* point, float* distance, float* closestOut) const
{
    dtPolyRef polyRef = INVALID_POLYREF;

    // No cached corridor offline, so GetPathPolyByPosition never applies - straight to findNearestPoly.
    float extents[VERTEX_SIZE] = { 3.0f, 5.0f, 3.0f };
    float closestPoint[VERTEX_SIZE] = { 0.0f, 0.0f, 0.0f };
    if (dtStatusSucceed(_navMeshQuery->findNearestPoly(point, extents, &_filter, &polyRef, closestPoint)) &&
        polyRef != INVALID_POLYREF)
    {
        *distance = dtVdist(closestPoint, point);
        if (closestOut)
            dtVcopy(closestOut, closestPoint);

        return polyRef;
    }

    // The extent must not overlap more than 128 polygons - see dtNavMeshQuery::findNearestPoly.
    extents[1] = 50.0f;

    if (dtStatusSucceed(_navMeshQuery->findNearestPoly(point, extents, &_filter, &polyRef, closestPoint)) &&
        polyRef != INVALID_POLYREF)
    {
        *distance = dtVdist(closestPoint, point);
        if (closestOut)
            dtVcopy(closestOut, closestPoint);

        return polyRef;
    }

    *distance = FLT_MAX;
    return INVALID_POLYREF;
}

bool NavQuery::CalculatePath(G3D::Vector3 const& start, G3D::Vector3 const& end)
{
    if (!IsValidMapCoord(start) || !IsValidMapCoord(end))
        return false;

    Clear();
    _type = PATHFIND_BLANK;
    _distToStartPoly = 0.0f;
    _distToEndPoly = 0.0f;
    _findPathPolys = 0;
    _note.clear();

    SetEndPosition(end);
    SetStartPosition(start);

    if (!_navMesh || !_navMeshQuery || !HaveTile(start) || !HaveTile(end))
    {
        BuildShortcut();
        _type = PATHFIND_NORMAL | PATHFIND_NOT_USING_PATH;
        return true;
    }

    BuildPolyPath(start, end);
    return true;
}

void NavQuery::BuildPolyPath(G3D::Vector3 const& startPos, G3D::Vector3 const& endPos)
{
    float startPoint[VERTEX_SIZE] = { startPos.y, startPos.z, startPos.x };
    float endPoint[VERTEX_SIZE] = { endPos.y, endPos.z, endPos.x };

    dtPolyRef startPoly = GetPolyByLocation(startPoint, &_distToStartPoly);
    dtPolyRef endPoly = GetPolyByLocation(endPoint, &_distToEndPoly);

    _type = PATHFIND_NORMAL;

    // A hole in the mesh. Note the asymmetry in the core: this branch reads CanFly off a Creature
    // and assumes true for anything else, so a player never gets NOPATH here. The far-from-poly
    // branch below asks the Unit instead. The water half needs liquid data and is always false here.
    if (startPoly == INVALID_POLYREF || endPoly == INVALID_POLYREF)
    {
        BuildShortcut();

        bool const passesAnyway = _profile.isCreature ? _profile.canFly : true;
        if (passesAnyway)
        {
            _type = PATHFIND_NORMAL | PATHFIND_NOT_USING_PATH;
            return;
        }

        _type = PATHFIND_NOPATH;
        return;
    }

    bool const startFarFromPoly = _distToStartPoly > 7.0f;
    bool const endFarFromPoly = _distToEndPoly > 7.0f;

    if (startFarFromPoly || endFarFromPoly)
    {
        // The server also shortcuts when a swimmer moves between two submerged points. That needs
        // liquid data, so offline only the fly and fall branches can fire.
        bool const buildShortcut = _profile.canFly || (_profile.isFalling && endPos.z < startPos.z);

        if (buildShortcut)
        {
            BuildShortcut();
            _type = PATHFIND_NORMAL | PATHFIND_NOT_USING_PATH;
            AddFarFromPolyFlags(startFarFromPoly, endFarFromPoly);
            return;
        }

        float closestPoint[VERTEX_SIZE];
        if (dtStatusSucceed(_navMeshQuery->closestPointOnPoly(endPoly, endPoint, closestPoint, nullptr)))
        {
            dtVcopy(endPoint, closestPoint);
            SetActualEndPosition(G3D::Vector3(endPoint[2], endPoint[0], endPoint[1]));
        }

        _type = PATHFIND_INCOMPLETE;
        AddFarFromPolyFlags(startFarFromPoly, endFarFromPoly);
    }

    // Start and end on the same polygon: treat it as two polygons and split the line into points.
    if (startPoly == endPoly)
    {
        _pathPolyRefs[0] = startPoly;
        _polyLength = 1;
        _findPathPolys = 1;

        if (startFarFromPoly || endFarFromPoly)
        {
            _type = PATHFIND_INCOMPLETE;
            AddFarFromPolyFlags(startFarFromPoly, endFarFromPoly);
        }
        else
            _type = PATHFIND_NORMAL;

        BuildPointPath(startPoint, endPoint);
        return;
    }

    dtStatus const dtResult = _navMeshQuery->findPath(
        startPoly,
        endPoly,
        startPoint,
        endPoint,
        &_filter,
        _pathPolyRefs,
        (int*)&_polyLength,
        MAX_PATH_LENGTH);

    if (!_polyLength || dtStatusFailed(dtResult))
    {
        BuildShortcut();
        _type = PATHFIND_NOPATH;
        return;
    }

    _findPathPolys = _polyLength;

    if (_pathPolyRefs[_polyLength - 1] == endPoly && !(_type & PATHFIND_INCOMPLETE))
        _type = PATHFIND_NORMAL;
    else
        _type = PATHFIND_INCOMPLETE;

    AddFarFromPolyFlags(startFarFromPoly, endFarFromPoly);

    BuildPointPath(startPoint, endPoint);
}

void NavQuery::BuildPointPath(float const* startPoint, float const* endPoint)
{
    float pathPoints[MAX_POINT_PATH_LENGTH * VERTEX_SIZE];
    uint32 pointCount = 0;
    dtStatus dtResult = DT_FAILURE;

    if (_useStraightPath)
    {
        dtResult = _navMeshQuery->findStraightPath(
            startPoint,
            endPoint,
            _pathPolyRefs,
            _polyLength,
            pathPoints,
            nullptr,
            nullptr,
            (int*)&pointCount,
            _pointPathLimit);
    }
    else
    {
        dtResult = FindSmoothPath(
            startPoint,
            endPoint,
            _pathPolyRefs,
            _polyLength,
            pathPoints,
            (int*)&pointCount,
            _pointPathLimit);
    }

    // Start and end very close together: the first point is the start, so append the end.
    if (_polyLength == 1 && pointCount == 1)
    {
        dtVcopy(&pathPoints[1 * VERTEX_SIZE], endPoint);
        pointCount++;
    }
    else if (pointCount < 2 || dtStatusFailed(dtResult))
    {
        // FindSmoothPath fails outright once it fills its point budget, so a long but perfectly
        // walkable route comes back NOPATH rather than SHORT. Worth spelling out - it is the most
        // confusing verdict the server hands out.
        if (!_useStraightPath && pointCount >= _pointPathLimit)
            _note = Acore::StringFormat(
                "smooth path filled its {}-point budget ({:.0f} yards at {:.1f}/step); anything "
                "longer is reported NOPATH even when the corridor exists",
                _pointPathLimit, _pointPathLimit * SMOOTH_PATH_STEP_SIZE, SMOOTH_PATH_STEP_SIZE);

        BuildShortcut();
        _type |= PATHFIND_NOPATH;
        return;
    }
    else if (pointCount >= _pointPathLimit)
    {
        BuildShortcut();
        _type |= PATHFIND_SHORT;
        return;
    }

    _pathPoints.resize(pointCount);
    for (uint32 i = 0; i < pointCount; ++i)
        _pathPoints[i] = G3D::Vector3(pathPoints[i * VERTEX_SIZE + 2], pathPoints[i * VERTEX_SIZE],
                                      pathPoints[i * VERTEX_SIZE + 1]);

    NormalizePath();

    SetActualEndPosition(_pathPoints[pointCount - 1]);
}

void NavQuery::NormalizePath()
{
    // The server also snaps to the Blade's Edge Arena ropes here; that special case is not ported.
    for (G3D::Vector3& point : _pathPoints)
        point.z = _height.UpdateAllowedPositionZ(_profile, point.x, point.y, point.z);
}

void NavQuery::BuildShortcut()
{
    Clear();

    _pathPoints.resize(2);
    _pathPoints[0] = _startPosition;
    _pathPoints[1] = _actualEndPosition;

    NormalizePath();

    _type = PATHFIND_SHORTCUT;
}

uint32 NavQuery::FixupCorridor(dtPolyRef* path, uint32 npath, uint32 maxPath, dtPolyRef const* visited, uint32 nvisited)
{
    int32 furthestPath = -1;
    int32 furthestVisited = -1;

    // Find furthest common polygon.
    for (int32 i = npath - 1; i >= 0; --i)
    {
        bool found = false;
        for (int32 j = nvisited - 1; j >= 0; --j)
        {
            if (path[i] == visited[j])
            {
                furthestPath = i;
                furthestVisited = j;
                found = true;
            }
        }
        if (found)
            break;
    }

    if (furthestPath == -1 || furthestVisited == -1)
        return npath;

    uint32 req = nvisited - furthestVisited;
    uint32 orig = uint32(furthestPath + 1) < npath ? furthestPath + 1 : npath;
    uint32 size = npath > orig ? npath - orig : 0;
    if (req + size > maxPath)
        size = maxPath - req;

    if (size)
        memmove(path + req, path + orig, size * sizeof(dtPolyRef));

    for (uint32 i = 0; i < req; ++i)
        path[i] = visited[(nvisited - 1) - i];

    return req + size;
}

bool NavQuery::GetSteerTarget(float const* startPos, float const* endPos, float minTargetDist,
                              dtPolyRef const* path, uint32 pathSize, float* steerPos,
                              unsigned char& steerPosFlag, dtPolyRef& steerPosRef)
{
    static const uint32 MAX_STEER_POINTS = 3;
    float steerPath[MAX_STEER_POINTS * VERTEX_SIZE];
    unsigned char steerPathFlags[MAX_STEER_POINTS];
    dtPolyRef steerPathPolys[MAX_STEER_POINTS];
    uint32 nsteerPath = 0;
    dtStatus dtResult = _navMeshQuery->findStraightPath(startPos, endPos, path, pathSize,
        steerPath, steerPathFlags, steerPathPolys, (int*)&nsteerPath, MAX_STEER_POINTS);
    if (!nsteerPath || dtStatusFailed(dtResult))
        return false;

    // Find vertex far enough to steer to.
    uint32 ns = 0;
    while (ns < nsteerPath)
    {
        if ((steerPathFlags[ns] & DT_STRAIGHTPATH_OFFMESH_CONNECTION) ||
            !InRangeYZX(&steerPath[ns * VERTEX_SIZE], startPos, minTargetDist, 1000.0f))
            break;

        ns++;
    }

    if (ns >= nsteerPath)
        return false;

    dtVcopy(steerPos, &steerPath[ns * VERTEX_SIZE]);
    steerPos[1] = startPos[1];  // keep Z value
    steerPosFlag = steerPathFlags[ns];
    steerPosRef = steerPathPolys[ns];

    return true;
}

dtStatus NavQuery::FindSmoothPath(float const* startPos, float const* endPos, dtPolyRef const* polyPath,
                                  uint32 polyPathSize, float* smoothPath, int* smoothPathSize,
                                  uint32 maxSmoothPathSize)
{
    *smoothPathSize = 0;
    uint32 nsmoothPath = 0;

    dtPolyRef polys[MAX_PATH_LENGTH];
    memcpy(polys, polyPath, sizeof(dtPolyRef) * polyPathSize);
    uint32 npolys = polyPathSize;

    float iterPos[VERTEX_SIZE], targetPos[VERTEX_SIZE];

    if (polyPathSize > 1)
    {
        if (dtStatusFailed(_navMeshQuery->closestPointOnPolyBoundary(polys[0], startPos, iterPos)))
            return DT_FAILURE;

        if (dtStatusFailed(_navMeshQuery->closestPointOnPolyBoundary(polys[npolys - 1], endPos, targetPos)))
            return DT_FAILURE;
    }
    else
    {
        dtVcopy(iterPos, startPos);
        dtVcopy(targetPos, endPos);
    }

    dtVcopy(&smoothPath[nsmoothPath * VERTEX_SIZE], iterPos);
    nsmoothPath++;

    while (npolys && nsmoothPath < maxSmoothPathSize)
    {
        float steerPos[VERTEX_SIZE];
        unsigned char steerPosFlag;
        dtPolyRef steerPosRef = INVALID_POLYREF;

        if (!GetSteerTarget(iterPos, targetPos, SMOOTH_PATH_SLOP, polys, npolys, steerPos, steerPosFlag, steerPosRef))
            break;

        bool endOfPath = (steerPosFlag & DT_STRAIGHTPATH_END) != 0;
        bool offMeshConnection = (steerPosFlag & DT_STRAIGHTPATH_OFFMESH_CONNECTION) != 0;

        float delta[VERTEX_SIZE];
        dtVsub(delta, steerPos, iterPos);
        float len = dtMathSqrtf(dtVdot(delta, delta));
        if ((endOfPath || offMeshConnection) && len < SMOOTH_PATH_STEP_SIZE)
            len = 1.0f;
        else
            len = SMOOTH_PATH_STEP_SIZE / len;

        float moveTgt[VERTEX_SIZE];
        dtVmad(moveTgt, iterPos, delta, len);

        float result[VERTEX_SIZE];
        const static uint32 MAX_VISIT_POLY = 16;
        dtPolyRef visited[MAX_VISIT_POLY];

        uint32 nvisited = 0;
        if (dtStatusFailed(_navMeshQuery->moveAlongSurface(polys[0], iterPos, moveTgt, &_filter, result,
                                                           visited, (int*)&nvisited, MAX_VISIT_POLY)))
            return DT_FAILURE;

        npolys = FixupCorridor(polys, npolys, MAX_PATH_LENGTH, visited, nvisited);

        _navMeshQuery->getPolyHeight(polys[0], result, &result[1]);
        result[1] += 0.5f;
        dtVcopy(iterPos, result);

        if (endOfPath && InRangeYZX(iterPos, steerPos, SMOOTH_PATH_SLOP, 1.0f))
        {
            dtVcopy(iterPos, targetPos);
            if (nsmoothPath < maxSmoothPathSize)
            {
                dtVcopy(&smoothPath[nsmoothPath * VERTEX_SIZE], iterPos);
                nsmoothPath++;
            }
            break;
        }
        else if (offMeshConnection && InRangeYZX(iterPos, steerPos, SMOOTH_PATH_SLOP, 1.0f))
        {
            // Advance the path up to and over the off-mesh connection.
            dtPolyRef prevRef = INVALID_POLYREF;
            dtPolyRef polyRef = polys[0];
            uint32 npos = 0;
            while (npos < npolys && polyRef != steerPosRef)
            {
                prevRef = polyRef;
                polyRef = polys[npos];
                npos++;
            }

            for (uint32 i = npos; i < npolys; ++i)
                polys[i - npos] = polys[i];

            npolys -= npos;

            float connectionStartPos[VERTEX_SIZE], connectionEndPos[VERTEX_SIZE];
            if (dtStatusSucceed(_navMesh->getOffMeshConnectionPolyEndPoints(prevRef, polyRef, connectionStartPos,
                                                                            connectionEndPos)))
            {
                if (nsmoothPath < maxSmoothPathSize)
                {
                    dtVcopy(&smoothPath[nsmoothPath * VERTEX_SIZE], connectionStartPos);
                    nsmoothPath++;
                }

                dtVcopy(iterPos, connectionEndPos);
                if (dtStatusFailed(_navMeshQuery->getPolyHeight(polys[0], iterPos, &iterPos[1])))
                    return DT_FAILURE;
                iterPos[1] += 0.5f;
            }
        }

        if (nsmoothPath < maxSmoothPathSize)
        {
            dtVcopy(&smoothPath[nsmoothPath * VERTEX_SIZE], iterPos);
            nsmoothPath++;
        }
    }

    *smoothPathSize = nsmoothPath;

    // Anything at the cap is most likely a loop.
    return nsmoothPath < MAX_POINT_PATH_LENGTH ? DT_SUCCESS : DT_FAILURE;
}
}
