"""
Saving, opening and autosaving a sheet.

Before this module there was no way to keep a session at all: the only
thing the app could write was the finished PDF, so closing the window -
or any of the crashes fixed alongside this - took every placement, crop
and label with it.  That is the "my progress is gone" report, and no
amount of crash-proofing answers it on its own.

A project is a zip with a ``.tdsheet`` extension:

    project.json        the whole ProjectState, as plain data
    assets/<id>.png     one file per imported image
    panel.json          the side panel's cell values

The images go in the file rather than being referenced, because the
source DWG is often somewhere the architect will not think to keep, and
a project that reopens with empty frames is not a saved project.  PNG
keeps line art exact; a JPEG-sourced photo is re-encoded once, losslessly
from the decoded pixels.

DESIGN RULE (section 9): no Qt imports here.  The UI decides when to
save; this module only knows how.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import asdict
from typing import Any, Callable, Optional

from PIL import Image

from app.core.project_state import (
    Annotation,
    Callout,
    ImportedAsset,
    PlacedItem,
    ProjectMetadata,
    ProjectState,
    ReferenceDrawing,
    RevisionEntry,
    SheetTemplate,
)

PROJECT_EXTENSION = ".tdsheet"
PROJECT_FILTER = "Drawing Sheet Project (*.tdsheet);;All Files (*)"

# Bumped whenever the on-disk shape changes in a way an older build
# could not read.  A newer file opened by an older app is refused with a
# clear message rather than half-loaded.
FORMAT_VERSION = 1

LogCallback = Callable[[str, str], None]


def _noop_log(severity: str, message: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Tuples survive a JSON round trip as lists, so they are put back by hand
# ---------------------------------------------------------------------------

def _as_tuple(value) -> Optional[tuple]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return None


def _rect4(value, fallback=(0.0, 0.0, 100.0, 100.0)) -> tuple:
    t = _as_tuple(value)
    if t is None or len(t) != 4:
        return fallback
    try:
        return tuple(float(v) for v in t)
    except (TypeError, ValueError):
        return fallback


def _point2(value) -> Optional[tuple]:
    t = _as_tuple(value)
    if t is None or len(t) != 2:
        return None
    try:
        return (float(t[0]), float(t[1]))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Serialise
# ---------------------------------------------------------------------------

def state_to_dict(state: ProjectState,
                  panel_values: Optional[dict] = None) -> dict:
    """Everything about the sheet except the image pixels."""
    template = None
    if state.template is not None:
        template = asdict(state.template)

    return {
        "format_version": FORMAT_VERSION,
        "template": template,
        "metadata": asdict(state.metadata),
        "assets": [
            {
                "id": a.id,
                "source_path": a.source_path,
                "source_type": a.source_type,
                "vector_source_path": a.vector_source_path,
                "dwg_source_path": a.dwg_source_path,
                "dwg_component_id": a.dwg_component_id,
                "has_image": a.image is not None,
            }
            for a in state.assets.values()
        ],
        "placed_items": [asdict(pi) for pi in state.placed_items],
        "annotations": [asdict(a) for a in state.annotations],
        "callouts": [asdict(c) for c in state.callouts],
        "groups": dict(state.groups),
        "revision_history": [asdict(r) for r in state.revision_history],
        "reference_drawings": [asdict(r) for r in state.reference_drawings],
        "panel_values": dict(panel_values or {}),
    }


def _write_asset_png(archive: zipfile.ZipFile, asset: ImportedAsset) -> None:
    """Store one asset's pixels, without letting a bad one stop the save."""
    if asset.image is None:
        return
    buffer = io.BytesIO()
    image = asset.image
    # A palette or CMYK image cannot be written as PNG untouched.
    if image.mode not in ("RGB", "RGBA", "L", "LA", "P", "1"):
        image = image.convert("RGB")
    image.save(buffer, format="PNG", optimize=False, compress_level=4)
    archive.writestr(f"assets/{asset.id}.png", buffer.getvalue())


def _write_asset_vector(archive: zipfile.ZipFile, asset: ImportedAsset) -> None:
    """Store the vector line art alongside the raster, if there is any.

    Without this a reopened project quietly lost its crispness: the path
    recorded in the manifest points into a session temp folder that is
    gone by the next launch, so the compositor fell back to the bitmap
    and the sheet printed softer than the one saved the day before.
    """
    source = asset.vector_source_path
    if not source or not os.path.isfile(source):
        return
    archive.write(source, f"assets/{asset.id}.pdf")


def _put_in_place(temp_path: str, path: str) -> None:
    """Move the finished archive onto *path*, however the volume allows.

    ``os.replace`` is the right answer: it is atomic, so a save
    interrupted halfway - by the very crash the autosave exists to
    survive - cannot leave a truncated file where the good one was.

    It is not always available.  Under an MSIX or AppContainer package,
    and on some redirected or synced profiles, %LOCALAPPDATA% is a
    reparse point onto another volume: Windows then sees the two paths as
    different devices and refuses *every* rename inside that folder with
    ERROR_NOT_SAME_DEVICE, even when both names sit in the same listing.
    Left at ``os.replace`` alone the autosave simply never wrote anything
    on those machines, which is the one place it is least excusable.

    The fallback keeps the previous version alongside while the copy is
    in flight and puts it back if the copy fails, so the worst case is
    the older sheet rather than no sheet.
    """
    try:
        os.replace(temp_path, path)
        return
    except OSError:
        pass

    backup = path + ".bak"
    had_previous = os.path.isfile(path)
    if had_previous:
        try:
            shutil.copyfile(path, backup)
        except OSError:
            had_previous = False

    try:
        shutil.copyfile(temp_path, path)
    except OSError:
        if had_previous:
            try:
                shutil.copyfile(backup, path)
            except OSError:
                pass
        raise
    finally:
        for leftover in (temp_path, backup if had_previous else None):
            if leftover:
                try:
                    os.unlink(leftover)
                except OSError:
                    pass


def save_project(
    state: ProjectState,
    path: str,
    panel_values: Optional[dict] = None,
    log: LogCallback = _noop_log,
) -> str:
    """Write the whole session to *path*.

    The archive is built beside its destination and only put in place
    once it is complete - see :func:`_put_in_place` for how, and for the
    volumes where the atomic route is not on offer.
    """
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)

    handle, temp_path = tempfile.mkstemp(
        suffix=".tmp", prefix=".tdsheet-", dir=parent
    )
    os.close(handle)

    try:
        with zipfile.ZipFile(
            temp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr(
                "project.json",
                json.dumps(state_to_dict(state, panel_values), indent=1),
            )
            for asset in state.assets.values():
                try:
                    _write_asset_png(archive, asset)
                except Exception as exc:  # noqa: BLE001
                    log("warning",
                        f"Could not store the image for {asset.id}: {exc}")
                try:
                    _write_asset_vector(archive, asset)
                except Exception as exc:  # noqa: BLE001
                    log("warning",
                        f"Could not store the line art for {asset.id}: {exc}")

        _put_in_place(temp_path, path)
        log("success", f"Project saved: {os.path.basename(path)}")
        return path
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Deserialise
# ---------------------------------------------------------------------------

def _load_metadata(data: dict) -> ProjectMetadata:
    metadata = ProjectMetadata()
    for key, value in (data or {}).items():
        if hasattr(metadata, key):
            setattr(metadata, key, value)
    return metadata


def _load_template(data: Optional[dict]) -> Optional[SheetTemplate]:
    if not data:
        return None
    try:
        return SheetTemplate(
            template_name=data.get("template_name", ""),
            base_pdf_path=data.get("base_pdf_path", ""),
            paper_size=data.get("paper_size", "A1"),
            orientation=data.get("orientation", "landscape"),
            guide_rects=data.get("guide_rects") or {},
            callout_column=data.get("callout_column") or {},
        )
    except Exception:  # noqa: BLE001
        return None


def _load_placed_item(data: dict) -> Optional[PlacedItem]:
    try:
        return PlacedItem(
            id=data["id"],
            item_type=data.get("item_type", "image"),
            source_asset_id=data["source_asset_id"],
            crop_box=_as_tuple(data.get("crop_box")),
            page_rect=_rect4(data.get("page_rect")),
            rotation=float(data.get("rotation") or 0.0),
            circular_mask=bool(data.get("circular_mask")),
            z_order=int(data.get("z_order") or 0),
            group_id=data.get("group_id"),
            callout_id=data.get("callout_id"),
            description=data.get("description"),
            leader_style=data.get("leader_style") or "dashed",
            leader_target_page_pos=_point2(data.get("leader_target_page_pos")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_annotation(data: dict) -> Optional[Annotation]:
    try:
        kind = data.get("kind")
        if kind not in Annotation.KINDS:
            return None
        return Annotation(
            id=data["id"],
            kind=kind,
            text=str(data.get("text", "")),
            page_pos=_point2(data.get("page_pos")) or (0.0, 0.0),
            target_page_pos=_point2(data.get("target_page_pos")),
            font_size=float(data.get("font_size") or 11.0),
            rule_width=float(data.get("rule_width") or 0.0),
            group_id=data.get("group_id"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_project(
    path: str,
    log: LogCallback = _noop_log,
    vector_dir: Optional[str] = None,
) -> tuple[ProjectState, dict]:
    """Rebuild a ProjectState from a ``.tdsheet`` file.

    Returns ``(state, panel_values)``.  Raises on a file that is not a
    project at all; anything merely damaged inside is reported through
    *log* and skipped, so a single bad record never costs the rest.

    ``vector_dir`` is where the stored line art is unpacked to.  The
    caller should pass a folder it will clean up; without one a fresh
    temp directory is made per open and nothing ever removes it.
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Project file not found: {path}")

    if not zipfile.is_zipfile(path):
        raise ValueError(
            f"{os.path.basename(path)} is not a drawing sheet project file."
        )

    state = ProjectState()

    with zipfile.ZipFile(path, "r") as archive:
        try:
            raw = archive.read("project.json")
        except KeyError:
            raise ValueError(
                f"{os.path.basename(path)} is missing its project data."
            ) from None

        data = json.loads(raw.decode("utf-8"))

        version = int(data.get("format_version") or 1)
        if version > FORMAT_VERSION:
            raise ValueError(
                f"{os.path.basename(path)} was saved by a newer version of "
                "the app (format "
                f"{version}, this build reads {FORMAT_VERSION})."
            )

        state.template = _load_template(data.get("template"))
        state.metadata = _load_metadata(data.get("metadata"))

        names = set(archive.namelist())
        for record in data.get("assets") or []:
            asset_id = record.get("id")
            if not asset_id:
                continue
            image = None
            member = f"assets/{asset_id}.png"
            if member in names:
                try:
                    with archive.open(member) as handle:
                        image = Image.open(io.BytesIO(handle.read()))
                        image.load()
                except Exception as exc:  # noqa: BLE001
                    log("warning",
                        f"Could not read the stored image for {asset_id}: {exc}")
            elif record.get("has_image"):
                log("warning",
                    f"The stored image for {asset_id} is missing from the file.")

            # The vector line art, unpacked to somewhere it will still be
            # when the sheet is generated.  The path in the manifest is
            # from the session that saved it and means nothing here.
            vector_path = None
            vector_member = f"assets/{asset_id}.pdf"
            if vector_member in names:
                try:
                    if vector_dir is None:
                        vector_dir = tempfile.mkdtemp(prefix="bny_vectors_")
                    os.makedirs(vector_dir, exist_ok=True)
                    vector_path = os.path.join(vector_dir, f"{asset_id}.pdf")
                    with archive.open(vector_member) as source, \
                            open(vector_path, "wb") as target:
                        shutil.copyfileobj(source, target)
                except Exception as exc:  # noqa: BLE001 - raster still prints
                    log("warning",
                        f"Could not restore the line art for {asset_id}: {exc}")
                    vector_path = None
            elif record.get("vector_source_path"):
                # Saved before line art was stored in the file, or stored
                # and then lost.  Fall back to the original path in case
                # it is still there on this machine.
                original = record["vector_source_path"]
                vector_path = original if os.path.isfile(original) else None

            state.assets[asset_id] = ImportedAsset(
                id=asset_id,
                source_path=record.get("source_path", ""),
                source_type=record.get("source_type", "image"),
                image=image,
                vector_source_path=vector_path,
                dwg_source_path=record.get("dwg_source_path"),
                dwg_component_id=record.get("dwg_component_id"),
            )

    # Placed items come after the assets, so an item whose asset failed to
    # load can be dropped rather than left dangling.
    skipped = 0
    for record in data.get("placed_items") or []:
        item = _load_placed_item(record)
        if item is None or item.source_asset_id not in state.assets:
            skipped += 1
            continue
        state.placed_items.append(item)
    if skipped:
        log("warning",
            f"{skipped} placed item(s) could not be restored and were skipped.")

    for record in data.get("annotations") or []:
        annotation = _load_annotation(record)
        if annotation is not None:
            state.annotations.append(annotation)

    placed_ids = {pi.id for pi in state.placed_items}
    for record in data.get("callouts") or []:
        try:
            if (record.get("crop_source_asset_id") not in state.assets
                    or record.get("placed_item_id") not in placed_ids):
                continue
            state.callouts.append(Callout(
                id=record["id"],
                description=record.get("description", ""),
                crop_source_asset_id=record["crop_source_asset_id"],
                crop_box=_rect4(record.get("crop_box"), (0.0, 0.0, 1.0, 1.0)),
                placed_item_id=record["placed_item_id"],
                leader_style=record.get("leader_style") or "dashed",
            ))
        except (KeyError, TypeError, ValueError):
            continue

    for record in data.get("revision_history") or []:
        state.revision_history.append(RevisionEntry(**{
            k: v for k, v in record.items()
            if k in RevisionEntry.__dataclass_fields__
        }))
    for record in data.get("reference_drawings") or []:
        state.reference_drawings.append(ReferenceDrawing(**{
            k: v for k, v in record.items()
            if k in ReferenceDrawing.__dataclass_fields__
        }))

    # Only keep group memberships whose members actually came back.
    known = placed_ids | {a.id for a in state.annotations}
    for group_id, members in (data.get("groups") or {}).items():
        live = [m for m in members if m in known]
        if len(live) > 1:
            state.groups[group_id] = live
        else:
            # A group of one is not a group; clear the stale marker so it
            # does not drag a lone item around.
            for member_id in live:
                record = state._grouped_record(member_id)
                if record is not None:
                    record.group_id = None

    log("success",
        f"Opened {os.path.basename(path)}: {len(state.assets)} asset(s), "
        f"{len(state.placed_items)} placed, {len(state.annotations)} label(s)")

    return state, dict(data.get("panel_values") or {})


# ---------------------------------------------------------------------------
# Autosave and recovery
# ---------------------------------------------------------------------------

def autosave_path() -> str:
    """Where the rolling recovery copy lives."""
    from app.core.crash_guard import app_data_dir

    return os.path.join(app_data_dir(), "recovery" + PROJECT_EXTENSION)


def autosave_marker_path() -> str:
    """A file that exists only while a session is running.

    Present at startup means the last session did not close cleanly, so
    the recovery copy is worth offering.  Absent means the architect quit
    properly and whatever is in the recovery file is already theirs.
    """
    from app.core.crash_guard import app_data_dir

    return os.path.join(app_data_dir(), "session.lock")


def mark_session_open() -> None:
    try:
        with open(autosave_marker_path(), "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass


def mark_session_closed() -> None:
    try:
        os.unlink(autosave_marker_path())
    except OSError:
        pass


def has_unclean_recovery() -> bool:
    """Whether there is a recovery file from a session that never ended."""
    return (
        os.path.isfile(autosave_marker_path())
        and os.path.isfile(autosave_path())
    )


def clear_autosave() -> None:
    for path in (autosave_path(), autosave_marker_path()):
        try:
            os.unlink(path)
        except OSError:
            pass


def promote_autosave(destination: str) -> str:
    """Copy the recovery file somewhere the architect chose."""
    shutil.copy2(autosave_path(), destination)
    return destination
