from __future__ import annotations

import json
import logging
import math
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from ezf3d import model
from ezf3d.asm.brep import Shape

from .conversion_errors import ConversionError

logger = logging.getLogger(__name__)

CM_TO_MM = 10.0
#: Chords an arc is cut into for the inside-a-loop test, per full turn.
ARC_CHORDS = 48
#: What ezf3d raises on a record it cannot read.
EZF3D_ERRORS = (model.Ef3dError, struct.error, ValueError, KeyError, IndexError)


@dataclass
class Curve:
    """
    A sketch line, arc or circle, in the sketch's own millimetres.
    """

    id: str
    kind: str
    construction: bool
    data: dict

    def polyline(self) -> list[tuple[float, float]]:
        """
        Approximate the curve by straight pieces.

        Returns:
        - The points along it, first to last; an arc runs counter-clockwise.
        """
        if self.kind == "line":
            return [(self.data["start"]["u"], self.data["start"]["v"]), (self.data["end"]["u"], self.data["end"]["v"])]
        cu, cv, radius = self.data["center"]["u"], self.data["center"]["v"], self.data["radius"]
        start, end = (0.0, math.tau) if self.kind == "circle" else (self.data["start_angle"], self.data["end_angle"])
        sweep = (end - start) % math.tau or math.tau
        count = max(4, math.ceil(ARC_CHORDS * sweep / math.tau))
        return [(cu + radius * math.cos(start + sweep * i / count), cv + radius * math.sin(start + sweep * i / count)) for i in range(count + 1)]

    def identity(self) -> tuple:
        """
        Describe the curve's shape, so duplicates can be told.

        Returns:
        - A key equal for two curves drawn on top of one another.
        """
        points = self.polyline()
        ends = sorted((round(points[0][0], 5), round(points[0][1], 5), round(points[-1][0], 5), round(points[-1][1], 5)))
        middle = points[len(points) // 2]
        return (self.kind, *ends, round(middle[0], 5), round(middle[1], 5))


@dataclass
class Sketch:
    """
    A sketch: its plane, its curves and the closed chains they form.
    """

    id: str
    origin: np.ndarray
    x_dir: np.ndarray
    y_dir: np.ndarray
    normal: np.ndarray
    curves: dict[str, Curve]
    #: Closed chains of curve ids, as the file lists them.  A curve named twice
    #: in one chain is a slit, not a boundary.
    loops: list[list[str]]
    #: The name it was drawn under in SolidCraft, once it has been.
    drawn_as: str = ""
    #: Its regions as SolidCraft finds them: the sample points in each.
    regions: list[list[tuple[float, float]]] | None = None

    def world(self, point) -> np.ndarray:
        """
        Place a sketch point in the model.

        Args:
        - point: (u, v) in the sketch, millimetres.

        Returns:
        - The same point as model x, y, z.
        """
        return self.origin + point[0] * self.x_dir + point[1] * self.y_dir

    def local(self, point) -> tuple[float, float]:
        """
        Drop a model point onto the sketch.

        Args:
        - point: Model x, y, z in millimetres.

        Returns:
        - Its (u, v) in the sketch.
        """
        offset = np.asarray(point, float) - self.origin
        return float(offset @ self.x_dir), float(offset @ self.y_dir)

    def slits(self) -> set[str]:
        """
        Find the curves that are slits rather than boundaries.

        Returns:
        - Ids of curves a chain names more than once.
        """
        return {name for loop in self.loops for name in loop if loop.count(name) > 1}

    def inside_loop(self, index: int, point) -> bool:
        """
        Tell whether a point is inside one closed chain.

        An even-odd test: a slit is crossed twice and cancels.

        Args:
        - index: Which chain of `loops`.
        - point: (u, v) in the sketch.

        Returns:
        - True when the point is inside the chain.
        """
        x, y = point
        crossings = 0
        for name in self.loops[index]:
            line = self.curves[name].polyline() if name in self.curves else []
            for (x1, y1), (x2, y2) in zip(line, line[1:], strict=False):
                if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                    crossings += 1
        return crossings % 2 == 1

    def sample_points(self, offsets: tuple[float, ...] = (0.15, 1.0, 3.0), per_segment: int = 8) -> list[tuple[float, float]]:
        """
        Scatter points to either side of every curve, close by and further off.

        Every region of a sketch is bounded by curves, so the close ones reach
        every region wider than the smallest offset without working the regions
        out.  The further ones sit clear of the rounding a later fillet leaves
        along a region's edge, which the close ones would mistake for a cut.

        Args:
        - offsets: Distances from the curve, in millimetres.
        - per_segment: Points along each straight line, per side and offset.

        Returns:
        - The points, as (u, v) in the sketch.
        """
        points: list[tuple[float, float]] = []
        seen: set[tuple] = set()
        for curve in self.curves.values():
            if curve.identity() in seen:
                continue
            seen.add(curve.identity())
            line = curve.polyline()
            steps = per_segment if curve.kind == "line" else 1
            for start, end in zip(line, line[1:], strict=False):
                points += points_beside(start, end, steps, offsets)
        return points


def points_beside(start, end, steps: int, offsets: tuple[float, ...]) -> list[tuple[float, float]]:
    """
    Scatter points to either side of one straight piece of a curve.

    Args:
    - start: Where the piece starts, (u, v).
    - end: Where it ends.
    - steps: Points along it, per side and offset.
    - offsets: Distances from it, in millimetres.

    Returns:
    - The points; none for a piece of no length.
    """
    (x1, y1), (x2, y2) = start, end
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1e-9:
        return []
    nx, ny = -(y2 - y1) / length, (x2 - x1) / length
    points = []
    for i in range(steps):
        t = (i + 0.5) / steps
        mx, my = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        for offset in offsets:
            points += [(mx + nx * offset, my + ny * offset), (mx - nx * offset, my - ny * offset)]
    return points


@dataclass
class Feature:
    """
    One entry of the Fusion timeline.
    """

    ordinal: int
    #: Fusion's own label: ``Extrude 2``.
    name: str
    #: ``Sketch``, ``Extrude``, ``Fillet``, ``Hole``, ``WorkPlane``...
    kind: str
    definition: dict
    dependencies: list[str]
    #: The same timeline entry as ezf3d reads it, for its parameters.
    ez: object | None = None

    def parameter(self, role: str):
        """
        Look up the parameter filling one slot of the feature.

        Args:
        - role: The slot's name, such as ``AlongDistance`` or ``Radius``.

        Returns:
        - The ezf3d parameter, or None when the feature has none in that slot.
        """
        return self.ez.role(role) if self.ez is not None else None


@dataclass
class Design:
    """
    One Fusion document, read.
    """

    name: str
    linked: bool
    f3d: Path
    features: list[Feature] = field(default_factory=list)
    sketches: dict[str, Sketch] = field(default_factory=dict)
    feature_ids: dict[str, Feature] = field(default_factory=dict)
    parameters: list = field(default_factory=list)
    #: Cylinders of the finished solid: (point on axis, axis, radius), mm.
    cylinders: list[tuple[np.ndarray, np.ndarray, float]] = field(default_factory=list)
    #: ASM edge slot -> the end points the file records for it, mm.
    edge_ends: dict[int, list[tuple[np.ndarray, np.ndarray]]] = field(default_factory=dict)
    #: (min xyz, max xyz) of the finished bodies' vertices in mm, as ezf3d reads them.
    bounds: tuple[list[float], list[float]] | None = None
    notes: list[str] = field(default_factory=list)


def vector(value: dict) -> np.ndarray:
    """
    Turn a CADIR point or direction into an array.

    Args:
    - value: A mapping with ``x``, ``y`` and ``z``.

    Returns:
    - The three numbers.
    """
    return np.array([value["x"], value["y"], value["z"]], float)


def collect_edges(node, out: dict[int, list]) -> None:
    """
    Gather the end points of every edge the native records place.

    A record with ``edge_slots`` and ``positions`` of equal length describes a
    face boundary: edge i runs from position i to position i + 1.

    Args:
    - node: Any part of the CADIR native section.
    - out: Edge slot -> list of (start, end), added to in place.
    """
    if isinstance(node, dict):
        slots, positions = node.get("edge_slots"), node.get("positions")
        if isinstance(slots, list) and isinstance(positions, list) and slots and len(slots) == len(positions):
            for i, slot in enumerate(slots):
                out.setdefault(slot, []).append((vector(positions[i]), vector(positions[(i + 1) % len(positions)])))
        for value in node.values():
            collect_edges(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_edges(value, out)


def sketch_frame(record: dict, placements: dict[int, list]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Work out where a sketch sits.

    Args:
    - record: The sketch's CADIR record.
    - placements: Sketch number -> Fusion's own 4x4 matrix for it.

    Returns:
    - Origin (mm), x direction, y direction and normal.
    """
    suffix = int(record["id"].rsplit("@", 1)[-1])
    if suffix not in placements:
        placement = record["placement"]
        origin, normal, x_dir = vector(placement["origin"]), vector(placement["normal"]), vector(placement["u_axis"])
        return origin, x_dir, np.cross(normal, x_dir), normal
    # Fusion's matrix is in centimetres: columns x, y, normal, origin.
    matrix = np.array(placements[suffix], float)
    x_dir, y_dir, normal, origin = matrix[:3, 0], matrix[:3, 1], matrix[:3, 2], matrix[:3, 3] * CM_TO_MM
    # Curve coordinates are measured from the model origin dropped onto the
    # plane, not from the matrix's own origin: a sketch on a work plane
    # carries an in-plane offset its curves do not include.
    return (origin @ normal) * normal, x_dir, y_dir, normal


def build_sketch(record: dict, entities: list[dict], placements: dict[int, list]) -> Sketch:
    """
    Build a sketch from its CADIR records.

    Args:
    - record: The sketch's record.
    - entities: The records of its points and curves.
    - placements: Sketch number -> Fusion's own 4x4 matrix for it.

    Returns:
    - The sketch, with its lines, arcs and circles.
    """
    origin, x_dir, y_dir, normal = sketch_frame(record, placements)
    curves = {
        entity["id"]: Curve(entity["id"], entity["geometry"]["kind"], bool(entity.get("construction")), entity["geometry"])
        for entity in entities
        if entity["geometry"]["kind"] in ("line", "arc", "circle")
    }
    loops = [[item["entity"] for item in loop] for loop in record.get("profiles", [])]
    return Sketch(record["id"], origin, x_dir, y_dir, normal, curves, loops)


def dump_cadir(cadmpeg: Path, f3d: Path) -> dict:
    """
    Have cadmpeg decode a Fusion document.

    Args:
    - cadmpeg: The cadmpeg executable.
    - f3d: The Fusion document.

    Returns:
    - cadmpeg's CADIR dump of it, parsed.

    Raises:
    - ConversionError: If cadmpeg cannot be run, fails, or writes something unreadable.
    """
    target = f3d.with_suffix(".cadir.json")
    try:
        subprocess.run([str(cadmpeg), "dump", str(f3d), "-o", str(target), "--force"], capture_output=True, check=True)
        return json.loads(target.read_text(encoding="utf-8"))
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace").strip()
        logger.error("cadmpeg dump of %s failed with code %s: %s", f3d, error.returncode, detail)
        raise ConversionError(f"cadmpeg could not decode {f3d.name}: {detail[-500:]}") from error
    except (OSError, json.JSONDecodeError) as error:
        logger.error("cadmpeg dump of %s could not be run or read: %s", f3d, error)
        raise ConversionError(f"cadmpeg's dump of {f3d.name} could not be run or read: {error}") from error


def read_sketches(design: Design, records: dict, arenas: dict) -> None:
    """
    Fill in a design's sketches.

    Args:
    - design: The design being read; its `sketches` are set.
    - records: The CADIR ``model`` section.
    - arenas: The CADIR native Fusion records.
    """
    placements = {item["entity_suffix"]: item["transform"] for item in arenas.get("design_sketch_placements", [])}
    by_sketch: dict[str, list[dict]] = {}
    for entity in records.get("sketch_entities", []):
        by_sketch.setdefault(entity["sketch"], []).append(entity)
    for record in records.get("sketches", []):
        design.sketches[record["id"]] = build_sketch(record, by_sketch.get(record["id"], []), placements)


def read_parameters(design: Design, doc) -> list:
    """
    Fill in a design's parameters and read its timeline as ezf3d sees it.

    Args:
    - design: The design being read; its `parameters` are set.
    - doc: The document as ezf3d opened it.

    Returns:
    - ezf3d's timeline entries, each with the parameters that drive it; empty
      when the document has no design or ezf3d cannot read it.
    """
    if doc.design is None:
        return []
    try:
        parameters = model.read_parameters(doc.design)
        design.parameters = list(parameters)
        return list(model.read_timeline(doc.design, parameters.values))
    except EZF3D_ERRORS as error:
        logger.error("ezf3d could not read the parameters or timeline of %s: %r", design.name, error)
        design.notes.append(f"ezf3d could not read the parameters ({error}); features use plain values")
        design.parameters = []
        return []


def read_features(design: Design, records: dict, ez_features: list) -> None:
    """
    Fill in a design's timeline.

    Args:
    - design: The design being read; its `features` and `feature_ids` are set.
    - records: The CADIR ``model`` section.
    - ez_features: The same timeline as ezf3d reads it.
    """
    ordered = sorted(records.get("features", []), key=lambda item: item["ordinal"])
    aligned = len(ordered) == len(ez_features)
    if not aligned and ordered:
        logger.error("%s: cadmpeg lists %d timeline entries, ezf3d %d", design.name, len(ordered), len(ez_features))
        design.notes.append("ezf3d and cadmpeg disagree on the timeline length; features use plain values, not parameters")
    for record in ordered:
        feature = Feature(
            ordinal=record["ordinal"],
            name=record.get("name") or record.get("source_tag") or f"Feature {record['ordinal'] + 1}",
            kind=record.get("source_tag") or "",
            definition=record.get("definition") or {},
            dependencies=record.get("dependencies") or [],
            ez=ez_features[record["ordinal"]] if aligned else None,
        )
        design.features.append(feature)
        design.feature_ids[record["id"]] = feature


def read_cylinders(design: Design, records: dict) -> None:
    """
    Fill in the cylinders of a design's finished solid.

    Args:
    - design: The design being read; its `cylinders` are set.
    - records: The CADIR ``model`` section.
    """
    for surface in records.get("surfaces", []):
        geometry = surface.get("geometry", {})
        if geometry.get("kind") == "cylinder":
            design.cylinders.append((vector(geometry["origin"]), vector(geometry["axis"]), float(geometry["radius"])))


def read_bounds(design: Design, doc) -> None:
    """
    Fill in the bounds of a design's finished bodies, as ezf3d reads them.

    Args:
    - design: The design being read; its `bounds` are set when ezf3d can read the bodies.
    - doc: The document as ezf3d opened it.
    """
    try:
        bodies = [body for body in doc.bodies if body.has_history] or doc.bodies
        points = [
            coedge.edge.start.position
            for body in bodies
            for face in Shape(body.model()).faces()
            for loop in face.loops()
            for coedge in loop.coedges()
            if coedge.edge is not None and coedge.edge.start is not None and coedge.edge.start.position is not None
        ]
    except EZF3D_ERRORS as error:
        logger.error("ezf3d could not read the bodies of %s: %r", design.name, error)
        design.notes.append(f"ezf3d could not read the bodies ({error})")
        return
    if points:
        design.bounds = (
            [round(min(float(p[i]) for p in points) * CM_TO_MM, 6) for i in range(3)],
            [round(max(float(p[i]) for p in points) * CM_TO_MM, 6) for i in range(3)],
        )


def read_design(doc, name: str, linked: bool, f3d: Path, cadir: dict) -> Design:
    """
    Combine cadmpeg's dump of a document with ezf3d's reading of it.

    Args:
    - doc: The document as ezf3d opened it.
    - name: The document's name.
    - linked: Whether it is a document the root design inserts.
    - f3d: The document on disk.
    - cadir: cadmpeg's dump of it.

    Returns:
    - The design: timeline, sketches, parameters and what its finished solid shows.

    Raises:
    - ConversionError: If the dump lacks a section the rebuild cannot do without.
    """
    design = Design(name=name, linked=linked, f3d=f3d)
    try:
        records = cadir["model"]
        arenas = cadir.get("native", {}).get("f3d", {}).get("arenas", {})
        read_sketches(design, records, arenas)
        read_features(design, records, read_parameters(design, doc))
        read_cylinders(design, records)
        collect_edges(arenas, design.edge_ends)
    except (KeyError, TypeError, ValueError) as error:
        logger.error("cadmpeg's dump of %s is not laid out as expected: %r", name, error)
        raise ConversionError(f"cadmpeg's dump of {name} is not laid out as expected: {error!r}") from error
    read_bounds(design, doc)
    logger.info("%s: %d timeline steps, %d sketches, %d parameters", name, len(design.features), len(design.sketches), len(design.parameters))
    return design
