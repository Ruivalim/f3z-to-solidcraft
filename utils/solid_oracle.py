from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from .conversion_errors import ConversionError

logger = logging.getLogger(__name__)

#: Ray direction for the inside test: skew to every axis, so a ray does not
#: run along the faces and edges of an axis-aligned part.
SKEW = np.array([0.5773, 0.2113, 0.7887]) / np.linalg.norm([0.5773, 0.2113, 0.7887])
EPS = 1e-9
#: One triangle of a binary STL file.
STL_RECORD = np.dtype([("normal", "<f4", 3), ("points", "<f4", (3, 3)), ("attribute", "<u2")])
#: Bytes of free text a binary STL file starts with.
STL_HEADER = 80


def dot(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Dot product of each row of one array with the same row of another.

    Args:
    - a: Vectors, one per row.
    - b: Vectors, one per row.

    Returns:
    - One number per row.
    """
    return np.einsum("ij,ij->i", a, b)


class Mesh:
    """
    A solid as triangles, in millimetres.
    """

    def __init__(self, triangles: np.ndarray) -> None:
        """
        Store the triangles as a corner and two edge vectors each.

        Args:
        - triangles: Array of shape (n, 3, 3): n triangles of three points.
        """
        self.a = triangles[:, 0, :]
        self.ab = triangles[:, 1, :] - self.a
        self.ac = triangles[:, 2, :] - self.a

    @classmethod
    def load(cls, path: Path) -> Mesh:
        """
        Read a binary STL file.

        Args:
        - path: The STL file.

        Returns:
        - The mesh the file holds.

        Raises:
        - ConversionError: If the file cannot be read or is cut short.
        """
        try:
            with open(path, "rb") as handle:
                handle.seek(STL_HEADER)
                count = int(np.fromfile(handle, dtype="<u4", count=1)[0])
                data = np.fromfile(handle, dtype=STL_RECORD, count=count)
        except (OSError, IndexError, ValueError) as error:
            logger.error("Could not read the mesh %s: %s", path, error)
            raise ConversionError(f"could not read the mesh {path}: {error}") from error
        if len(data) != count:
            logger.error("The mesh %s holds %d of its %d triangles", path, len(data), count)
            raise ConversionError(f"the mesh {path} is cut short")
        logger.debug("Read %d triangles from %s", count, path)
        return cls(data["points"].astype(float))

    def hits(self, origin, direction) -> np.ndarray:
        """
        Find where a ray crosses the mesh (Moller-Trumbore).

        Args:
        - origin: Where the ray starts.
        - direction: Unit vector the ray runs along.

        Returns:
        - The distance along the ray of each crossing, unsorted.
        """
        origin, direction = np.asarray(origin, float), np.asarray(direction, float)
        p = np.cross(direction, self.ac)
        det = dot(self.ab, p)
        ok = np.abs(det) > EPS
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        t = origin - self.a
        u = dot(t, p) * inv
        q = np.cross(t, self.ab)
        v = (q @ direction) * inv
        distance = dot(self.ac, q) * inv
        return distance[ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (distance > EPS)]

    def inside(self, point) -> bool:
        """
        Tell whether a point is inside the solid.

        Args:
        - point: The point, in millimetres.

        Returns:
        - True when a ray from the point crosses the surface an odd number of times.
        """
        return len(self.hits(point, SKEW)) % 2 == 1

    def first_hit(self, origin, direction) -> float | None:
        """
        Measure how far along a ray the surface is first met.

        Args:
        - origin: Where the ray starts.
        - direction: The way it runs; any length.

        Returns:
        - The distance to the first crossing, or None if the ray meets nothing.
        """
        hits = self.hits(origin, np.asarray(direction, float) / np.linalg.norm(direction))
        return float(hits.min()) if len(hits) else None

    def last_exit(self, origin, direction, limit: float | None = None) -> float | None:
        """
        Measure how far along a ray the material last ends.

        Args:
        - origin: Where the ray starts.
        - direction: The way it runs; any length.
        - limit: Look no further than this distance; None for no limit.

        Returns:
        - The distance at which the ray last leaves material, or None if it never does.
        """
        direction = np.asarray(direction, float) / np.linalg.norm(direction)
        hits = np.sort(self.hits(origin, direction))
        # Crossings alternate in and out; which comes first depends on where the ray starts.
        exits = hits[0::2] if self.inside(origin) else hits[1::2]
        if limit is not None:
            exits = exits[exits <= limit + 1e-6]
        return float(exits.max()) if len(exits) else None

    def edge_distances(self, p: np.ndarray) -> np.ndarray:
        """
        Measure from a point to the nearest edge of every triangle.

        Args:
        - p: The point relative to each triangle's first corner, one row per triangle.

        Returns:
        - One distance per triangle.
        """
        best = np.full(len(self.a), np.inf)
        for start, edge in ((0.0, self.ab), (0.0, self.ac), (self.ab, self.ac - self.ab)):
            rel = p - start
            along = np.clip(dot(rel, edge) / np.maximum(dot(edge, edge), EPS), 0, 1)
            best = np.minimum(best, np.linalg.norm(rel - edge * along[:, None], axis=1))
        return best

    def distance(self, point) -> float:
        """
        Measure from a point to the surface.

        Args:
        - point: The point, in millimetres.

        Returns:
        - The distance to the nearest point of any triangle.
        """
        p = np.asarray(point, float) - self.a
        d1, d2 = dot(self.ab, p), dot(self.ac, p)
        aa, bb, ab = dot(self.ab, self.ab), dot(self.ac, self.ac), dot(self.ab, self.ac)
        det = np.maximum(aa * bb - ab * ab, EPS)
        u = (bb * d1 - ab * d2) / det
        v = (aa * d2 - ab * d1) / det
        # Where the foot of the perpendicular lands on the triangle it is the
        # nearest point; off it, the nearest point is on one of the edges.
        face = (u >= 0) & (v >= 0) & (u + v <= 1)
        best = self.edge_distances(p)
        foot = self.ab * u[:, None] + self.ac * v[:, None]
        best[face] = np.minimum(best[face], np.linalg.norm(p[face] - foot[face], axis=1))
        return float(best.min())
