from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import ezf3d
from ezf3d.container.archive import F3DArchive

from .conversion_common import (
    BOUNDS_TOLERANCE,
    DESIGN_SUFFIX,
    DESIGN_SUFFIXES,
    EXACT,
    FAILED,
    INFERRED,
    SCRIPT_NAME,
    SKIPPED,
    Part,
    Progress,
    default_paths,
    measured,
    slug,
)
from .conversion_errors import ConversionError
from .fusion_design import Design, dump_cadir, read_design
from .logging_utils import AppLogger
from .solid_oracle import Mesh
from .solvecraft_session import Config, Session, load_config
from .timeline_builder import Builder

logger = logging.getLogger(__name__)

#: The project folder, which holds main.py and config.yaml.
PROJECT_DIR = Path(__file__).absolute().parent.parent
#: The kinds of file a folder is searched for.
SOURCE_SUFFIXES = {".f3z", ".f3d"}
#: The configuration used when --config is not given.
DEFAULT_CONFIG = PROJECT_DIR / "config.yaml"

HELP_DESCRIPTION = """\
Recreate a Fusion .f3z/.f3d design in SolidCraft, feature by feature.

The Fusion timeline is replayed as SolidCraft features: the same sketches,
extrudes, holes and fillets, in the same order and under the same names. Each
part is then measured against its original solid. A report lists what
became of every timeline step: rebuilt as recorded, rebuilt with parts inferred
from the finished solid, failed, or skipped."""

HELP_EPILOG = """\
examples:
  main.py design.f3z                         convert, then ask whether to save or open
  main.py design.f3z --save                  save next to design.f3z without asking
  main.py design.f3z --save out\\part.solidcraft --force
  main.py design.f3z --save out\\part.json         save the design as plain JSON (SolidCraft 0.6+)
  main.py design.f3z --open                  open a temporary copy in the SolidCraft UI
  main.py design.f3z --save --exact-on-mismatch
  main.py design.f3z --solidcraft-dir C:\\Tools\\solidcraft --output-dir D:\\Converted
  main.py C:\\Archive --save                  convert every .f3z/.f3d in the folder, saving each beside its source
  main.py C:\\Archive --recursive --save --output-dir D:\\Converted --force
  main.py --gui                              open a window to drop files or folders on

saving writes three files side by side: NAME.solidcraft (the design),
NAME.script.json (the commands that build it) and NAME.report.txt.
SolidCraft 0.6 and newer write the design as a zip container, which 0.5 and
older cannot open; older builds write plain JSON under the same name.

a folder is converted file by file; one that fails is reported and the rest
carry on, and a summary is printed at the end. With --output-dir, files of the
same name in different subfolders would save to the same place.

default configuration: {config}
log file:              {log}"""
#: cadmpeg check errors about Fusion's own bookkeeping records, not about geometry.
HARMLESS_ERRORS = {"native_links"}
#: What ezf3d raises on an archive it cannot open.
ARCHIVE_ERRORS = (ezf3d.model.Ef3dError, OSError, zipfile.BadZipFile, struct.error, KeyError, ValueError)


def start_logging() -> Path:
    """
    Start logging to the converter's log file, the way QSnippet does.

    Returns:
    - The path of the log file.

    Raises:
    - ConversionError: If the log folder or file cannot be created.
    """
    try:
        log_dir = default_paths()["log_dir"]
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{SCRIPT_NAME}.log"
        AppLogger(log_filepath=str(log_path), log_level=logging.DEBUG)
    except (OSError, KeyError) as error:
        raise ConversionError(f"could not start logging: {error!r}") from error
    return log_path


def unpack_document(source: Path, doc, target: Path) -> None:
    """
    Write one document of an archive to a file of its own.

    Args:
    - source: The .f3z or .f3d file.
    - doc: The document as ezf3d opened it.
    - target: Where its .f3d is written.

    Raises:
    - ConversionError: If the document cannot be read from the archive or written.
    """
    try:
        if "!" in doc.source:
            with F3DArchive(source) as package:
                target.write_bytes(package.read(doc.source.split("!", 1)[1]))
        else:
            shutil.copyfile(source, target)
    except ARCHIVE_ERRORS as error:
        logger.error("Could not unpack %s from %s: %r", doc.name, source, error)
        raise ConversionError(f"could not unpack {doc.name} from {source.name}: {error}") from error


def read_parts(config: Config, source: Path, work: Path, shown: Progress) -> list[Part]:
    """
    Read every document of a Fusion archive.

    Args:
    - config: Where cadmpeg is.
    - source: The .f3z or .f3d file.
    - work: A folder for temporary files.
    - shown: The progress display.

    Returns:
    - One part per document, linked documents first: the root inserts them before it uses them.

    Raises:
    - ConversionError: If the archive or one of its documents cannot be read.
    """
    logger.info("Opening %s", source)
    shown.start(f"Opening {source.name}")
    parts: list[Part] = []
    try:
        root = ezf3d.readfile(source)
    except ARCHIVE_ERRORS as error:
        logger.error("Could not open %s: %r", source, error)
        raise ConversionError(f"could not open {source}: {error}") from error
    with root:
        documents = root.documents()
        # Per document: read it, convert its original solid, set its parameters.
        shown.expect(3 * len(documents))
        for doc in documents[1:] + documents[:1]:
            shown.start(f"{doc.name}: reading the timeline")
            f3d = work / f"{slug(doc.name)}.f3d"
            unpack_document(source, doc, f3d)
            design = read_design(doc, doc.name, doc is not root, f3d, dump_cadir(config.cadmpeg, f3d))
            parts.append(Part(design))
            shown.expect(len(design.features))
            shown.finish(f"{doc.name}: {len(design.features)} timeline steps, {len(design.sketches)} sketches, {len(design.parameters)} parameters")
    return parts


def convert_to_step(config: Config, part: Part) -> Path | None:
    """
    Have cadmpeg write a document's original solid as STEP.

    cadmpeg's check also covers Fusion's bookkeeping records, which fail on
    files whose geometry is fine, so the file is written despite errors and the
    errors that are about geometry are noted.

    Args:
    - config: Where cadmpeg is.
    - part: The document; notes about the conversion are added to it.

    Returns:
    - The STEP file, or None when cadmpeg could not write one.
    """
    step = part.design.f3d.with_suffix(".step")
    try:
        run = subprocess.run(
            [str(config.cadmpeg), "convert", str(part.design.f3d), "-o", str(step), "--allow-errors"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        logger.error("%s: cadmpeg could not be run: %s", part.design.name, error)
        part.notes.append(f"cadmpeg could not be run ({error}); nothing to check the rebuild against")
        return None
    output = run.stdout + run.stderr
    kinds = set(re.findall(r"\[error/(\w+)\]", output)) - HARMLESS_ERRORS
    if kinds:
        logger.error("%s: cadmpeg reported %s errors reading the original solid", part.design.name, sorted(kinds))
        part.notes.append(f"cadmpeg reported {', '.join(sorted(kinds))} errors reading the original solid")
    repaired = re.search(r"(\d+) signed or self-intersecting analytic surface", output)
    if repaired:
        part.notes.append(
            f"cadmpeg altered {repaired.group(1)} surface(s) of the original solid to write it as STEP; "
            "the original's volume, and so the check, can be off where those are"
        )
    if not step.is_file():
        logger.error("%s: cadmpeg wrote no STEP file (exit code %s): %s", part.design.name, run.returncode, output[-500:])
        part.notes.append("cadmpeg could not convert the original solid to STEP; nothing to check the rebuild against")
        return None
    return step


def read_original(config: Config, part: Part) -> None:
    """
    Read a document's original solid: as STEP, as SolidCraft measures it, and as triangles.

    Args:
    - config: Where cadmpeg and SolidCraft are.
    - part: The document; its `step`, `reference` and `final` are set when available.
    """
    logger.info("%s: converting the original solid", part.design.name)
    step = convert_to_step(config, part)
    if step is None:
        return
    stl = step.with_suffix(".stl")
    try:
        run = subprocess.run([str(config.cli), "eval", str(step)], capture_output=True, text=True, encoding="utf-8")
        reference = measured(json.loads(run.stdout).get("bodies", []))
        subprocess.run([str(config.cli), "run", "--in", str(step), "--out", str(stl), "--quiet"], capture_output=True)
    except (OSError, json.JSONDecodeError) as error:
        logger.error("%s: SolidCraft could not read %s: %r", part.design.name, step, error)
        reference = None
    if reference is None:
        part.notes.append("SolidCraft could not read the STEP of the original solid; nothing to check the rebuild against")
        return
    part.step, part.reference = step, reference
    if stl.is_file():
        part.final = Mesh.load(stl)
    logger.info("%s: original solid %s mm, %d faces", part.design.name, reference.size, reference.faces)


def insert_original(session: Session, builder: Builder, part: Part, shown: Progress) -> None:
    """
    Rebuild a part by inserting its original solid, without feature history.

    Args:
    - session: The SolidCraft session.
    - builder: The part's builder, for its parameters and body bookkeeping.
    - part: The part; its `result` is set.
    - shown: The progress display.
    """
    name = part.design.name
    builder.parameters()
    shown.start(f"{name}: inserting the original solid")
    ok, error = session.execute("file.insert_step", {"path": str(part.step), "name": f"{name} (exact solid)"})
    if not ok:
        logger.error("%s: the original solid could not be inserted: %s", name, error)
    shown.finish(f"{name}: original solid inserted" if ok else f"{name}: {error}", "" if ok else FAILED)
    for index, body in enumerate(builder.refresh(), start=1):
        wanted = name if index == 1 else f"{name} {index}"
        if body["name"] != wanted:
            session.execute("body.rename", {"body": body["name"], "name": wanted})
    part.result = measured(builder.refresh())


def rebuild(config: Config, parts: list[Part], work: Path, shown: Progress) -> Session:
    """
    Build the whole design in a new session, each part by its route.

    Args:
    - config: Where SolidCraft is.
    - parts: The documents, linked ones first.
    - work: A folder for temporary files.
    - shown: The progress display.

    Returns:
    - The session, holding the design and the script that made it.

    Raises:
    - ConversionError: If SolidCraft cannot be started or stops answering.
    """
    session = Session(config.cli, [work] + [part.step.parent for part in parts if part.step is not None])
    components: list[Design] = []
    try:
        for part in parts:
            design = part.design
            part.outcomes, part.result = [], None
            if design.linked:
                session.execute("component.create", {"name": design.name})
                components.append(design)
            builder = Builder(session, part, work, [] if design.linked else components, shown)
            if part.route == "history":
                builder.run()
            else:
                insert_original(session, builder, part, shown)
            if design.linked:
                session.execute("component.activate", {"component": "root"})
    except BaseException:
        # Failed or cancelled part way: SolidCraft is not left running.
        session.close()
        raise
    return session


def check(part: Part) -> str:
    """
    Compare a rebuilt part with its original.

    Args:
    - part: The part.

    Returns:
    - Why the rebuilt part is not its original, or an empty string when it is.
    """
    if part.result is None:
        return "no body was made"
    if part.reference is not None:
        return part.result.differs_from(part.reference)
    if part.design.bounds is not None:
        low, high = part.design.bounds
        apart = max(abs(a - b) for a, b in zip(low + high, part.result.low + part.result.high, strict=True))
        return "" if apart <= BOUNDS_TOLERANCE else f"bounds differ from the original's by {apart:.3f} mm"
    return "there is no original solid to check against"


def part_report(part: Part) -> list[str]:
    """
    Describe what became of one part.

    Args:
    - part: The part.

    Returns:
    - The lines of the report about it.
    """
    design = part.design
    if part.route == "exact":
        lines = [f"{design.name}: original solid inserted (no feature history)"]
    else:
        counts = {status: sum(o.status == status for o in part.outcomes) for status in (EXACT, INFERRED, FAILED, SKIPPED)}
        lines = [
            f"{design.name}: {len(part.outcomes)} timeline steps: {counts[EXACT]} as recorded, "
            f"{counts[INFERRED]} with parts inferred, {counts[FAILED]} failed, {counts[SKIPPED]} skipped"
        ]
        width = max((len(o.feature) for o in part.outcomes), default=0)
        lines += [f"  {number:>3}  {o.feature:<{width}}  {o.status:<8}  {o.detail}" for number, o in enumerate(part.outcomes, start=1)]
    if part.result is not None:
        lines.append(f"    result: size {part.result.size} mm, volume {part.result.volume:.1f} mm3, {part.result.faces} faces")
    if part.reference is not None:
        lines.append(f"    original: size {part.reference.size} mm, volume {part.reference.volume:.1f} mm3, {part.reference.faces} faces")
    problem = check(part)
    lines.append(f"    check: {'MISMATCH: ' + problem if problem else 'same volume and bounds as the original solid'}")
    return lines + [f"    note: {note}" for note in design.notes + part.notes] + [""]


def report(parts: list[Part]) -> str:
    """
    Describe the whole run.

    Args:
    - parts: The documents, rebuilt.

    Returns:
    - The report as text, ending with whether every part matches its original.
    """
    lines = [line for part in parts for line in part_report(part)]
    consistent = not any(check(part) for part in parts)
    lines.append("RESULT: " + ("every part matches its original" if consistent else "NOT every part matches its original"))
    return "\n".join(lines)


def write_script(commands: list[dict], source: Path, path: Path) -> None:
    """
    Write the commands that rebuilt the design as a SolidCraft script.

    Any STEP file the script inserts is copied next to it, so the script does
    not depend on a temporary folder.

    Args:
    - commands: The commands, in order.
    - source: The Fusion file they came from, for the description.
    - path: Where the script is written; its name ends in ``.script.json``.

    Raises:
    - ConversionError: If the script or a STEP file cannot be written.
    """
    written = []
    try:
        for entry in commands:
            params = dict(entry["params"])
            if entry["command"] == "file.insert_step":
                copy = path.with_name(f"{path.name.removesuffix('.script.json')}.{Path(params['path']).name}")
                if copy != Path(params["path"]):
                    shutil.copyfile(params["path"], copy)
                params["path"] = str(copy)
            written.append({"command": entry["command"], "params": params})
        script = {"description": f"Rebuilt from {source.name} by f3z2solvecraft (see the report).", "commands": written}
        path.write_text(json.dumps(script, indent=1), encoding="utf-8")
    except OSError as error:
        logger.error("Could not write the script %s: %s", path, error)
        raise ConversionError(f"could not write the script {path}: {error}") from error
    logger.info("Wrote %d commands to %s", len(written), path)


def save_design(config: Config, script: Path, design: Path) -> None:
    """
    Run a script through SolidCraft and save the design it builds.

    Args:
    - config: Where SolidCraft is.
    - script: The script.
    - design: Where the design is saved; SolidCraft 0.6 writes plain JSON
      for a ``.json`` name and a zip container for any other.

    Raises:
    - ConversionError: If SolidCraft cannot be run or fails to build or save the design.
    """
    try:
        subprocess.run([str(config.cli), "run", str(script), "--save", str(design), "--quiet"], check=True, capture_output=True, text=True, encoding="utf-8")
    except subprocess.CalledProcessError as error:
        logger.error("SolidCraft could not build %s from %s (code %s): %s", design, script, error.returncode, error.stderr)
        raise ConversionError(f"SolidCraft could not save {design}: {(error.stderr or '').strip()[-500:]}") from error
    except OSError as error:
        logger.error("SolidCraft could not be run to save %s: %s", design, error)
        raise ConversionError(f"SolidCraft could not be run: {error}") from error
    logger.info("Saved %s", design)


def open_design(config: Config, design: Path) -> None:
    """
    Open a design in the SolidCraft desktop application.

    Args:
    - config: Where SolidCraft is.
    - design: The design to open.

    Raises:
    - ConversionError: If the application cannot be started.
    """
    try:
        subprocess.Popen([str(config.app), str(design)])
    except OSError as error:
        logger.error("Could not start %s: %s", config.app, error)
        raise ConversionError(f"could not start {config.app}: {error}") from error
    logger.info("Opened %s in the SolidCraft UI", design)


def ask(question: str) -> str:
    """
    Ask a question at the terminal.

    Args:
    - question: The prompt.

    Returns:
    - The answer with surrounding spaces removed; empty when there is no
      input to read, which every caller treats as the default.
    """
    try:
        return input(question).strip()
    except EOFError:
        logger.error("No input to read for the question %r; taking the default", question.strip())
        print()
        return ""


def choose_action(args, many: bool = False) -> str:
    """
    Decide what to do with the rebuilt design, or with each of several.

    Args:
    - args: The command line; ``--save`` and ``--open`` answer without asking.
    - many: Whether a folder of files is being converted; the question then covers them all.

    Returns:
    - ``s`` to save, ``o`` to open, or anything else to do neither.
    """
    if args.save is not None:
        return "s"
    if args.open:
        return "o"
    if not sys.stdin.isatty():
        print("\nNothing will be saved: run again with --save or --open." if many else "\nNothing saved. Run again with --save [PATH] or --open.")
        return ""
    if many:
        return ask("[s] save each design with its script   [o] open each in the SolidCraft UI   [n] report only: ").lower()[:1]
    return ask("\n[s] save the design with the script   [o] open in the SolidCraft UI   [q] quit: ").lower()[:1]


def choose_target(args, default: Path, ask_path: bool = True) -> Path | None:
    """
    Decide where the design is saved, asking when the terminal allows.

    Args:
    - args: The command line; ``--save PATH`` names the place, ``--force`` allows overwriting.
    - default: Where it goes when no place is given.
    - ask_path: Whether the place may be asked for; False when converting a folder.

    Returns:
    - The design's path, or None when an existing design is not to be overwritten.
      A name given with ``.json`` or ``.solvecraft`` keeps it; any other ends in ``.solidcraft``.
    """
    target = Path(args.save) if args.save else None
    # --save without a path means the default place, without asking; the place
    # is asked for only when saving was chosen at the prompt.
    if ask_path and args.save is None and sys.stdin.isatty():
        typed = ask(f"Save as [{default}]: ").strip('"')
        target = Path(typed) if typed else None
    design = target or default
    if design.suffix.lower() not in DESIGN_SUFFIXES:
        design = design.with_suffix(DESIGN_SUFFIX)
    if design.exists() and not args.force:
        if not sys.stdin.isatty() or ask(f"{design} exists. Overwrite? [y/N]: ").lower() != "y":
            print("Not overwritten (use --force).")
            logger.info("%s exists and was not overwritten", design)
            return None
    return design


def save_result(config: Config, source: Path, commands: list[dict], text: str, design: Path, also_open: bool) -> None:
    """
    Save the design, the script that builds it and the report, side by side.

    Args:
    - config: Where SolidCraft is.
    - source: The Fusion file.
    - commands: The commands that rebuilt the design.
    - text: The report.
    - design: Where the design is saved.
    - also_open: Whether to open the saved design in the UI.

    Raises:
    - ConversionError: If a file cannot be written or SolidCraft fails.
    """
    script, notes = design.with_suffix(".script.json"), design.with_suffix(".report.txt")
    try:
        design.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(text + "\n", encoding="utf-8")
    except OSError as error:
        logger.error("Could not write %s: %s", notes, error)
        raise ConversionError(f"could not write {notes}: {error}") from error
    write_script(commands, source, script)
    save_design(config, script, design)
    print(f"Saved {design}\n      {script}\n      {notes}")
    if also_open:
        open_design(config, design)


def open_result(config: Config, source: Path, commands: list[dict], work: Path) -> None:
    """
    Build the design in a temporary folder and open it in the UI.

    Args:
    - config: Where SolidCraft is.
    - source: The Fusion file.
    - commands: The commands that rebuilt the design.
    - work: The temporary folder.

    Raises:
    - ConversionError: If a file cannot be written or SolidCraft fails.
    """
    script, design = work / f"{source.stem}.script.json", work / f"{source.stem}{DESIGN_SUFFIX}"
    write_script(commands, source, script)
    save_design(config, script, design)
    open_design(config, design)
    print(f"Opened {design} (a temporary copy; use Save As in the app to keep it)")


def finish(config: Config, source: Path, commands: list[dict], work: Path, text: str, args, choice: str | None = None) -> None:
    """
    Save the design through the script, open it in the app, or neither.

    Args:
    - config: Where SolidCraft is and where output goes.
    - source: The Fusion file.
    - commands: The commands that rebuilt the design.
    - work: The temporary folder.
    - text: The report.
    - args: The command line.
    - choice: What to do, when already decided for a whole folder; None to
      decide now, asking if the command line does not say.

    Raises:
    - ConversionError: If saving or opening fails.
    """
    decided = choice is not None
    choice = choice if decided else choose_action(args)
    if choice == "s":
        design = choose_target(args, (config.output_dir or source.parent) / f"{source.stem}{DESIGN_SUFFIX}", ask_path=not decided)
        if design is not None:
            save_result(config, source, commands, text, design, args.open)
    elif choice == "o":
        open_result(config, source, commands, work)
    else:
        logger.info("Nothing saved or opened")


def make_work_folder() -> Path:
    """
    Create the temporary folder one conversion works in.

    Returns:
    - The new folder.

    Raises:
    - ConversionError: If the folder cannot be created.
    """
    try:
        return Path(tempfile.mkdtemp(prefix="f3z2solvecraft-"))
    except OSError as error:
        logger.error("Could not create a temporary folder: %s", error)
        raise ConversionError(f"could not create a temporary folder: {error}") from error


def convert(config: Config, source: Path, work: Path, exact_on_mismatch: bool, display: Progress | None = None) -> tuple[list[Part], list[dict]]:
    """
    Read a Fusion file and rebuild it in SolidCraft.

    Args:
    - config: Where the tools are.
    - source: The .f3z or .f3d file.
    - work: A folder for temporary files.
    - exact_on_mismatch: Whether a part whose rebuilt history does not match
      its original gets the original solid inserted instead.
    - display: Where progress is shown; None for the bar on the terminal. The
      window passes its own, which has the same methods.

    Returns:
    - The parts with their outcomes, and the commands that rebuilt the design.

    Raises:
    - ConversionError: If the file cannot be read or SolidCraft fails.
    """
    with display if display is not None else Progress() as shown:
        parts = read_parts(config, source, work, shown)
        for part in parts:
            shown.start(f"{part.design.name}: converting the original solid")
            read_original(config, part)
            found = f"{part.reference.size} mm, {part.reference.faces} faces" if part.reference else "not available"
            shown.finish(f"{part.design.name}: original solid {found}")
        session = rebuild(config, parts, work, shown)
        session.close()
        mismatched = [part for part in parts if check(part) and part.step is not None]
        if exact_on_mismatch and mismatched:
            logger.info("Inserting the original solid for %s", [part.design.name for part in mismatched])
            for part in mismatched:
                part.route = "exact"
            # A second pass: parts that matched are replayed, the rest get their original solid.
            shown.expect(sum(1 + (len(p.design.features) if p.route == "history" else 1) for p in parts))
            session = rebuild(config, parts, work, shown)
            session.close()
        shown.start("Done")
    return parts, session.script


def parse_arguments() -> argparse.Namespace:
    """
    Read the command line.

    Returns:
    - The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG.format(config=DEFAULT_CONFIG, log=default_paths()["log_dir"] / f"{SCRIPT_NAME}.log"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", type=Path, nargs="?", metavar="SOURCE", help="the Fusion .f3z or .f3d file to convert, or a folder of them; optional with --gui")
    parser.add_argument("--recursive", action="store_true", help="when SOURCE is a folder, convert the files in its subfolders too")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="open a window to drop files or folders on instead of converting at the terminal; SOURCE and the other options set what the window starts with",
    )

    result = parser.add_argument_group("what to do with the result (asked when neither is given: at the end for a file, once at the start for a folder)")
    result.add_argument(
        "--save",
        nargs="?",
        const="",
        metavar="PATH",
        help="save the design, its script and the report without asking; PATH is the .solidcraft file, or a .json name for plain JSON, for a single file only "
        "(default: the source's name in the output folder)",
    )
    result.add_argument("--open", action="store_true", help="open the result in the SolidCraft UI; with --save, opens the saved design")
    result.add_argument("--force", action="store_true", help="overwrite an existing design instead of asking")

    rebuild_options = parser.add_argument_group("how to rebuild")
    rebuild_options.add_argument(
        "--exact-on-mismatch",
        action="store_true",
        help="for a part whose rebuilt history does not match the original solid, insert the original solid instead (no feature history for that part)",
    )

    settings = parser.add_argument_group("configuration (each option overrides the same setting in the configuration file)")
    settings.add_argument("--config", type=Path, metavar="FILE", help="YAML configuration file (default: config.yaml beside main.py, if there is one)")
    settings.add_argument(
        "--solidcraft-dir",
        "--solvecraft-dir",
        dest="solidcraft_dir",
        metavar="DIR",
        help="folder holding solidcraft-cli.exe and solidcraft.exe (solvecraft-cli.exe and solvecraft.exe before SolidCraft 0.5)",
    )
    settings.add_argument("--cadmpeg", metavar="FILE", help="path of cadmpeg.exe")
    settings.add_argument("--output-dir", metavar="DIR", help="folder saved designs go to (default: beside each source file)")
    args = parser.parse_args()
    if args.source is None and not args.gui:
        parser.error("SOURCE is required unless --gui is given")
    return args


def config_path(args: argparse.Namespace) -> Path | None:
    """
    Decide which configuration file is read.

    Args:
    - args: The command line.

    Returns:
    - The file named by ``--config``, else config.yaml beside main.py when there is one, else None.
    """
    if args.config is not None:
        return args.config
    return DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None


def configuration(args: argparse.Namespace) -> Config:
    """
    Put the configuration together from the file and the command line.

    Args:
    - args: The command line.

    Returns:
    - The configuration, with command-line settings taking precedence.

    Raises:
    - ConversionError: If the file cannot be read or a tool is not where the settings say.
    """
    overrides = {"solidcraft_dir": args.solidcraft_dir, "cadmpeg": args.cadmpeg, "output_dir": args.output_dir}
    return load_config(config_path(args), overrides)


def convert_file(config: Config, source: Path, args: argparse.Namespace, choice: str | None = None) -> bool:
    """
    Convert one Fusion file, print its report, and save or open the result.

    Args:
    - config: Where the tools are and where output goes.
    - source: The .f3z or .f3d file.
    - args: The command line.
    - choice: What to do with the result, when already decided for a whole folder.

    Returns:
    - Whether every part of the design matches its original.

    Raises:
    - ConversionError: If the conversion cannot be done.
    """
    logger.info("Converting %s", source)
    work = make_work_folder()
    parts, commands = convert(config, source, work, args.exact_on_mismatch)
    text = report(parts)
    logger.info("Report for %s:\n%s", source.name, text)
    print()
    print(text)
    finish(config, source, commands, work, text, args, choice)
    return not any(check(part) for part in parts)


def find_sources(folder: Path, recursive: bool) -> list[Path]:
    """
    List the Fusion files in a folder.

    Args:
    - folder: The folder.
    - recursive: Whether to look in its subfolders too.

    Returns:
    - Its .f3z and .f3d files, sorted by path.

    Raises:
    - ConversionError: If the folder cannot be read or holds no Fusion file.
    """
    try:
        found = folder.rglob("*") if recursive else folder.iterdir()
        sources = sorted(path for path in found if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES)
    except OSError as error:
        logger.error("Could not list %s: %s", folder, error)
        raise ConversionError(f"could not list {folder}: {error}") from error
    if not sources:
        where = "or its subfolders" if recursive else "(use --recursive to look in subfolders)"
        raise ConversionError(f"no .f3z or .f3d file in {folder} {where}")
    logger.info("Found %d Fusion files in %s%s", len(sources), folder, " and its subfolders" if recursive else "")
    return sources


def convert_folder(config: Config, sources: list[Path], args: argparse.Namespace) -> int:
    """
    Convert every Fusion file of a folder, carrying on past one that fails.

    What to do with the results is decided once, before the first file, so the
    run does not stop to ask after each.

    Args:
    - config: Where the tools are and where output goes.
    - sources: The files, in order.
    - args: The command line.

    Returns:
    - How many files could not be converted.
    """
    print(f"{len(sources)} Fusion files to convert.")
    choice = choose_action(args, many=True)
    results: list[tuple[Path, str]] = []
    for number, source in enumerate(sources, start=1):
        print(f"\n[{number}/{len(sources)}] {source}")
        try:
            matches = convert_file(config, source, args, choice)
            results.append((source, "matches its original" if matches else "converted; does NOT match its original (see its report)"))
        except ConversionError as error:
            logger.error("Could not convert %s: %s", source, error, exc_info=True)
            print(f"error: {error}", file=sys.stderr)
            results.append((source, f"FAILED: {error}"))
    failed = sum(1 for _, outcome in results if outcome.startswith("FAILED"))
    summary = "\n".join(f"  {source.name}: {outcome}" for source, outcome in results)
    logger.info("Folder done: %d of %d converted\n%s", len(sources) - failed, len(sources), summary)
    print(f"\nSUMMARY: {len(sources) - failed} of {len(sources)} files converted\n{summary}")
    return failed


def run(args: argparse.Namespace) -> int:
    """
    Convert the file or folder named on the command line and save or open the results.

    Args:
    - args: The command line.

    Returns:
    - How many files could not be converted; always 0 for a single file,
      whose failure is raised instead.

    Raises:
    - ConversionError: If the source is not there, the configuration is not
      usable, or a single file cannot be converted.
    """
    source = args.source
    if source.is_file():
        convert_file(configuration(args), source, args)
        return 0
    if not source.is_dir():
        raise ConversionError(f"not a file or a folder: {source}")
    if args.save:
        raise ConversionError("--save PATH names one design; for a folder use --save with --output-dir DIR")
    return convert_folder(configuration(args), find_sources(source, args.recursive), args)


def run_gui(args: argparse.Namespace, log_path: Path) -> int:
    """
    Open the converter's window and wait until it is closed.

    Args:
    - args: The command line; it sets what the window starts with.
    - log_path: The log file, which the window points to.

    Returns:
    - Always 0: the window reports files that could not be converted itself.

    Raises:
    - ConversionError: If PySide6, which the window is built with, is not installed.
    """
    try:
        # Imported here so converting at the terminal does not need PySide6.
        from ui.window import launch
    except ImportError as error:
        logger.error("The window could not be loaded: %r", error)
        raise ConversionError(f"--gui needs PySide6, which could not be loaded ({error}); run make create-venv to install it") from error
    logger.info("Window closed with code %s", launch(args, log_path))
    return 0


def main() -> int:
    """
    Run the converter, logging its start, its end and anything that stops it.

    Returns:
    - The process exit code: 0 on success, 1 when a conversion could not be done.
    """
    args = parse_arguments()
    try:
        log_path = start_logging()
    except ConversionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    logger.info("Started: %s", " ".join(sys.argv))
    try:
        failed = run_gui(args, log_path) if args.gui else run(args)
    except ConversionError as error:
        logger.error("Stopped: %s", error, exc_info=True)
        print(f"error: {error} (details in {log_path})", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted by the user")
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception:
        # Nothing is expected here; record it in full and let it surface.
        logger.exception("Stopped by an unexpected error")
        raise
    if failed:
        logger.error("Finished with %d file(s) not converted", failed)
        print(f"{failed} file(s) could not be converted (details in {log_path})", file=sys.stderr)
        return 1
    logger.info("Finished")
    return 0
