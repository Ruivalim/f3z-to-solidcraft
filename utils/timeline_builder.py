from __future__ import annotations

import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import numpy as np

from .conversion_common import (
    BOUNDS_TOLERANCE,
    EXACT,
    FAILED,
    INFERRED,
    MAJORITY,
    POINT_TOLERANCE,
    PROBE,
    SKIPPED,
    SNAP,
    VOLUME_TOLERANCE,
    Outcome,
    Part,
    Progress,
    Solid,
    measured,
    numbers,
    slug,
)
from .fusion_design import Curve, Design, Feature, Sketch
from .sketch_constraints import DIMENSION_ROLE, Carried, Constrainer
from .solid_oracle import Mesh
from .solvecraft_session import Session

logger = logging.getLogger(__name__)

PARAM_UNITS = {"mm", "cm", "m", "in", "ft", "deg", "rad", ""}
OPERATIONS = {"NewBody": "new", "Join": "join", "Cut": "cut", "new_body": "new", "join": "join", "cut": "cut", "intersect": "intersect"}
#: Most fillet or chamfer features one Fusion fillet or chamfer may be split into.
MAX_FILLET_PASSES = 4
#: Cosine of the turn (46 degrees) up to which one edge runs on into the next. SolidCraft 0.8
#: carries a fillet or chamfer on through a 45 degree corner, and not through a square one.
SMOOTH = math.cos(math.radians(46.0))
#: A placement closer than this to another, entry by entry, is the same placement.
PLACEMENT_TOLERANCE = 1e-6
#: Which outcome wins when a feature has several parts: the later in this list.
SEVERITY = [EXACT, INFERRED, FAILED]
#: What a feature method may raise on data that is not laid out as expected.
#: One feature failing this way must not take the rest of the timeline with it.
FEATURE_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError)
#: The point SolidCraft names when a profile pick is in no closed region.
POINT_IN_ERROR = re.compile(r"\[\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*\]")


def in_profiles(sketch: Sketch, chosen: list[int], others: list[int], point) -> bool:
    """
    Tell whether a sketch point is in the chains an extrude names.

    Args:
    - sketch: The sketch.
    - chosen: Indices of the chains the extrude names.
    - others: Indices of the sketch's other chains; a point inside one is in an island.
    - point: (u, v) in the sketch.

    Returns:
    - True when the point is inside a chosen chain and outside every other.
    """
    return any(sketch.inside_loop(i, point) for i in chosen) and not any(sketch.inside_loop(i, point) for i in others)


def in_regions(sketch: Sketch, regions: list[dict], point) -> bool:
    """
    Tell whether a sketch point is in the regions an extrude names.

    Args:
    - sketch: The sketch.
    - regions: Each region's ``outer`` chain index and its ``holes``.
    - point: (u, v) in the sketch.

    Returns:
    - True when the point is inside a region's outer chain and outside its holes.
    """
    return any(
        sketch.inside_loop(region["outer"], point) and not any(sketch.inside_loop(hole, point) for hole in region.get("holes", []))
        for region in regions
    )


def on_span(point: np.ndarray, a: np.ndarray, b: np.ndarray) -> bool:
    """
    Tell whether a point lies on the straight span between two others.

    Args:
    - point: The point.
    - a: One end of the span.
    - b: The other end.

    Returns:
    - True when the point is on the span, ends included.
    """
    span = b - a
    length = float(np.linalg.norm(span))
    if length < POINT_TOLERANCE:
        return False
    t = float((point - a) @ span) / length**2
    return -1e-6 <= t <= 1 + 1e-6 and float(np.linalg.norm(a + t * span - point)) < POINT_TOLERANCE


def on_circle(point: np.ndarray, start: np.ndarray, middle: np.ndarray, end: np.ndarray) -> bool:
    """
    Tell whether a point lies on the circle an arc is part of.

    Args:
    - point: The point.
    - start: Where the arc starts.
    - middle: The arc's midpoint.
    - end: Where the arc ends; the same as its start for a full circle.

    Returns:
    - True when the point is on that circle; False when it is not, or the
      three points are in line and so give no circle.
    """
    if np.linalg.norm(start - end) < POINT_TOLERANCE:
        # A full circle: its midpoint is opposite its start. Any direction across it gives its plane's normal.
        centre = (start + middle) / 2.0
        across = point - centre
        normal = np.cross(start - centre, across)
    else:
        u, v = middle - start, end - start
        normal = np.cross(u, v)
        if float(normal @ normal) < POINT_TOLERANCE**2:
            return False
        # The centre is equally far from all three points and in their plane.
        centre = start + (float(v @ v) * np.cross(u, normal) - float(u @ u) * np.cross(v, normal)) / (-2.0 * float(normal @ normal))
    radius = float(np.linalg.norm(start - centre))
    off_plane = abs(float((point - centre) @ normal)) / (float(np.linalg.norm(normal)) or 1.0)
    return radius > POINT_TOLERANCE and off_plane < POINT_TOLERANCE and abs(float(np.linalg.norm(point - centre)) - radius) < POINT_TOLERANCE


def same_edge(edge: dict, a: np.ndarray, b: np.ndarray) -> bool:
    """
    Tell whether a SolidCraft edge is a span the Fusion file records.

    Args:
    - edge: The edge, with ``start``, ``end`` and ``mid``.
    - a: One end of the recorded span.
    - b: The other end.

    Returns:
    - True when the edge has those ends, or is a straight piece of the span.
    """
    start, end, middle = np.array(edge["start"]), np.array(edge["end"]), np.array(edge["mid"])
    if np.linalg.norm(a - b) < POINT_TOLERANCE:
        # A closed edge is recorded by the one point it starts and ends at. SolidCraft
        # starts its own elsewhere and may split it in arcs: an arc of the circle through that point is part of the edge.
        return on_circle(a, start, middle, end)
    forward = np.linalg.norm(start - a) < POINT_TOLERANCE and np.linalg.norm(end - b) < POINT_TOLERANCE
    backward = np.linalg.norm(start - b) < POINT_TOLERANCE and np.linalg.norm(end - a) < POINT_TOLERANCE
    return bool(forward or backward) or (on_span(start, a, b) and on_span(end, a, b) and on_span(middle, a, b))


def is_straight(edge: dict) -> bool:
    """
    Tell whether an edge is a straight line.

    Args:
    - edge: The edge, with ``start``, ``end`` and ``mid``.

    Returns:
    - True when its midpoint is halfway between its ends.
    """
    start, end, middle = np.array(edge["start"]), np.array(edge["end"]), np.array(edge["mid"])
    return float(np.linalg.norm((start + end) / 2.0 - middle)) < POINT_TOLERANCE


def still_edge(edge: dict, target: dict) -> bool:
    """
    Tell whether an edge of a body is one picked earlier, or what is left of it.

    A fillet or chamfer on a neighbouring edge shortens a straight edge, which
    moves its midpoint; what is left still lies on the span it had.

    Args:
    - edge: The edge as the body has it now.
    - target: The edge as it was when it was picked.

    Returns:
    - True when the two have the same midpoint, or the edge is a piece of a straight target.
    """
    if float(np.linalg.norm(np.array(edge["mid"]) - np.array(target["mid"]))) < POINT_TOLERANCE:
        return True
    a, b = np.array(target["start"]), np.array(target["end"])
    return is_straight(target) and all(on_span(np.array(edge[key]), a, b) for key in ("start", "end", "mid"))


def leaving(edge: dict, key: str) -> np.ndarray | None:
    """
    Find the direction an edge sets off in from one of its ends.

    Args:
    - edge: The edge, with ``start``, ``end`` and ``mid``.
    - key: ``start`` or ``end``: the end to set off from.

    Returns:
    - The unit direction, taking a curved edge for an arc through its three
      points; None for an edge of no length or one closed on itself.
    """
    here, there = np.array(edge[key]), np.array(edge["end" if key == "start" else "start"])
    middle = np.array(edge["mid"])
    if float(np.linalg.norm(there - here)) < POINT_TOLERANCE:
        return None
    if is_straight(edge):
        return (there - here) / np.linalg.norm(there - here)
    u, v = middle - here, there - here
    normal = np.cross(u, v)
    if float(normal @ normal) < POINT_TOLERANCE**2:
        return None
    # The centre is equally far from all three points and in their plane; the arc leaves square to its radius.
    centre = here + (float(v @ v) * np.cross(u, normal) - float(u @ u) * np.cross(v, normal)) / (-2.0 * float(normal @ normal))
    tangent = np.cross(normal, here - centre)
    tangent /= np.linalg.norm(tangent)
    return tangent if float(tangent @ u) > 0 else -tangent


def smooth_runs(edges: list[dict]) -> list[list[dict]]:
    """
    Group edges into runs in which each edge runs on into the next without a sharp turn.

    Args:
    - edges: Edges of the bodies, each with its ``body``.

    Returns:
    - The runs, each a list of edges; an edge no other runs on from is a run of one.
    """
    run_of = list(range(len(edges)))

    def root(index: int) -> int:
        """
        Find the run an edge is in.

        Args:
        - index: The edge's place in `edges`.

        Returns:
        - The place of the edge that stands for its run.
        """
        while run_of[index] != index:
            index = run_of[index]
        return index

    for i, first in enumerate(edges):
        for j in range(i + 1, len(edges)):
            second = edges[j]
            if first.get("body") != second.get("body"):
                continue
            for a in ("start", "end"):
                for b in ("start", "end"):
                    if float(np.linalg.norm(np.array(first[a]) - np.array(second[b]))) >= POINT_TOLERANCE:
                        continue
                    out, back = leaving(first, a), leaving(second, b)
                    # Two edges that leave their shared end in opposite directions run straight on into one another.
                    # Straight on gives -1; the limit allows the turn SolidCraft still carries on through.
                    if out is not None and back is not None and float(out @ back) < -SMOOTH:
                        run_of[root(j)] = root(i)
    runs: dict[int, list[dict]] = {}
    for index, edge in enumerate(edges):
        runs.setdefault(root(index), []).append(edge)
    return list(runs.values())


def axis_angle(rotation: np.ndarray) -> tuple[list[float], float] | None:
    """
    Describe a rotation matrix as a turn about an axis.

    Args:
    - rotation: The 3x3 matrix.

    Returns:
    - The axis and the angle in degrees (angle 0 for no rotation), or None
      when the matrix mirrors, scales or shears and so is no rotation.
    """
    if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-6) or np.linalg.det(rotation) < 0:
        return None
    angle = math.acos(max(-1.0, min(1.0, (float(np.trace(rotation)) - 1.0) / 2.0)))
    if angle < 1e-9:
        return [0.0, 0.0, 1.0], 0.0
    if math.pi - angle < 1e-6:
        # A half turn: the axis is the direction the rotation leaves alone.
        column = (rotation + np.eye(3))[:, int(np.argmax(np.diag(rotation)))]
        return numbers(column / np.linalg.norm(column), 9), 180.0
    axis = np.array([rotation[2, 1] - rotation[1, 2], rotation[0, 2] - rotation[2, 0], rotation[1, 0] - rotation[0, 1]])
    return numbers(axis / np.linalg.norm(axis), 9), round(math.degrees(angle), 9)


def chains(sketch: Sketch) -> list[tuple[list[Curve], tuple[float, float], tuple[float, float]]]:
    """
    Group a drawn sketch's curves into chains joined end to end.

    Args:
    - sketch: A drawn sketch.

    Returns:
    - Each chain's curves in order, and the points it starts and ends at;
      a closed chain starts and ends at the same point.
    """
    slits, taken, left = sketch.slits(), set(), []
    for curve in sketch.curves.values():
        drawn = sketch.drawn_ids.get(curve.id)
        if drawn and drawn not in taken and not curve.construction and curve.id not in slits and curve.kind != "circle":
            taken.add(drawn)
            left.append(curve)
    found = []
    while left:
        chain = [left.pop(0)]
        head, tail = chain[0].polyline()[0], chain[0].polyline()[-1]
        grown = True
        while grown:
            grown = False
            for curve in left:
                start, end = curve.polyline()[0], curve.polyline()[-1]
                if math.dist(start, tail) < SNAP or math.dist(end, tail) < SNAP:
                    chain.append(curve)
                    tail = end if math.dist(start, tail) < SNAP else start
                elif math.dist(start, head) < SNAP or math.dist(end, head) < SNAP:
                    chain.insert(0, curve)
                    head = end if math.dist(start, head) < SNAP else start
                else:
                    continue
                left.remove(curve)
                grown = True
                break
        found.append((chain, head, tail))
    return found


@dataclass
class EdgeTreatment:
    """
    How a kind of edge feature is rebuilt: a fillet or a chamfer.
    """

    #: The SolidCraft command, and the name of its size parameter.
    command: str
    size: str
    #: The slot the size's parameter fills in the Fusion feature.
    role: str
    #: The word for doing it, and the word for the feature.
    verb: str
    noun: str
    #: How far the old edge is left off the finished surface, per unit of size, when the faces meet at 90 degrees.
    offset: float

    def measure(self, size: float) -> str:
        """
        Write a size for the report.

        Args:
        - size: The radius or distance, in millimetres.

        Returns:
        - ``R2.5`` for a fillet, ``2.5 mm`` for a chamfer.
        """
        return f"R{round(size, 4)}" if self.size == "radius" else f"{round(size, 4)} mm"


FILLET = EdgeTreatment("solid.fillet", "radius", "Radius", "round", "fillet", math.sqrt(2.0) - 1.0)
CHAMFER = EdgeTreatment("solid.chamfer", "distance", "Distance", "chamfer", "chamfer", 1.0 / math.sqrt(2.0))


class EndSnapper:
    """
    Moves computed arc ends onto the line ends they were meant to meet.

    An arc is stored as centre, radius and angles, so its ends are computed and
    can miss the line ends they should meet by a rounding error, which leaves
    an outline open.
    """

    def __init__(self, sketch: Sketch) -> None:
        """
        Collect the ends of the sketch's lines.

        Args:
        - sketch: The sketch being drawn.
        """
        self.anchors = [
            numbers(end) for curve in sketch.curves.values() if curve.kind == "line" for end in (curve.polyline()[0], curve.polyline()[-1])
        ]

    def snap(self, point) -> list[float]:
        """
        Move a point onto a known end if it is close enough to be meant for it.

        Args:
        - point: (u, v) in the sketch.

        Returns:
        - The end it belongs on, or the point itself, which then becomes a known end.
        """
        point = numbers(point)
        near = min(self.anchors, key=lambda anchor: math.dist(anchor, point), default=point)
        if math.dist(near, point) < SNAP:
            return near
        self.anchors.append(point)
        return point


@dataclass
class ExtrudeJob:
    """
    One extrude being worked out: what is known, and what had to be inferred.
    """

    feature: Feature
    sketch: Sketch
    operation: str
    #: The finished solid, and the document's bodies as rebuilt so far.
    final: Mesh | None
    current: Mesh | None
    #: What the finished solid was asked for, for the report.
    inferred: list[str] = field(default_factory=list)

    def changes(self, point, sign: float) -> bool:
        """
        Ask the finished solid whether this extrude acted at a sketch point.

        Args:
        - point: (u, v) in the sketch.
        - sign: Which side of the sketch plane to look on: 1 along its normal, -1 against.

        Returns:
        - True when, just off the sketch there, a cut has removed material or
          a join or new body has added it.
        """
        probe = self.sketch.world(point) + sign * PROBE * self.sketch.normal
        in_current = self.current is not None and self.current.inside(probe)
        if self.operation == "cut":
            return in_current and not self.final.inside(probe)
        return self.final.inside(probe) and not in_current

    def changes_either_side(self, point) -> bool:
        """
        Ask the finished solid whether this extrude acted on either side of a sketch point.

        Args:
        - point: (u, v) in the sketch.

        Returns:
        - True when it did, along the normal or against it.
        """
        return self.changes(point, 1) or self.changes(point, -1)


class Builder:
    """
    Replays one document's timeline into a session.
    """

    def __init__(self, session: Session, part: Part, work: Path, components: list[Design], shown: Progress) -> None:
        """
        Get ready to rebuild a document.

        Args:
        - session: The SolidCraft session to build in.
        - part: The document, and where its outcomes are recorded.
        - work: A folder for temporary files.
        - components: The linked documents, built as components already; a Component Insert places them.
        - shown: The progress display.
        """
        self.session = session
        self.shown = shown
        self.part = part
        self.design = part.design
        self.work = work
        self.components = components
        self.prefix = f"{self.design.name}: " if self.design.linked else ""
        #: Fusion parameter name -> the name it has in SolidCraft.
        self.names: dict[str, str] = {}
        self.before = {body.get("name") for body in session.bodies()}
        #: Names of the bodies this document has made so far.
        self.bodies: list[str] = []
        self.last_sketch: Sketch | None = None
        #: The sketches drawn so far, in order.
        self.drawn: list[Sketch] = []
        #: How many Component Insert entries have been met.
        self.inserts = 0

    # Bookkeeping
    def label(self, feature: Feature) -> str:
        """
        Name a feature as it will appear in SolidCraft's timeline.

        Args:
        - feature: The Fusion timeline entry.

        Returns:
        - Fusion's own name, prefixed with the document's for a linked document.
        """
        return f"{self.prefix}{feature.name}"

    def done(self, feature: Feature, status: str, detail: str = "") -> None:
        """
        Record what became of a timeline entry.

        Args:
        - feature: The entry.
        - status: One of the outcome statuses.
        - detail: What was done, or why it was not.
        """
        self.part.outcomes.append(Outcome(feature.name, status, detail))
        log = logger.error if status == FAILED else logger.info
        log("%s: %s %s: %s", self.design.name, feature.name, status, detail)

    def refresh(self) -> list[dict]:
        """
        Measure the bodies this document has made.

        Returns:
        - SolidCraft's record of each; `bodies` is updated to their names.
        """
        own = [body for body in self.session.bodies() if body.get("name") not in self.before]
        self.bodies = [body["name"] for body in own]
        return own

    def current(self) -> Mesh | None:
        """
        Export this document's bodies as they stand.

        Returns:
        - Their triangles, or None when the document has no body yet.
        """
        return self.session.mesh(self.bodies, self.work / f"{slug(self.design.name)}.current.stl")

    def volume(self) -> float:
        """
        Measure the volume of this document's bodies.

        Returns:
        - Their total volume in cubic millimetres.
        """
        return sum(body.get("volume_mm3") or 0.0 for body in self.refresh())

    def expression(self, feature: Feature, role: str, value_mm: float) -> str:
        """
        Write a feature's value as SolidCraft should hold it.

        Args:
        - feature: The feature.
        - role: The slot the value fills, such as ``AlongDistance``.
        - value_mm: The value, in millimetres.

        Returns:
        - The name of the parameter filling that slot when it has that value, else the value itself.
        """
        parameter = feature.parameter(role)
        if parameter is not None and parameter.name in self.names and abs(abs(parameter.value) * 10.0 - abs(value_mm)) < 1e-6:
            return self.names[parameter.name]
        return f"{round(abs(value_mm), 6)} mm"

    # The timeline
    def run(self) -> None:
        """
        Rebuild the document: its parameters, then each timeline entry in order.
        """
        logger.info("%s: rebuilding %d timeline steps", self.design.name, len(self.design.features))
        self.parameters()
        for feature in self.design.features:
            self.shown.start(f"{self.design.name}: {feature.name}")
            self.build(feature)
            outcome = self.part.outcomes[-1]
            self.shown.finish(f"{self.design.name}: {feature.name}", outcome.status)
            solid = measured(self.refresh())
            if solid is not None and feature.kind != "Sketch":
                outcome.detail += f"  [now {solid.size} mm, {solid.volume:.0f} mm3]"
        self.part.result = measured(self.refresh())
        logger.info("%s: rebuilt; result %s", self.design.name, self.part.result)

    def build(self, feature: Feature) -> None:
        """
        Rebuild one timeline entry, recording its outcome whatever happens.

        Args:
        - feature: The entry.
        """
        builders = {
            "Sketch": self.sketch,
            "Extrude": self.extrude,
            "Fillet": self.fillet,
            "Chamfer": self.chamfer,
            "Sweep": self.sweep,
            "Hole": self.hole,
            "WorkPlane": self.work_plane,
            "Component Insert": self.component_insert,
        }
        build = builders.get(feature.kind)
        if build is None:
            self.done(feature, SKIPPED, f"{feature.kind or 'unknown'} features are not rebuilt yet")
            return
        try:
            late = self.late_dimensions(feature) if feature.kind != "Sketch" else ""
            build(feature)
        except FEATURE_ERRORS as error:
            logger.exception("%s: %s could not be rebuilt", self.design.name, feature.name)
            self.done(feature, FAILED, f"{type(error).__name__}: {error}")
            return
        self.part.outcomes[-1].detail += late

    def late_dimensions(self, feature: Feature) -> str:
        """
        Place the sketch dimensions the file lists with a feature that is not a sketch.

        A dimension added to a sketch after later features were made is listed
        with the last of those, not with its sketch. The feature's own sketch
        is tried first, then the others, newest first.

        Args:
        - feature: A timeline entry that is not a sketch.

        Returns:
        - A clause for the report, starting with ``; ``; empty when the feature lists no sketch dimension.
        """
        waiting = [p for p in getattr(feature.ez, "parameters", None) or [] if DIMENSION_ROLE.match(p.role or "")]
        listed, homes = len(waiting), []
        own = self.sketch_of(feature) if self.drawn else None
        for sketch in ([own] if own is not None else []) + [s for s in reversed(self.drawn) if s is not own]:
            if not waiting:
                break
            mark = len(self.session.script)
            ok, error = self.session.execute("sketch.edit", {"sketch": sketch.drawn_as})
            if not ok:
                logger.error("%s: %s could not be opened again for the dimensions of %s: %s", self.design.name, sketch.drawn_as, feature.name, error)
                continue
            left = Constrainer(self.session, sketch, self.design.name).dimensions(waiting, self.names, Carried())
            self.session.execute("sketch.finish", {})
            if len(left) < len(waiting):
                homes.append(sketch.drawn_as)
            else:
                # Nothing was added: opening and closing the sketch need not be in the script.
                del self.session.script[mark:]
            waiting = left
        if not listed:
            return ""
        logger.info("%s: %s lists %d sketch dimensions; %d placed on %s", self.design.name, feature.name, listed, listed - len(waiting), homes or "no sketch")
        placed = f"{listed - len(waiting)} of {listed} sketch dimensions the file lists with it placed by their value"
        return f"; {placed} on {', '.join(homes)}" if homes else f"; {placed}"

    def parameters(self) -> None:
        """
        Create the document's parameters in SolidCraft.
        """
        self.shown.start(f"{self.design.name}: parameters")
        prefix = f"{slug(self.design.name)}_" if self.design.linked else ""
        wanted = {p.name: f"{prefix}{p.name}" for p in self.design.parameters if p.unit in PARAM_UNITS}
        for parameter in self.design.parameters:
            self.set_parameter(parameter, wanted, bool(prefix))
        self.shown.finish(f"{self.design.name}: {len(self.names)} of {len(self.design.parameters)} parameters")
        logger.info("%s: %d of %d parameters set", self.design.name, len(self.names), len(self.design.parameters))

    def set_parameter(self, parameter, wanted: dict[str, str], renamed: bool) -> None:
        """
        Create one parameter in SolidCraft.

        Args:
        - parameter: The Fusion parameter.
        - wanted: Fusion parameter name -> the name it gets in SolidCraft.
        - renamed: Whether names differ, so expressions must be rewritten to match.
        """
        if parameter.name not in wanted:
            logger.error("%s: parameter %s has unit %r, which SolidCraft does not have", self.design.name, parameter.name, parameter.unit)
            self.part.notes.append(f"parameter {parameter.name} has unit {parameter.unit!r}; left out")
            return
        expression = parameter.expression
        if renamed:
            expression = re.sub(r"[A-Za-z_]\w*", lambda m: wanted.get(m.group(0), m.group(0)), expression)
        params = {"name": wanted[parameter.name], "expression": expression, "unit": parameter.unit}
        if parameter.comment:
            params["comment"] = parameter.comment
        ok, error = self.session.execute("parameters.change", params)
        if ok:
            self.names[parameter.name] = wanted[parameter.name]
        else:
            logger.error("%s: parameter %s = %r was refused: %s", self.design.name, parameter.name, parameter.expression, error)
            self.part.notes.append(f"parameter {parameter.name} = {parameter.expression!r} was refused: {error}")

    def component_insert(self, feature: Feature) -> None:
        """
        Account for a Component Insert; the linked documents were built as components already.

        Args:
        - feature: The timeline entry.
        """
        index, self.inserts = self.inserts, self.inserts + 1
        if not self.components:
            self.done(feature, SKIPPED, "the inserted document is not in the archive")
            return
        if index >= len(self.design.placements):
            names = ", ".join(component.name for component in self.components)
            self.done(feature, INFERRED, f"component {names} (left at the origin; the file gives no placement for this insert)")
            return
        source, matrix = self.design.placements[index]
        component = next((c for c in self.components if c.source == source), None)
        if component is None:
            self.done(feature, SKIPPED, f"the inserted document {source} is not in the archive")
            return
        status, where = self.place(component, matrix)
        self.done(feature, status, f"component {component.name} {where}")

    def place(self, component: Design, matrix: np.ndarray) -> tuple[str, str]:
        """
        Move a component to where the file places it.

        Args:
        - component: The linked document, built as a component at the origin.
        - matrix: Its placement: a 4x4 matrix, millimetres.

        Returns:
        - The outcome status, and where the component now is, for the report.
        """
        if np.allclose(matrix, np.eye(4), atol=PLACEMENT_TOLERANCE):
            return EXACT, "at the origin, where the file places it"
        turn = axis_angle(matrix[:3, :3])
        if turn is None:
            return FAILED, "left at the origin: its placement mirrors or scales it, which is not rebuilt"
        axis, angle = turn
        params: dict = {"occurrence": f"{component.name}:1", "space": "world", "translate": numbers(matrix[:3, 3])}
        if angle:
            params |= {"axis": axis, "angle": f"{angle} deg"}
        ok, result = self.session.execute("occurrence.move", params)
        if not ok:
            return FAILED, f"left at the origin: {result}"
        # SolidCraft answers with the placement it holds, column by column.
        held = np.array(result.get("transform", []), float) if isinstance(result, dict) else np.empty(0)
        if held.shape != (4, 4) or not np.allclose(held.T, matrix, atol=PLACEMENT_TOLERANCE):
            logger.error("%s: %s was moved to %s, not %s", self.design.name, component.name, held.tolist(), matrix.tolist())
            return FAILED, "moved, but SolidCraft holds a placement other than the file's"
        moved = f"moved {numbers(matrix[:3, 3])} mm" + (f" and turned {angle} deg about {axis}" if angle else "")
        return EXACT, f"{moved}, as the file places it"

    # Sketches
    def sketch(self, feature: Feature) -> None:
        """
        Draw a sketch on its own plane.

        Args:
        - feature: The timeline entry.
        """
        sketch = self.design.sketches.get(feature.definition.get("sketch", ""))
        if sketch is None or not sketch.curves:
            self.done(feature, SKIPPED, "no curves were decoded for this sketch")
            return
        name = self.label(feature)
        ok, error = self.session.execute("sketch.create", {"plane": self.plane(sketch), "name": name})
        if not ok:
            self.done(feature, FAILED, str(error))
            return
        drawn, refused = self.draw(sketch)
        sketch.drawn_as = name
        carried = Constrainer(self.session, sketch, self.design.name).run(list(getattr(feature.ez, "parameters", None) or []), self.names)
        self.session.execute("sketch.finish", {})
        self.last_sketch = sketch
        self.drawn.append(sketch)
        detail = f"{drawn} curves on its own plane" + (f", {refused} curves refused" if refused else "") + f"; {carried.summary()}"
        # A dimension is put on a curve of its size, which the file does not name.
        self.done(feature, FAILED if drawn == 0 else INFERRED if carried.dimensions else EXACT, detail)

    @staticmethod
    def plane(sketch: Sketch) -> object:
        """
        Describe a sketch's plane for `sketch.create`.

        Args:
        - sketch: The sketch.

        Returns:
        - ``XY`` when it is that plane, else its origin and axes.
        """
        axes = np.round([sketch.x_dir, sketch.y_dir], 9)
        if not np.any(np.abs(sketch.origin) > 1e-9) and np.array_equal(axes, [[1, 0, 0], [0, 1, 0]]):
            return "XY"
        return {"origin": numbers(sketch.origin), "x_dir": numbers(sketch.x_dir, 9), "y_dir": numbers(sketch.y_dir, 9)}

    def draw(self, sketch: Sketch) -> tuple[int, int]:
        """
        Draw a sketch's lines, arcs and circles, once each.

        Args:
        - sketch: The sketch, already created and active.

        Returns:
        - How many curves were drawn, and how many SolidCraft refused.
        """
        slits, snapper = sketch.slits(), EndSnapper(sketch)
        #: Shape of a curve -> the id SolidCraft gave it; curves drawn on top of one another share one.
        seen: dict[tuple, str] = {}
        sketch.drawn_ids = {}
        drawn = refused = 0
        for curve in sketch.curves.values():
            identity = curve.identity()
            if identity in seen:
                if seen[identity]:
                    sketch.drawn_ids[curve.id] = seen[identity]
                continue
            command, params = self.curve_command(curve, snapper)
            if curve.construction or curve.id in slits:
                params["construction"] = True
            ok, result = self.session.execute(command, params)
            if not ok:
                logger.error("%s: a %s of %s was refused: %s", self.design.name, curve.kind, sketch.id, result)
            made = result.get("created", {}).get("curves", []) if ok and isinstance(result, dict) else []
            seen[identity] = made[0] if made else ""
            if made:
                sketch.drawn_ids[curve.id] = made[0]
            drawn, refused = drawn + ok, refused + (not ok)
        return drawn, refused

    @staticmethod
    def curve_command(curve: Curve, snapper: EndSnapper) -> tuple[str, dict]:
        """
        Work out the command that draws one curve.

        Args:
        - curve: The curve.
        - snapper: Moves arc ends onto the line ends they meet.

        Returns:
        - The command id and its parameters.
        """
        points = curve.polyline()
        if curve.kind == "line":
            return "sketch.line", {"points": [numbers(points[0]), numbers(points[-1])]}
        centre = numbers([curve.data["center"]["u"], curve.data["center"]["v"]])
        if curve.kind == "circle":
            return "sketch.circle.center", {"center": centre, "radius": round(curve.data["radius"], 6)}
        return "sketch.arc.center_point", {"center": centre, "start": snapper.snap(points[0]), "end": snapper.snap(points[-1])}

    def sketch_of(self, feature: Feature) -> Sketch | None:
        """
        Find the sketch a feature works from.

        Args:
        - feature: The feature.

        Returns:
        - The drawn sketch it names, or one among its dependencies, or the one
          drawn before it; None when no sketch has been drawn.
        """
        definition = feature.definition
        named = definition.get("profile", {}).get("value", {}).get("sketch") or definition.get("properties", {}).get("profile")
        candidates = [named] + [self.design.feature_ids[d].definition.get("sketch") for d in feature.dependencies if d in self.design.feature_ids]
        for candidate in candidates:
            sketch = self.design.sketches.get(candidate or "")
            if sketch is not None and sketch.drawn_as:
                return sketch
        return self.last_sketch

    # Sketch regions
    def regions(self, sketch: Sketch) -> list[list[tuple[float, float]]]:
        """
        List a sketch's regions as SolidCraft finds them.

        SolidCraft does not list a sketch's regions, so a trial extrude picking
        every sample point is made, read back for the region each pick resolved
        to, and undone.

        Args:
        - sketch: A drawn sketch.

        Returns:
        - The sample points in each region; empty when the sketch has no closed region.
        """
        if sketch.regions is not None:
            return sketch.regions
        sketch.regions = []
        points = self.trial_extrude(sketch, list(dict.fromkeys(tuple(numbers(p)) for p in sketch.sample_points())))
        if not points:
            return sketch.regions
        keys = self.trial_regions(len(points))
        grouped: dict[str, list] = {}
        for point, key in zip(points, keys, strict=False):
            grouped.setdefault(str(sorted(key) if isinstance(key, list) else key), []).append(point)
        sketch.regions = list(grouped.values()) if keys else []
        logger.debug("%s: %s has %d regions", self.design.name, sketch.drawn_as, len(sketch.regions))
        return sketch.regions

    def trial_extrude(self, sketch: Sketch, points: list) -> list:
        """
        Extrude every point that is in a region of a sketch, as a trial left in place.

        SolidCraft refuses the whole extrude when one pick is in no closed
        region and names that point; it is dropped and the rest tried again.

        Args:
        - sketch: A drawn sketch.
        - points: Candidate picks, (u, v).

        Returns:
        - The picks SolidCraft accepted, in order; empty when no trial was made.
        """
        while points:
            params = {"sketch": sketch.drawn_as, "profiles": [{"point": list(p)} for p in points], "distance": 1, "operation": "new", "name": "__probe"}
            ok, error = self.session.execute("solid.extrude", params, record=False)
            if ok:
                return points
            at = POINT_IN_ERROR.search(str(error))
            if at is None:
                logger.error("%s: the trial extrude of %s failed: %s", self.design.name, sketch.drawn_as, error)
                return []
            bad = (float(at.group(1)), float(at.group(2)))
            points.remove(min(points, key=lambda p: math.dist(p, bad)))
        return []

    def trial_regions(self, picks: int) -> list:
        """
        Read back which region each pick of the trial extrude resolved to, and undo the trial.

        Args:
        - picks: How many picks the trial had.

        Returns:
        - One key per pick (the region's curve loop or its index, depending
          on how SolidCraft stored it), or an empty list when it stored something else.
        """
        ok, info = self.session.execute("timeline.inspect", {"feature": "__probe"}, record=False)
        self.session.undo()
        profiles = info.get("definition", {}).get("profiles", {}) if ok and isinstance(info, dict) else {}
        keys = profiles.get("loops") or profiles.get("indices") or []
        if len(keys) != picks:
            logger.error("%s: SolidCraft stored %d regions for %d picks: %s", self.design.name, len(keys), picks, profiles.get("type"))
            return []
        return keys

    # Extrudes
    def extrude(self, feature: Feature) -> None:
        """
        Rebuild an extrude: its regions, its extent and its operation.

        Args:
        - feature: The timeline entry.
        """
        sketch = self.sketch_of(feature)
        if sketch is None:
            self.done(feature, FAILED, "no sketch to extrude")
            return
        operation = OPERATIONS.get(feature.definition.get("op") or getattr(getattr(feature.ez, "extrude", None), "operation", ""))
        if operation is None:
            self.done(feature, FAILED, "the operation (new body, join or cut) was not decoded")
            return
        job = ExtrudeJob(feature, sketch, operation, self.part.final, self.current())
        regions = self.regions(sketch)
        if not regions:
            self.done(feature, FAILED, f"SolidCraft finds no closed region in {sketch.drawn_as}")
            return
        chosen = self.choose_regions(job, regions)
        if not chosen:
            self.done(feature, FAILED, "its profile was not decoded and the finished solid does not show it")
            return
        extent = self.extent(job, [p for i in chosen for p in regions[i]])
        if extent is None:
            self.done(feature, FAILED, "its extent was not decoded and there is no finished solid to infer it from")
            return
        params = {"sketch": sketch.drawn_as, "operation": operation, "name": self.label(feature)} | extent | self.taper(feature)
        params["profiles"] = [{"point": list(regions[i][0])} for i in chosen]
        ok, error, apart = self.make("solid.extrude", params, operation)
        if not ok:
            self.done(feature, FAILED, str(error))
            return
        what = f"{operation}, {len(chosen)} region(s) of {sketch.drawn_as}"
        if job.inferred:
            what += f"; {', '.join(job.inferred)} inferred from the finished solid"
        if apart:
            what += f"; {apart}"
        self.done(feature, INFERRED if job.inferred or apart else EXACT, what)

    @staticmethod
    def region_test(sketch: Sketch, profile: dict):
        """
        Build the test for the profile a decoded extrude names.

        Args:
        - sketch: The extrude's sketch.
        - profile: The ``profile`` of the extrude's definition.

        Returns:
        - A function telling whether a sketch point is in that profile, or
          None when the definition names none.
        """
        value = profile.get("value", {})
        if profile.get("kind") == "sketch_profiles":
            chosen = [i for i in value.get("profiles", []) if i < len(sketch.loops)]
            if chosen:
                return partial(in_profiles, sketch, chosen, [i for i in range(len(sketch.loops)) if i not in chosen])
        if profile.get("kind") == "sketch_regions":
            regions = [r for r in value.get("regions", []) if r.get("outer", len(sketch.loops)) < len(sketch.loops)]
            if regions:
                return partial(in_regions, sketch, regions)
        return None

    def choose_regions(self, job: ExtrudeJob, regions: list[list]) -> list[int]:
        """
        Pick the sketch regions an extrude sweeps.

        The decoded profile is a chain of curves, which is not always the region
        Fusion swept; it is kept only where the finished solid bears it out.

        Args:
        - job: The extrude; ``profile`` is added to what it inferred when the solid decided.
        - regions: The sketch's regions, as sample points.

        Returns:
        - Indices of the chosen regions; empty when neither source shows any.
        """

        def holding(predicate) -> list[int]:
            """
            List the regions most of whose points satisfy a test.

            Args:
            - predicate: The test, of one sketch point.

            Returns:
            - Indices of those regions.
            """
            return [i for i, region in enumerate(regions) if sum(map(predicate, region)) >= MAJORITY * len(region)]

        test = self.region_test(job.sketch, job.feature.definition.get("profile", {}))
        decoded = holding(test) if test is not None else []
        shown = holding(job.changes_either_side) if job.final is not None and job.operation != "intersect" else []
        shown = self.own_regions(job, regions, shown)
        use_decoded = bool(decoded) and (decoded == shown or job.final is None or not shown)
        logger.debug("%s: %s %s, %d regions, decoded %s, shown by the solid %s", self.design.name, job.feature.name, job.operation, len(regions), decoded, shown)
        if not use_decoded and shown:
            job.inferred.append("profile")
        return decoded if use_decoded else shown

    def own_regions(self, job: ExtrudeJob, regions: list[list], shown: list[int]) -> list[int]:
        """
        Leave out the regions a later extrude of the same sketch fills.

        Just off the sketch plane, material added by this extrude and material
        added by a later one of the same sketch look alike. They differ in how
        far they reach: a region whose material stops short of this extrude's
        length is the later one's.

        Args:
        - job: The extrude.
        - regions: The sketch's regions, as sample points.
        - shown: Indices of the regions the finished solid shows material in.

        Returns:
        - The regions that reach this extrude's length; all of them when the
          length is not decoded, no later extrude shares the sketch, or none reaches.
        """
        length = self.blind_length(job.feature)
        if length is None or job.operation == "cut" or not self.extruded_again(job):
            return shown
        direction = math.copysign(1.0, length) * job.sketch.normal
        kept = []
        for index in shown:
            hits = [job.final.first_hit(job.sketch.world(point) + PROBE * direction, direction) for point in regions[index]]
            reach = [hit + PROBE for hit in hits if hit is not None]
            if not reach or statistics.median(reach) >= abs(length) - BOUNDS_TOLERANCE:
                kept.append(index)
        if kept and len(kept) < len(shown):
            logger.debug("%s: %s leaves %d region(s) of %s to a later extrude", self.design.name, job.feature.name, len(shown) - len(kept), job.sketch.drawn_as)
        return kept or shown

    def extruded_again(self, job: ExtrudeJob) -> bool:
        """
        Tell whether a later extrude adds material from the same sketch.

        Args:
        - job: The extrude.

        Returns:
        - True when a later join or new-body extrude names the same sketch.
        """
        for feature in self.design.features:
            definition = feature.definition
            named = definition.get("profile", {}).get("value", {}).get("sketch") if isinstance(definition.get("profile"), dict) else None
            if feature.ordinal > job.feature.ordinal and feature.kind == "Extrude" and named == job.sketch.id and OPERATIONS.get(definition.get("op")) in ("new", "join"):
                return True
        return False

    @staticmethod
    def blind_length(feature: Feature) -> float | None:
        """
        Read an extrude's length where the file gives it as a plain distance from its sketch.

        Args:
        - feature: The extrude.

        Returns:
        - The length in millimetres, negative when it runs against the sketch's
          normal; None when the file gives another kind of extent.
        """
        definition = feature.definition
        extent = definition.get("extent", {})
        termination = extent.get("side", {}).get("termination", {})
        if extent.get("kind") != "one_sided" or termination.get("kind") != "blind" or definition.get("start", {}).get("kind") != "profile_plane":
            return None
        length = float(termination["length"])
        return -length if definition.get("direction", {}).get("kind") == "reversed_profile_normal" else length

    def extent(self, job: ExtrudeJob, points: list) -> dict | None:
        """
        Work out how far an extrude goes, and which way.

        Args:
        - job: The extrude.
        - points: Sample points of its regions.

        Returns:
        - The extent parameters for `solid.extrude`, or None when the extent is
          not decoded and there is no finished solid to infer it from.
        """
        decoded = self.decoded_extent(job)
        if decoded is not None:
            return decoded
        if job.final is None:
            return None
        if getattr(getattr(job.feature.ez, "extrude", None), "direction", "OneSide") == "Symmetric":
            return self.symmetric_extent(job, points)
        return self.one_sided_extent(job, points)

    def decoded_extent(self, job: ExtrudeJob) -> dict | None:
        """
        Read an extrude's extent where the file gives it as a plain distance.

        Args:
        - job: The extrude.

        Returns:
        - The extent parameters, or None when the file gives another kind of extent.
        """
        definition = job.feature.definition
        extent = definition.get("extent", {})
        termination = extent.get("side", {}).get("termination", {})
        if extent.get("kind") != "one_sided" or termination.get("kind") != "blind" or definition.get("start", {}).get("kind") != "profile_plane":
            return None
        length = float(termination["length"])
        params = {"distance": self.expression(job.feature, "AlongDistance", length)}
        if length < 0:
            params["direction"] = "negative"
        return params

    def symmetric_extent(self, job: ExtrudeJob, points: list) -> dict:
        """
        Work out a symmetric extrude's distance per side.

        SolidCraft's distance is per side; Fusion's is the whole or the half,
        and the file does not say which, so the finished solid is measured.

        Args:
        - job: The extrude; what was inferred is added to it.
        - points: Sample points of its regions.

        Returns:
        - The extent parameters.
        """
        normal = job.sketch.normal
        reach = [job.final.first_hit(job.sketch.world(p) + PROBE * normal, normal) for p in points]
        half = statistics.median([r + PROBE for r in reach if r is not None] or [0.0])
        along = job.feature.parameter("AlongDistance")
        stated = abs(along.value) * 10.0 if along is not None else 0.0
        job.inferred.append("whole or half length")
        if stated and abs(half - stated / 2) <= abs(half - stated):
            name = self.names.get(along.name)
            return {"direction": "symmetric", "distance": f"{name} / 2" if name else f"{round(stated / 2, 6)} mm"}
        if stated:
            return {"direction": "symmetric", "distance": self.expression(job.feature, "AlongDistance", stated)}
        return {"direction": "symmetric", "distance": f"{round(half, 6)} mm"}

    def one_sided_extent(self, job: ExtrudeJob, points: list) -> dict:
        """
        Work out which way a one-sided extrude goes, and how far.

        Args:
        - job: The extrude; what was inferred is added to it.
        - points: Sample points of its regions.

        Returns:
        - The extent parameters.
        """
        sign = 1.0 if sum(job.changes(p, 1) for p in points) >= sum(job.changes(p, -1) for p in points) else -1.0
        params: dict = {"direction": "negative"} if sign < 0 else {}
        job.inferred.append("side")
        along = job.feature.parameter("AlongDistance")
        if along is not None:
            return params | {"distance": self.expression(job.feature, "AlongDistance", abs(along.value) * 10.0)}
        job.inferred.append("depth")
        return params | self.depth(job, points, sign)

    def depth(self, job: ExtrudeJob, points: list, sign: float) -> dict:
        """
        Measure how deep a to-object or through-all extrude goes in the finished solid.

        Args:
        - job: The extrude.
        - points: Sample points of its regions.
        - sign: Which way it goes: 1 along the sketch normal, -1 against.

        Returns:
        - A ``distance``, or ``through_all`` when most of it runs out of the solid.
        """
        reach, open_ended = [], 0
        for point in points:
            end, runs_out = self.reach(job, point, sign)
            open_ended += runs_out
            reach += [end] if end is not None else []
        if reach and open_ended <= len(reach) / 2:
            return {"distance": f"{round(statistics.median(reach) + PROBE, 6)} mm"}
        return {"through_all": True}

    @staticmethod
    def reach(job: ExtrudeJob, point, sign: float) -> tuple[float | None, bool]:
        """
        Measure how far an extrude goes from one sketch point.

        A cut ends where the material being cut does, short of where the
        finished solid resumes: the gap between may be space that was already
        empty.  A join or new body ends where the finished solid does.

        Args:
        - job: The extrude.
        - point: (u, v) in the sketch.
        - sign: Which way it goes.

        Returns:
        - The distance, or None when this point says nothing; and whether the
          cut runs out of the finished solid here.
        """
        direction = sign * job.sketch.normal
        start = job.sketch.world(point) + PROBE * direction
        stop = job.final.first_hit(start, direction)
        if job.operation != "cut":
            return stop, False
        if job.current is None or not job.current.inside(start):
            return None, False
        return job.current.last_exit(start, direction, stop), stop is None

    def taper(self, feature: Feature) -> dict:
        """
        Read an extrude's taper angle.

        Args:
        - feature: The extrude.

        Returns:
        - The ``taper`` parameter, or nothing when the sides are straight.
        """
        taper = feature.parameter("TaperAngle")
        if taper is None or abs(taper.value) <= 1e-12:
            return {}
        return {"taper": self.names.get(taper.name) or f"{round(math.degrees(taper.value), 6)} deg"}

    def bodies_for(self, operation: str) -> dict:
        """
        Say which bodies an extrude makes or acts on.

        Args:
        - operation: ``new``, ``join``, ``cut`` or ``intersect``.

        Returns:
        - A name for a new body, or this document's bodies as targets, so a
          cut does not reach into another document's.
        """
        if operation == "new":
            return {"body_name": self.design.name if not self.bodies else f"{self.design.name} {len(self.bodies) + 1}"}
        return {"targets": list(self.bodies)} if self.bodies else {}

    def make(self, command: str, params: dict, operation: str) -> tuple[bool, object, str]:
        """
        Run an extrude or a sweep, keeping a join SolidCraft refuses as a body of its own.

        From 0.8 SolidCraft refuses a join it cannot fuse exactly, such as
        curved bodies that only touch, where older builds moved the new
        material a little to make them overlap.

        Args:
        - command: ``solid.extrude`` or ``solid.sweep``.
        - params: Its parameters, without the bodies it makes or acts on.
        - operation: ``new``, ``join``, ``cut`` or ``intersect``.

        Returns:
        - Whether it worked, SolidCraft's refusal if not, and a note for the
          report when the material was made but not joined.
        """
        ok, error = self.session.execute(command, params | self.bodies_for(operation))
        if ok or operation != "join" or not self.bodies:
            return ok, error, ""
        apart, _ = self.session.execute(command, params | {"operation": "new"} | self.bodies_for("new"))
        if not apart:
            return False, error, ""
        logger.warning("%s: SolidCraft refused to join %s, so it is a body of its own: %s", self.design.name, params.get("name"), error)
        return True, error, f"made as a body of its own, which SolidCraft refused to join to the rest ({error})"

    # Fillets
    def edges(self) -> list[dict]:
        """
        List the edges of this document's bodies.

        Returns:
        - SolidCraft's record of each edge, with the name of its body added.
        """
        found = []
        for body in self.bodies:
            ok, topology = self.session.call("body_topology", {"body": body})
            if ok and isinstance(topology, dict):
                found += [edge | {"body": body} for edge in topology.get("edges", [])]
            else:
                logger.error("%s: SolidCraft could not list the edges of %s: %s", self.design.name, body, topology)
        return found

    def fillet(self, feature: Feature) -> None:
        """
        Rebuild a fillet, one SolidCraft fillet per radius group.

        Args:
        - feature: The timeline entry.
        """
        self.edge_feature(feature, FILLET)

    def chamfer(self, feature: Feature) -> None:
        """
        Rebuild a chamfer, one SolidCraft chamfer per group of edges.

        Args:
        - feature: The timeline entry.
        """
        self.edge_feature(feature, CHAMFER)

    def edge_feature(self, feature: Feature, treatment: EdgeTreatment) -> None:
        """
        Rebuild a fillet or a chamfer, group by group.

        Args:
        - feature: The timeline entry.
        - treatment: Whether it is a fillet or a chamfer.
        """
        groups = feature.definition.get("groups") or [{}]
        status, notes, made = EXACT, [], 0
        for number, group in enumerate(groups, start=1):
            name = self.label(feature) if len(groups) == 1 else f"{self.label(feature)} ({number})"
            treated, group_status, group_notes = self.edge_group(feature, group, name, treatment)
            made += treated
            notes += group_notes
            status = max(status, group_status, key=SEVERITY.index)
        self.done(feature, status if made else FAILED, "; ".join(notes))

    def edge_group(self, feature: Feature, group: dict, name: str, treatment: EdgeTreatment) -> tuple[int, str, list[str]]:
        """
        Rebuild one group of a fillet or chamfer: the edges that share a size.

        Args:
        - feature: The fillet or chamfer.
        - group: The group's definition; empty when the file does not decode it.
        - name: The name the SolidCraft feature gets.
        - treatment: Whether it is a fillet or a chamfer.

        Returns:
        - How many edges were treated, the group's outcome status, and notes for the report.
        """
        size, why = self.edge_size(feature, group, treatment)
        if not size:
            return 0, FAILED, [why]
        candidates = self.edges()
        status, notes = EXACT, []
        chosen, known, slots = self.recorded_edges(group, candidates)
        logger.debug("%s: %s names %d edges, %d of them with known positions; %d edges of the body match", self.design.name, name, slots, known, len(chosen))
        if slots and known < slots:
            notes.append(f"positions known for {known} of its {slots} edges")
            status = INFERRED
        if not chosen and slots and known == slots:
            # Inferring here would take edges that belong to a later fillet or chamfer.
            return 0, FAILED, [f"the {slots} edges it names are not on the body as rebuilt so far"]
        if not chosen and self.part.final is not None:
            chosen = self.inferred_edges(candidates, size * treatment.offset)
            notes.append("edges inferred from the finished solid")
            status = INFERRED
        if not chosen:
            return 0, FAILED, notes + ["no edges found for it"]
        treated, passes, error = self.treat_edges(chosen, treatment, self.expression(feature, treatment.role, size), name)
        if treated:
            notes.append(f"{treatment.measure(size)} on {treated} edge(s)" if treated == len(chosen) else f"{treatment.measure(size)} on {treated} of {len(chosen)} edge(s)")
        if passes > 1:
            notes.append(f"SolidCraft would not {treatment.verb} them in one {treatment.noun}, so it took {passes}")
            status = INFERRED
        if treated < len(chosen):
            notes.append(f"SolidCraft could not {treatment.verb} the rest ({error})")
            status = INFERRED if treated else FAILED
        return treated, status, notes

    @staticmethod
    def edge_size(feature: Feature, group: dict, treatment: EdgeTreatment) -> tuple[float | None, str]:
        """
        Read the size of a fillet or chamfer group.

        Args:
        - feature: The fillet or chamfer.
        - group: The group's definition.
        - treatment: Whether it is a fillet or a chamfer.

        Returns:
        - The radius or distance in millimetres, or None and why there is none.
        """
        if treatment is FILLET:
            radius = group.get("radius", {})
            if radius.get("kind") == "constant":
                return radius.get("radius"), ""
        else:
            spec = group.get("spec", {})
            if spec.get("kind") == "distance":
                return spec.get("distance"), ""
            if spec.get("kind"):
                # Two distances, or a distance and an angle: which face gets which is not decoded.
                return None, f"a {spec['kind'].replace('_', ' ')} chamfer is not rebuilt yet, only one of equal distances"
        parameter = feature.parameter(treatment.role)
        if parameter is None:
            return None, f"{treatment.size} not decoded"
        return abs(parameter.value) * 10.0, ""

    def recorded_edges(self, group: dict, candidates: list[dict]) -> tuple[list[dict], int, int]:
        """
        Find the edges a fillet or chamfer group names in the file.

        The file names the edges by their slot in the solid as it stood before
        the feature; other records give the end points of some slots.

        Args:
        - group: The group's definition.
        - candidates: The edges of the bodies as rebuilt so far.

        Returns:
        - The matching edges, how many named slots have known end points, and
          how many slots are named (0 when the file names none).
        """
        selection = group.get("edges", {})
        if selection.get("kind") != "historical":
            return [], 0, 0
        slots = [int(edge.rsplit(":", 1)[-1]) for edge in selection.get("value", {}).get("edges", [])]
        spans = [span for slot in slots for span in self.design.edge_ends.get(slot, [])]
        chosen = [edge for edge in candidates if any(same_edge(edge, a, b) for a, b in spans)]
        return chosen, sum(1 for slot in slots if slot in self.design.edge_ends), len(slots)

    def inferred_edges(self, candidates: list[dict], expected: float) -> list[dict]:
        """
        Find the edges the finished solid shows rounded or chamfered by some size.

        A rounded 90-degree edge leaves its old midline r(sqrt 2 - 1) off the
        finished surface; a chamfered one, its distance over sqrt 2.

        Args:
        - candidates: The edges of the bodies as rebuilt so far.
        - expected: How far off the finished surface the old edge is left, in millimetres.

        Returns:
        - The edges whose midpoints are that far from the finished solid.
        """
        return [edge for edge in candidates if abs(self.part.final.distance(edge["mid"]) - expected) <= 0.25 * expected]

    def treat_edges(self, chosen: list[dict], treatment: EdgeTreatment, size: str, name: str) -> tuple[int, int, object]:
        """
        Fillet or chamfer the chosen edges, in as few features as SolidCraft allows.

        SolidCraft does not take every edge combination Fusion does. When it
        refuses the whole set, the largest set it handles is treated first and
        the edges left over are tried again as a further feature.

        Args:
        - chosen: The edges to treat.
        - treatment: Whether to fillet or chamfer them.
        - size: The radius or distance, as an expression.
        - name: The name of the feature.

        Returns:
        - How many edges were treated, how many features it took, and
          SolidCraft's last refusal.
        """
        # Edge indices change with every fillet; where the edges are does not.
        targets = list(chosen)
        treated, passes, refusal = 0, 0, ""
        while targets and passes < MAX_FILLET_PASSES:
            picks = [edge for edge in self.edges() if any(still_edge(edge, target) for target in targets)]
            if not picks:
                break
            before = measured(self.refresh())
            pass_name = name if passes == 0 else f"{name} (part {passes + 1})"
            ok, error = self.attempt_edges(picks, treatment, size, pass_name, before, record=True)
            accepted, sound = (picks, picks) if ok else self.largest_set(picks, treatment, size, pass_name, before)
            if not ok:
                refusal = error
            if not accepted:
                break
            left = [target for target in targets if not any(still_edge(edge, target) for edge in accepted)]
            treated += len(targets) - len(left)
            passes += 1
            # An edge SolidCraft refuses on its own is not tried again: each refusal takes seconds,
            # and the edges treated since seldom change it.
            targets = [target for target in left if any(still_edge(edge, target) for edge in sound)]
        return treated, passes, refusal

    def attempt_edges(self, edges: list[dict], treatment: EdgeTreatment, size: str, name: str, before: Solid | None, record: bool) -> tuple[bool, object]:
        """
        Fillet or chamfer some edges, keeping the result only if it is sound.

        Neither ever makes a body larger than it was; one that did has gone
        wrong and is undone.  A trial is undone either way.

        Args:
        - edges: The edges to treat.
        - treatment: Whether to fillet or chamfer them.
        - size: The radius or distance, as an expression.
        - name: The name of the feature.
        - before: The bodies as measured before it.
        - record: Whether to keep a sound result; False for a trial.

        Returns:
        - Whether SolidCraft made a sound feature of these edges, and its refusal if not.
        """
        params = {"edges": [{"body": edge["body"], "index": edge["index"]} for edge in edges], treatment.size: size, "name": name}
        ok, error = self.session.execute(treatment.command, params, record)
        if not ok:
            return False, error
        after = measured(self.refresh())
        grew = after is None or before is None or not after.inside_bounds_of(before)
        if grew or not record:
            self.session.undo()
            if record:
                self.session.script.pop()
        if grew:
            # Expected while a trial is looking for the edges SolidCraft handles; an error when it was meant to stay.
            log = logger.error if record else logger.debug
            log("%s: SolidCraft's %s %s of %d edges made the body larger; undone", self.design.name, treatment.noun, name, len(edges))
        return (not grew), f"SolidCraft's {treatment.noun} of these edges together made the body larger"

    def largest_set(self, picks: list[dict], treatment: EdgeTreatment, size: str, name: str, before: Solid | None) -> tuple[list[dict], list[dict]]:
        """
        Fillet or chamfer the largest set of edges SolidCraft handles together.

        Args:
        - picks: The edges wanted.
        - treatment: Whether to fillet or chamfer them.
        - size: The radius or distance, as an expression.
        - name: The name of the feature.
        - before: The bodies as measured before it.

        Returns:
        - The edges treated, empty when SolidCraft handles none; and the edges
          it handles on their own, treated or not.
        """
        # SolidCraft carries a fillet or chamfer along edges that run on into one
        # another, so such a run stands or falls together: one trial tells for all of it.
        runs = smooth_runs(picks)
        sound = [run for run in runs if self.attempt_edges(run, treatment, size, name, before, record=False)[0]]
        alone = [edge for run in sound for edge in run]
        logger.debug("%s: %s: SolidCraft takes %d of %d edges on their own, in %d of %d run(s)", self.design.name, name, len(alone), len(picks), len(sound), len(runs))
        if alone and self.attempt_edges(alone, treatment, size, name, before, record=True)[0]:
            return alone, alone
        # The runs it takes one by one are not always taken together: add them one at a time.
        accepted: list[dict] = []
        for run in sound if len(sound) > 1 else []:
            if self.attempt_edges(accepted + run, treatment, size, name, before, record=False)[0]:
                accepted += run
        if accepted and self.attempt_edges(accepted, treatment, size, name, before, record=True)[0]:
            return accepted, alone
        return [], alone

    # Sweeps
    def sweep(self, feature: Feature) -> None:
        """
        Rebuild a sweep: a sketch region carried along a chain of sketch curves.

        The file names the profile's sketch but not its region, and names the
        path only by how many curves it has; the finished solid and the drawn
        sketches fill both in.

        Args:
        - feature: The timeline entry.
        """
        definition = feature.definition
        mode = definition.get("mode", {})
        section = self.design.sketches.get(definition.get("section", {}).get("value", {}).get("value", ""))
        operation = OPERATIONS.get(mode.get("op"))
        if section is None or not section.drawn_as:
            self.done(feature, FAILED, "the sketch of its profile was not decoded or not drawn")
            return
        if mode.get("mode", "solid") != "solid" or operation is None:
            self.done(feature, FAILED, f"a {mode.get('mode', 'solid')} sweep with operation {mode.get('op')!r} is not rebuilt yet")
            return
        job = ExtrudeJob(feature, section, operation, self.part.final, self.current())
        regions = self.regions(section)
        chosen = self.choose_regions(job, regions) if regions else []
        if not chosen:
            self.done(feature, FAILED, f"its profile is not decoded and the finished solid does not show which region of {section.drawn_as} it is")
            return
        found = self.sweep_path(feature, section, section.world(regions[chosen[0]][0]))
        if found is None:
            self.done(feature, FAILED, "no chain of sketch curves to sweep along was found")
            return
        path_sketch, path = found
        params = {"sketch": section.drawn_as, "path_sketch": path_sketch.drawn_as, "path": path, "operation": operation, "name": self.label(feature)}
        params["profiles"] = [{"point": list(regions[i][0])} for i in chosen]
        ok, error, apart = self.make("solid.sweep", params, operation)
        if not ok:
            self.done(feature, FAILED, str(error))
            return
        what = f"{operation}, {len(chosen)} region(s) of {section.drawn_as} along {len(path)} curve(s) of {path_sketch.drawn_as}; profile and path inferred"
        if apart:
            what += f"; {apart}"
        extent = definition.get("path_extent", {})
        if extent.get("along_fraction", 1.0) != 1.0 or extent.get("against_fraction", 0.0) != 0.0:
            what += "; swept along the whole path, not the part of it the file gives"
        self.done(feature, INFERRED, what)

    def sweep_path(self, feature: Feature, section: Sketch, start: np.ndarray) -> tuple[Sketch, list[str]] | None:
        """
        Find the chain of curves a sweep runs along.

        Args:
        - feature: The sweep.
        - section: The sketch of its profile, which the path is not in.
        - start: A point of the profile, in the model; the path starts at its end nearest this.

        Returns:
        - The path's sketch and its curves' SolidCraft ids in order, or None when no chain is found.
        """
        wanted = self.design.group_sizes.get(feature.definition.get("path", {}).get("value", ""))
        # (distance of its nearer end from the profile, sketch, curves in order from that end)
        best: tuple[float, Sketch, list[Curve]] | None = None
        for sized in (True, False):
            if sized and not wanted:
                continue
            for sketch in reversed(self.drawn):
                if sketch is section:
                    continue
                for chain, head, tail in chains(sketch):
                    if sized and len(chain) != wanted:
                        continue
                    from_head, from_tail = (float(np.linalg.norm(sketch.world(end) - start)) for end in (head, tail))
                    if best is None or min(from_head, from_tail) < best[0]:
                        best = (min(from_head, from_tail), sketch, chain if from_head <= from_tail else chain[::-1])
            if best is not None:
                break
        if best is None:
            return None
        logger.debug("%s: %s runs along %d curves of %s, %.3f mm from its profile", self.design.name, feature.name, len(best[2]), best[1].drawn_as, best[0])
        return best[1], [best[1].drawn_ids[curve.id] for curve in best[2]]

    # Holes
    def hole(self, feature: Feature) -> None:
        """
        Rebuild a hole feature.

        The file does not say which sketch points were drilled; the holes left
        in the finished solid do.

        Args:
        - feature: The timeline entry.
        """
        sketch = self.sketch_of(feature)
        diameter = feature.parameter("HoleDiameter")
        if sketch is None or diameter is None:
            self.done(feature, FAILED, "its sketch or diameter was not decoded")
            return
        radius = abs(diameter.value) * 10.0 / 2.0
        spots = self.hole_spots(sketch, radius)
        if not spots:
            self.done(feature, FAILED, f"no hole of diameter {round(radius * 2, 4)} mm survives in the finished solid to place it by")
            return
        params = self.hole_params(feature, radius)
        detail = f"{params['type']}, {len(spots)} position(s) inferred from the finished solid"
        # Holes by position are the reference: SolidCraft's hole at sketch points
        # does not always remove the same material, so it is kept only if it does.
        before, current = self.volume(), self.current()
        drilled, error = self.drill_by_position(feature, sketch, spots, params, current, record=False)
        removed = before - self.volume()
        for _ in range(drilled):
            self.session.undo()
        if not drilled:
            self.done(feature, FAILED, str(error))
            return
        why = self.drill_at_sketch_points(feature, sketch, spots, params, before, removed)
        if not why:
            self.done(feature, INFERRED, detail)
            return
        drilled, _ = self.drill_by_position(feature, sketch, spots, params, current, record=True)
        self.done(feature, INFERRED, f"{detail}; made as {drilled} separate hole(s) ({why})")

    def hole_spots(self, sketch: Sketch, radius: float) -> list[tuple[float, float]]:
        """
        Find where a hole feature drilled, from the cylinders left in the finished solid.

        Args:
        - sketch: The hole's sketch.
        - radius: The hole's radius, in millimetres.

        Returns:
        - The positions, as (u, v) in the sketch.
        """
        spots: dict[tuple, tuple[float, float]] = {}
        for origin, axis, size in self.design.cylinders:
            if abs(size - radius) < POINT_TOLERANCE and abs(abs(axis @ sketch.normal) - 1.0) < 1e-6:
                u, v = sketch.local(origin)
                spots.setdefault((round(u, 3), round(v, 3)), (u, v))
        return list(spots.values())

    def hole_params(self, feature: Feature, radius: float) -> dict:
        """
        Collect a hole's size parameters.

        Args:
        - feature: The hole.
        - radius: Its radius, in millimetres.

        Returns:
        - Diameter, depth, tip angle and counterbore for `solid.hole`.
        """
        params: dict = {"diameter": self.expression(feature, "HoleDiameter", radius * 2), "type": "simple"}
        depth, tip = feature.parameter("HoleDepth"), feature.parameter("TipAngle")
        if depth is not None:
            params["depth"] = self.expression(feature, "HoleDepth", abs(depth.value) * 10.0)
        if tip is not None:
            params["tip_angle"] = f"{round(math.degrees(abs(tip.value)), 6)} deg"
        bore, bore_depth = feature.parameter("CBDiameter"), feature.parameter("CBDepth")
        if bore is not None and bore_depth is not None:
            params |= {
                "type": "counterbore",
                "cb_diameter": self.expression(feature, "CBDiameter", abs(bore.value) * 10.0),
                "cb_depth": self.expression(feature, "CBDepth", abs(bore_depth.value) * 10.0),
            }
        return params

    def drill_by_position(self, feature: Feature, sketch: Sketch, spots: list, params: dict, current: Mesh | None, record: bool) -> tuple[int, object]:
        """
        Drill one hole per position, into whichever side has material.

        Args:
        - feature: The hole feature.
        - sketch: Its sketch.
        - spots: The positions, as (u, v) in the sketch.
        - params: The holes' size parameters.
        - current: The bodies as they stand, to tell which side has material.
        - record: Whether the holes go into the script; False for a trial.

        Returns:
        - How many holes SolidCraft drilled, and its last refusal.
        """
        drilled, error = 0, ""
        for number, spot in enumerate(spots, start=1):
            position = sketch.world(spot)
            into = 1.0 if current is not None and current.inside(position + PROBE * sketch.normal) else -1.0
            name = self.label(feature) if number == 1 else f"{self.label(feature)} ({number})"
            ok, error = self.session.execute("solid.hole", params | {"position": numbers(position), "direction": numbers(into * sketch.normal), "name": name}, record)
            drilled += ok
        return drilled, error

    def drill_at_sketch_points(self, feature: Feature, sketch: Sketch, spots: list, params: dict, before: float, removed: float) -> str:
        """
        Drill the holes as Fusion has them: one feature at points of the sketch.

        Args:
        - feature: The hole feature.
        - sketch: Its sketch.
        - spots: The positions, as (u, v) in the sketch.
        - params: The holes' size parameters.
        - before: The bodies' volume before drilling.
        - removed: The volume holes by position remove; the result must match it.

        Returns:
        - An empty string when the feature was made, else why it was not (and it is undone).
        """
        ok, info = self.session.execute("sketch.inspect", {"sketch": sketch.drawn_as}, record=False)
        drawn = [p for p in (info.get("points", []) if ok and isinstance(info, dict) else []) if p.get("id") != "origin"]
        ids = [next((p["id"] for p in drawn if math.dist(p["at"], spot) < POINT_TOLERANCE), None) for spot in spots]
        if not all(ids):
            return "the sketch has no point at every position"
        ok, error = self.session.execute("solid.hole", params | {"sketch": sketch.drawn_as, "points": ids, "name": self.label(feature)})
        if not ok:
            return str(error)
        if abs(before - self.volume() - removed) <= VOLUME_TOLERANCE * max(removed, 1.0):
            return ""
        self.session.undo()
        self.session.script.pop()
        return "SolidCraft's hole at sketch points removes the wrong material"

    # Construction
    def work_plane(self, feature: Feature) -> None:
        """
        Rebuild a work plane as an offset plane where the next sketch sits.

        How Fusion defined the plane is not decoded.

        Args:
        - feature: The timeline entry.
        """
        following = next((f for f in self.design.features if f.ordinal > feature.ordinal and f.kind == "Sketch"), None)
        sketch = self.design.sketches.get(following.definition.get("sketch", "")) if following else None
        if sketch is None:
            self.done(feature, SKIPPED, "its definition is not decoded and no sketch follows it")
            return
        for base, axis in (("XY", 2), ("XZ", 1), ("YZ", 0)):
            if abs(abs(sketch.normal[axis]) - 1.0) < 1e-9:
                offset = round(float(sketch.origin[axis]), 6)
                ok, error = self.session.execute("construct.plane.offset", {"base": base, "offset": f"{offset} mm", "name": self.label(feature)})
                detail = f"offset plane {offset} mm from {base}, taken from {following.name} (its own definition is not decoded)"
                self.done(feature, INFERRED if ok else FAILED, detail if ok else str(error))
                return
        self.done(feature, SKIPPED, "its definition is not decoded and the next sketch is not parallel to an origin plane")
