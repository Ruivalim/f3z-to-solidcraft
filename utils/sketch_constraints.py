from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from .conversion_common import POINT_TOLERANCE
from .fusion_design import Sketch
from .solvecraft_session import Session

logger = logging.getLogger(__name__)

#: Fusion constraints between two curves -> the SolidCraft command for each.
PAIRED = {
    "perpendicular": "sketch.constraint.perpendicular",
    "parallel": "sketch.constraint.parallel",
    "tangent": "sketch.constraint.tangent",
    "equal": "sketch.constraint.equal",
    "concentric": "sketch.constraint.concentric",
    "collinear": "sketch.constraint.collinear",
}
#: Every kind of Fusion constraint there is a SolidCraft command for.
KNOWN = set(PAIRED) | {"coincident", "coincident_loci", "horizontal", "vertical", "midpoint", "symmetric"}
#: The role ezf3d gives a parameter that is a sketch dimension: ``Linear Dimension-2``.
DIMENSION_ROLE = re.compile(r"^(Linear|Radial|Diameter) Dimension\b")
#: A drawn curve further than this (mm) from where it was is one a constraint moved.
MOVED = 1e-4
#: Two lengths closer than this (mm) are the same length.
SAME_LENGTH = 1e-6
#: What SolidCraft answers when a constraint adds nothing to those already there.
IMPLIED = "over-constrain"


@dataclass
class Carried:
    """
    What became of a sketch's constraints and dimensions.
    """

    #: Constraints SolidCraft now holds, and those that follow from how the sketch was drawn.
    constraints: int = 0
    #: Constraints the file lists for the sketch.
    listed: int = 0
    #: Constraints SolidCraft refused or that moved the sketch, and were taken back.
    refused: int = 0
    #: Kinds of constraint there is no SolidCraft command for.
    unknown: list[str] = field(default_factory=list)
    #: Kinds of constraint that refer to a curve or point the sketch was drawn without.
    undrawn: list[str] = field(default_factory=list)
    #: Dimensions placed, and dimensions the file lists for the sketch.
    dimensions: int = 0
    dimensions_listed: int = 0

    def summary(self) -> str:
        """
        Describe the outcome for the report.

        Returns:
        - One clause about the constraints and, when the sketch has dimensions, one about them.
        """
        if not self.listed and not self.dimensions_listed:
            return "it has no constraints or dimensions"
        text = f"{self.constraints} of {self.listed} constraints carried over"
        reasons = [f"{self.refused} refused by SolidCraft"] if self.refused else []
        if self.unknown:
            reasons.append(f"SolidCraft has no command for: {', '.join(sorted(set(self.unknown)))}")
        if self.undrawn:
            reasons.append(f"on a curve or point that was not drawn: {', '.join(sorted(set(self.undrawn)))}")
        if reasons:
            text += f" ({'; '.join(reasons)})"
        if self.dimensions_listed:
            text += f"; {self.dimensions} of {self.dimensions_listed} dimensions placed by their value (the file does not say what each one measures)"
        return text


class Constrainer:
    """
    Adds a Fusion sketch's constraints and dimensions to the sketch drawn for it.

    The sketch is drawn at its final coordinates, so every constraint already
    holds. One that SolidCraft refuses, or that moves a curve, is taken back:
    the sketch must stay where the features built on it expect it.
    """

    def __init__(self, session: Session, sketch: Sketch, design_name: str) -> None:
        """
        Get ready to constrain a sketch that is drawn and still open.

        Args:
        - session: The SolidCraft session, with the sketch active.
        - sketch: The Fusion sketch; its `drawn_ids` say which curve became which.
        - design_name: The document's name, for the log.
        """
        self.session = session
        self.sketch = sketch
        self.design_name = design_name
        self.shape = self.inspect()

    def inspect(self) -> dict:
        """
        Read the sketch as SolidCraft holds it.

        Returns:
        - Its ``curves`` and ``points``; empty lists when SolidCraft does not answer.
        """
        ok, info = self.session.execute("sketch.inspect", {}, record=False)
        if not ok or not isinstance(info, dict):
            logger.error("%s: SolidCraft could not inspect %s: %s", self.design_name, self.sketch.id, info)
            return {"curves": [], "points": []}
        return info

    @staticmethod
    def positions(shape: dict) -> dict[str, list[float]]:
        """
        List the numbers that place every curve of a sketch.

        Args:
        - shape: The sketch as `inspect` returns it.

        Returns:
        - Curve id -> its end points, centre and radius, whichever it has.
        """
        placed = {}
        for curve in shape.get("curves", []):
            numbers = [n for key in ("start_at", "end_at", "center_at") for n in curve.get(key) or []]
            placed[curve["id"]] = numbers + ([curve["radius"]] if curve.get("radius") is not None else [])
        return placed

    def moved(self) -> bool:
        """
        Tell whether the sketch is no longer where it was drawn.

        Returns:
        - True when a curve has moved, or SolidCraft no longer lists it.
        """
        before, after = self.positions(self.shape), self.positions(self.inspect())
        return any(
            name not in after or len(after[name]) != len(numbers) or any(abs(a - b) > MOVED for a, b in zip(after[name], numbers, strict=True))
            for name, numbers in before.items()
        )

    # References
    def point(self, entity: str) -> str | None:
        """
        Find the drawn point a Fusion sketch point became.

        Args:
        - entity: The Fusion point's id.

        Returns:
        - The id of the SolidCraft point at the same place, or None when there is none.
        """
        at = self.sketch.points.get(entity)
        if at is None:
            return None
        near = min(self.shape.get("points", []), key=lambda p: math.dist(p["at"], at), default=None)
        return near["id"] if near is not None and math.dist(near["at"], at) < POINT_TOLERANCE else None

    def resolve(self, reference: object) -> str | None:
        """
        Find what a constraint's reference became in SolidCraft.

        Args:
        - reference: A curve or point id, or a locus naming one.

        Returns:
        - The SolidCraft id of that curve or point, or None when it was not drawn.
        """
        if isinstance(reference, dict):
            reference = reference.get("entity") if reference.get("kind") == "entity" else None
        if not isinstance(reference, str):
            return None
        return self.sketch.drawn_ids.get(reference) or self.point(reference)

    def command(self, definition: dict) -> tuple[str, dict] | None:
        """
        Work out the SolidCraft command for one Fusion constraint.

        Args:
        - definition: The constraint as cadmpeg decodes it.

        Returns:
        - The command id and its parameters; None when there is no command for
          this kind, or something it refers to was not drawn.
        """
        kind = definition.get("kind")
        if kind in PAIRED:
            a, b = self.resolve(definition.get("first")), self.resolve(definition.get("second"))
            return (PAIRED[kind], {"a": a, "b": b}) if a and b else None
        if kind in ("coincident", "coincident_loci"):
            members = [self.resolve(member) for member in definition.get("entities") or definition.get("loci") or []]
            return ("sketch.constraint.coincident", {"a": members[0], "b": members[1]}) if len(members) == 2 and all(members) else None
        if kind in ("horizontal", "vertical"):
            line = self.resolve(definition.get("entity"))
            return ("sketch.constraint.horizontal_vertical", {"line": line, "mode": kind}) if line else None
        if kind == "midpoint":
            point, line = self.resolve(definition.get("point")), self.resolve(definition.get("entity"))
            return ("sketch.constraint.midpoint", {"point": point, "line": line}) if point and line else None
        if kind == "symmetric":
            a, b, line = (self.resolve(definition.get(key)) for key in ("first", "second", "axis"))
            return ("sketch.constraint.symmetry", {"a": a, "b": b, "line": line}) if a and b and line else None
        return None

    # Adding
    def add(self, command: str, params: dict) -> str:
        """
        Add one constraint or dimension, keeping it only if the sketch stays put.

        Args:
        - command: The command id.
        - params: Its parameters.

        Returns:
        - An empty string when it was added, else why it was not.
        """
        ok, result = self.session.execute(command, params)
        if not ok:
            return str(result)
        solved = not isinstance(result, dict) or result.get("sketch", {}).get("solved", True)
        if solved and not self.moved():
            return ""
        self.session.undo()
        self.session.script.pop()
        return "it could not be solved" if not solved else "it moved the sketch"

    def constraints(self, carried: Carried) -> None:
        """
        Add the sketch's constraints.

        Args:
        - carried: The tally, added to in place.
        """
        carried.listed = len(self.sketch.constraints)
        for definition in self.sketch.constraints:
            kind = definition.get("native_kind") or definition.get("kind") or "unknown"
            wanted = self.command(definition)
            if wanted is None:
                (carried.undrawn if definition.get("kind") in KNOWN else carried.unknown).append(kind)
                continue
            command, params = wanted
            if params.get("a") is not None and params.get("a") == params.get("b"):
                # Two Fusion curves drawn as one, or two points drawn as one: nothing left to constrain.
                carried.constraints += 1
                continue
            why = self.add(command, params)
            if not why or IMPLIED in why:
                # SolidCraft refuses a constraint that adds nothing: the ones already there imply it.
                carried.constraints += 1
            else:
                carried.refused += 1
                logger.debug("%s: %s of %s was not kept: %s", self.design_name, kind, self.sketch.drawn_as or self.sketch.id, why)

    def target(self, role: str, value: float, used: set[str]) -> tuple[str, str] | None:
        """
        Find a curve a dimension of a given value can measure.

        The file gives a dimension's value and whether it is a length, a radius
        or a diameter, but not what it measures; a curve of that size is taken.

        Args:
        - role: ``Linear``, ``Radial`` or ``Diameter``.
        - value: The dimension's value, in millimetres or as a radius for the round kinds.
        - used: Ids of the curves that have a dimension already.

        Returns:
        - The curve's id and the dimension type, or None when no curve has that size.
        """
        for curve in self.shape.get("curves", []):
            if curve["id"] in used or curve.get("construction"):
                continue
            if role == "Linear" and curve.get("type") == "line":
                size, kind = math.dist(curve["start_at"], curve["end_at"]), "length"
            elif role != "Linear" and curve.get("radius") is not None:
                size, kind = float(curve["radius"]), role.lower().replace("radial", "radius")
            else:
                continue
            if abs(size - value) < SAME_LENGTH:
                return curve["id"], kind
        return None

    def dimensions(self, parameters: list, names: dict[str, str], carried: Carried) -> None:
        """
        Place the sketch's dimensions on curves of their size.

        Args:
        - parameters: The parameters ezf3d lists for the sketch.
        - names: Fusion parameter name -> the name it has in SolidCraft.
        - carried: The tally, added to in place.
        """
        used: set[str] = set()
        for parameter in parameters:
            role = DIMENSION_ROLE.match(parameter.role or "")
            if role is None:
                continue
            carried.dimensions_listed += 1
            value_mm = abs(parameter.value) * 10.0
            found = self.target(role.group(1), value_mm / 2.0 if role.group(1) == "Diameter" else value_mm, used)
            if found is None:
                continue
            curve, kind = found
            value = names.get(parameter.name) or f"{round(value_mm, 6)} mm"
            why = self.add("sketch.dimension", {"entities": [curve], "type": kind, "value": value})
            if why:
                logger.debug("%s: dimension %s on %s of %s was not kept: %s", self.design_name, parameter.name, curve, self.sketch.drawn_as or self.sketch.id, why)
                continue
            used.add(curve)
            carried.dimensions += 1

    def run(self, parameters: list, names: dict[str, str]) -> Carried:
        """
        Add the sketch's constraints, then its dimensions.

        Args:
        - parameters: The parameters ezf3d lists for the sketch.
        - names: Fusion parameter name -> the name it has in SolidCraft.

        Returns:
        - What was carried over and what was not.
        """
        carried = Carried()
        self.constraints(carried)
        self.dimensions(parameters, names, carried)
        logger.info("%s: %s: %s", self.design_name, self.sketch.drawn_as or self.sketch.id, carried.summary())
        return carried
