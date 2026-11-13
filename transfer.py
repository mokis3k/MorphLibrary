# ─────────────────────────────────────────────────────────────────────────────
# transfer.py - Shape Key Transfer
#
# Public API (used by other addon modules):
#   Transfer               - transfer engine class (used in groups.py)
#   TransferModalMixin     - modal operator integration
#   CLASSES                - tuple of Blender classes to register
#
# Everything else in this file is a private implementation detail.
# ─────────────────────────────────────────────────────────────────────────────

import bpy
import bmesh
from mathutils.bvhtree import BVHTree
import numpy as np
from time import perf_counter


# ══════════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS - validation
# ══════════════════════════════════════════════════════════════════════════════

def _is_valid_mesh_object(obj):
    # Object must exist, be a MESH, and have mesh data.
    return obj is not None and getattr(obj, "type", None) == "MESH" and obj.data is not None


def _is_target_data_editable(obj):
    # Reject linked mesh or shape key datablocks.
    # Transfer requires writable local data.
    mesh = obj.data
    if mesh is None:
        return False
    if getattr(mesh, "library", None) is not None:
        return False
    if mesh.shape_keys is not None and getattr(mesh.shape_keys, "library", None) is not None:
        return False
    return True


def _clamp01(value):
    return max(0.0, min(1.0, value))


# ══════════════════════════════════════════════════════════════════════════════
#  PRIVATE HELPERS - geometry
# ══════════════════════════════════════════════════════════════════════════════

def _read_coords(points):
    coords = np.empty(len(points) * 3, dtype=np.float32)
    points.foreach_get("co", coords)
    return coords.reshape((-1, 3))


def _write_coords(points, coords):
    values = np.ascontiguousarray(coords, dtype=np.float32)

    if values.shape != (len(points), 3):
        raise ValueError(
            "Coordinate count does not match the shape key."
        )

    if not np.isfinite(values).all():
        raise ValueError(
            "Shape key coordinates contain non-finite values."
        )

    points.foreach_set("co", values.ravel())


def _get_basis_coords_local(obj):
    data = obj.data.shape_keys
    points = (
        data.key_blocks[0].data
        if data is not None and len(data.key_blocks)
        else obj.data.vertices
    )
    return _read_coords(points)


def _transform_coords(matrix, coords):
    transform = np.asarray(matrix, dtype=np.float64)
    values = np.asarray(coords, dtype=np.float64)

    return (
        values @ transform[:3, :3].T
        + transform[:3, 3]
    )


def _to_world(matrix_world, coords_local):
    return _transform_coords(matrix_world, coords_local)


def _to_local(matrix_world_inv, coords_world):
    return _transform_coords(matrix_world_inv, coords_world)


_DEGENERATE_EPS = 1e-10


def _is_degenerate_tri(a, b, c):
    # True if the triangle has zero (or near-zero) area.
    return (b - a).cross(c - a).length_squared < _DEGENERATE_EPS


def _build_source_surface(obj, basis_world_coords):
    """
    Build a triangulated copy of the source surface in WORLD space and a
    BVH tree for it. Degenerate (zero-area) triangles are excluded at
    construction time rather than being handled as a per-vertex fallback,
    so a query never lands on a degenerate triangle in the first place.

    bmesh.ops.triangulate does not add new vertices, so triangle vertex
    indices match the indices used in shape key data.

    Returns:
        bvh         - BVHTree for nearest-point queries (None if no usable
                      triangles exist)
        triangles   - list[(i0, i1, i2)] vertex indices per (non-degenerate)
                      triangle
        degenerate_count - number of triangles excluded for being degenerate
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()

        if len(bm.faces) == 0:
            return None, [], 0

        # Apply world-space Basis coordinates, regardless of the object's
        # current active shape key.
        for i, v in enumerate(bm.verts):
            v.co = tuple(basis_world_coords[i])

        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()

        if len(bm.faces) == 0:
            return None, [], 0

        verts_co = [v.co.copy() for v in bm.verts]

        triangles = []
        degenerate_count = 0
        for f in bm.faces:
            idxs = tuple(fv.index for fv in f.verts)
            if len(idxs) != 3:
                continue
            a, b, c = verts_co[idxs[0]], verts_co[idxs[1]], verts_co[idxs[2]]
            if _is_degenerate_tri(a, b, c):
                degenerate_count += 1
                continue
            triangles.append(idxs)

        if not triangles:
            return None, [], degenerate_count

        bvh = BVHTree.FromPolygons(verts_co, triangles)
    finally:
        bm.free()

    return bvh, triangles, degenerate_count


def _build_projection_cache_steps(
    target_basis_world,
    bvh,
    triangles,
    source_basis,
):
    count = len(target_basis_world)

    indices = np.zeros((count, 3), dtype=np.intp)
    barycentric = np.zeros((count, 3), dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.intp)
    failures = 0

    for start in range(0, count, 1024):
        stop = min(start + 1024, count)
        locations = np.zeros(
            (stop - start, 3),
            dtype=np.float64,
        )
        valid = np.zeros(stop - start, dtype=bool)

        for local, index in enumerate(range(start, stop)):
            location, _normal, triangle, _distance = (
                bvh.find_nearest(
                    tuple(target_basis_world[index])
                )
            )

            if location is not None and triangle is not None:
                indices[index] = triangles[triangle]
                locations[local] = location
                valid[local] = True

        rows = np.flatnonzero(valid)

        if len(rows):
            vertices = source_basis[
                indices[start:stop][rows]
            ]
            edge_b = vertices[:, 1] - vertices[:, 0]
            edge_c = vertices[:, 2] - vertices[:, 0]
            offset = locations[rows] - vertices[:, 0]

            normals = np.cross(edge_b, edge_c)
            denominator = np.einsum(
                "ij,ij->i",
                normals,
                normals,
            )
            good = (
                np.isfinite(denominator)
                & (denominator > 0.0)
            )

            rows = rows[good]
            normals = normals[good]
            denominator = denominator[good]

            beta = np.einsum(
                "ij,ij->i",
                np.cross(offset[good], edge_c[good]),
                normals,
            ) / denominator

            gamma = np.einsum(
                "ij,ij->i",
                np.cross(edge_b[good], offset[good]),
                normals,
            ) / denominator

            weights = np.column_stack((
                1.0 - beta - gamma,
                beta,
                gamma,
            ))
            finite = np.isfinite(weights).all(axis=1)

            barycentric[start + rows[finite]] = (
                weights[finite]
            )
            failures += (
                stop - start
            ) - int(finite.sum())
        else:
            failures += stop - start

        yield (
            0.05 + 0.20 * stop / max(count, 1),
            f"Projecting vertices: {stop}/{count}",
        )

    return {
        "indices": indices,
        "barycentric": barycentric,
    }, failures


def _compute_new_sk_coords(
    cache,
    src_basis_world,
    src_sk_world,
    tgt_basis_world,
    weights,
    existing_world,
):
    indices = cache["indices"]
    deformation = src_sk_world - src_basis_world

    delta = np.einsum(
        "ij,ijk->ik",
        cache["barycentric"],
        deformation[indices],
    )
    result = tgt_basis_world + delta

    if weights is not None:
        factor = np.clip(
            np.asarray(weights, dtype=np.float64),
            0.0,
            1.0,
        )
        reference = (
            existing_world
            if existing_world is not None
            else tgt_basis_world
        )
        result = (
            reference
            + factor[:, None] * (result - reference)
        )

    return result, 0


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC TRANSFER ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class TransferCancelled(Exception):
    """Request cancellation at a transfer checkpoint."""


_TRANSFER_JOB = None


def is_transfer_running():
    return _TRANSFER_JOB is not None


class TransferModalMixin:
    def _transfer_begin(self, context, engine, names):
        global _TRANSFER_JOB

        if _TRANSFER_JOB is not None:
            self.report(
                {"WARNING"},
                "Another transfer is already running.",
            )
            return {"CANCELLED"}

        if (
            context.window is None
            or context.screen.is_animation_playing
        ):
            self.report(
                {"WARNING"},
                "Stop animation playback before transferring.",
            )
            return {"CANCELLED"}

        self._job_engine = engine
        self._job_names = list(names)

        if engine is not None:
            self._job_source = engine._source
            self._job_target = engine._target

        # Flush edit data before reading geometry or taking backups.
        if context.mode != "OBJECT":
            if not bpy.ops.object.mode_set.poll():
                self.report({"ERROR"}, "Cannot switch to Object Mode.")
                return {"CANCELLED"}
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError as error:
                self.report({"ERROR"}, f"Cannot switch to Object Mode: {error}")
                return {"CANCELLED"}

        if self._job_source.mode != "OBJECT" or self._job_target.mode != "OBJECT":
            self.report({"ERROR"}, "Both source and target must be in Object Mode.")
            return {"CANCELLED"}

        self._job_scene = context.scene
        self._job_frame = float(
            context.scene.frame_current_final
        )
        self._job_source_mesh = self._job_source.data
        self._job_target_mesh = self._job_target.data
        self._job_source_matrix = (
            self._job_source.matrix_world.copy()
        )
        self._job_target_matrix = (
            self._job_target.matrix_world.copy()
        )
        self._job_source_count = len(
            self._job_source.data.vertices
        )
        self._job_target_count = len(
            self._job_target.data.vertices
        )
        self._job_workspace = context.workspace

        self._job_steps = (
            engine.iter_transfer_shape_keys(self._job_names)
            if engine is not None
            else iter(((0.0, "Preparing preset transfer"),))
        )
        self._job_started = False
        self._job_cancel_pending = False
        self._job_cancel_sent = False
        self._job_timer = None

        try:
            self._job_timer = (
                context.window_manager.event_timer_add(
                    0.01,
                    window=context.window,
                )
            )
            context.window_manager.progress_begin(0, 100)
            context.window_manager.modal_handler_add(self)
            _TRANSFER_JOB = self
        except Exception:
            self._transfer_cleanup(context)
            raise

        return {"RUNNING_MODAL"}

    def _transfer_context_valid(self, context):
        return (
            context.scene == self._job_scene
            and float(context.scene.frame_current_final)
            == self._job_frame
            and self._job_source.data
            == self._job_source_mesh
            and self._job_target.data
            == self._job_target_mesh
            and self._job_source.matrix_world
            == self._job_source_matrix
            and self._job_target.matrix_world
            == self._job_target_matrix
            and len(self._job_source.data.vertices)
            == self._job_source_count
            and len(self._job_target.data.vertices)
            == self._job_target_count
        )

    def _transfer_abort(self):
        steps = getattr(self, "_job_steps", None)

        if (
            steps is None
            or self._job_engine is None
            or not getattr(self, "_job_started", False)
        ):
            return

        try:
            if not self._job_cancel_sent:
                steps.throw(
                    TransferCancelled("Transfer cancelled.")
                )
                self._job_cancel_sent = True

            for _progress in steps:
                pass

        except StopIteration:
            pass
        finally:
            self._job_steps = None

    def _transfer_cleanup(self, context):
        global _TRANSFER_JOB

        timer = getattr(self, "_job_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            self._job_timer = None

        context.window_manager.progress_end()

        workspace = getattr(
            self,
            "_job_workspace",
            None,
        )
        if workspace is not None:
            workspace.status_text_set(None)

        engine = getattr(self, "_job_engine", None)
        if engine is not None:
            engine.free()

        self._job_steps = None

        if _TRANSFER_JOB is self:
            _TRANSFER_JOB = None

    def modal(self, context, event):
        if event.type == "ESC" and event.value == "PRESS":
            if not self._job_cancel_sent:
                self._job_cancel_pending = True

            return {"RUNNING_MODAL"}

        if event.type != "TIMER":
            return {"RUNNING_MODAL"}

        try:
            if not self._transfer_context_valid(context):
                self._job_cancel_pending = (
                    not self._job_cancel_sent
                )

            if self._job_cancel_pending and (
                not self._job_started
                or self._job_engine is None
            ):
                self._transfer_cleanup(context)
                self.report({"INFO"}, "Transfer cancelled.")
                return {"CANCELLED"}

            deadline = perf_counter() + 0.02

            while perf_counter() < deadline:
                if (
                    self._job_cancel_pending
                    and not self._job_cancel_sent
                ):
                    self._job_cancel_pending = False
                    self._job_cancel_sent = True

                    progress, label = self._job_steps.throw(
                        TransferCancelled(
                            "Transfer cancelled."
                        )
                    )
                else:
                    self._job_started = True
                    progress, label = next(self._job_steps)

                percent = min(
                    100.0,
                    max(0.0, progress * 100.0),
                )
                context.window_manager.progress_update(
                    percent
                )
                self._job_workspace.status_text_set(
                    f"Shape Keys Transfer: {label} | "
                    f"{percent:.0f}% | Esc: Cancel"
                )

                for area in context.window.screen.areas:
                    area.tag_redraw()

        except StopIteration as completed:
            engine = self._job_engine
            self._job_steps = None

            if engine is not None and not completed.value:
                message = (
                    engine.last_error
                    or "Nothing transferred."
                )
                cancelled = engine.last_stats.get(
                    "cancelled",
                    False,
                )

                self._transfer_cleanup(context)
                self.report(
                    {"INFO"} if cancelled else {"WARNING"},
                    message,
                )
                return {"CANCELLED"}

            try:
                result = self._finish_transfer(context)

                if (
                    engine is not None
                    and result == {"CANCELLED"}
                ):
                    result = {"FINISHED"}

            except Exception as error:
                print(
                    "MorphDeck: transfer "
                    f"finalization failed: {error}"
                )
                self.report(
                    {"WARNING"},
                    "Transfer finalization failed. "
                    "Use Undo to restore the previous state.",
                )
                result = {"FINISHED"}
            finally:
                self._transfer_cleanup(context)

            return result

        except Exception as error:
            try:
                self._transfer_abort()
            except Exception as rollback_error:
                print(
                    "MorphDeck: rollback failed: "
                    f"{rollback_error}"
                )
            finally:
                self._transfer_cleanup(context)

            print(
                "MorphDeck: transfer failed: "
                f"{error}"
            )
            self.report(
                {"WARNING"},
                "Transfer failed. Use Undo if the "
                "previous state was not restored.",
            )

            # Preserve an undo step after an incomplete rollback.
            return {"FINISHED"}

        return {"RUNNING_MODAL"}

    def cancel(self, context):
        try:
            self._transfer_abort()
        except Exception as error:
            print(
                "MorphDeck: forced cancellation "
                f"failed: {error}"
            )
        finally:
            self._transfer_cleanup(context)

class TransferRollbackError(RuntimeError):
    """Signal that a failed transfer could not be fully rolled back."""


def _set_transfer_slider_range(kb, minimum, maximum):
    # Expand first to avoid Blender adjusting the opposite bound.
    kb.slider_min = min(float(kb.slider_min), minimum)
    kb.slider_max = max(float(kb.slider_max), maximum)
    kb.slider_min = minimum
    kb.slider_max = maximum

class Transfer:
    """
    Transfers shape keys from one Mesh object to another using a spatial
    surface correspondence (BVH nearest point + barycentric coordinates).

    All projection and deformation math runs in WORLD space: source and
    target geometry is transformed by their respective matrix_world before
    matching, and results are converted back to the target's local space
    before being written. This keeps the result correct even when source
    and target have different location / rotation / scale.

    Usage:

        t  = Transfer(source=source_obj, target=target_obj, vertex_group=None)
        ok = t.transfer_shape_keys(shapekey_names=['smile', 'blink'])
        print(t.last_stats)   # diagnostics for the call above
        t.free()              # optional, releases cached BVH / projection data

    The BVH tree and projection cache are built once, on the first call to
    transfer_shape_keys(), and reused on subsequent calls on the same
    instance (note: this means the instance snapshots source/target
    transforms and Basis geometry at that point; recreate the Transfer if
    either object's transform or base mesh changes between calls).

    After each transfer_shape_keys() call, self.last_stats holds counters
    useful for diagnosing "missing" vertices:
        transferred_keys     - shape keys successfully written
        skipped_keys         - shape keys skipped (data mismatch / write failure)
        projection_failed    - target vertices with no nearest source point
        degenerate_triangles - source triangles excluded as zero-area
        barycentric_failed   - vertices where barycentric transform failed
                               (defensive case, normally 0)
        masked_vertices      - target vertices with mask weight == 0

    Projection searches run in chunks. NumPy computes key deformations,
    and foreach_get/foreach_set handle coordinate reads and writes in bulk.
    """

    def __init__(self, source, target, vertex_group=None):
        """
        source        - Blender Object (Mesh) to read shape keys from
        target        - Blender Object (Mesh) to write shape keys to
        vertex_group  - name of a vertex group on target used as a transfer
                        mask (str or None)
        """
        self._source  = source
        self._target  = target
        self._vg_name = vertex_group or ""

        # Heavy resources, built lazily by _prepare_steps().
        self._bvh             = None
        self._triangles        = None
        self._src_mat          = None
        self._tgt_mat          = None
        self._tgt_mat_inv      = None
        self._src_basis_world  = None
        self._tgt_basis_world  = None
        self._cache            = None
        self._weights          = None
        self._base_stats       = None
        self._ready            = False

        self.last_stats = {}

    # ── Private ──────────────────────────────────────────────────────────

    def _prepare_steps(self):
        # Build the BVH tree and projection cache in world space.
        # No-op if already prepared.
        if self._ready:
            return True

        source = self._source
        target = self._target

        if not _is_valid_mesh_object(source):
            return False
        if not _is_valid_mesh_object(target):
            return False

        if not source.data.shape_keys or len(source.data.shape_keys.key_blocks) < 2:
            return False

        if len(source.data.polygons) == 0:
            return False

        if len(target.data.vertices) == 0:
            return False

        if not _is_target_data_editable(target):
            return False

        yield (0.01, "Reading Basis coordinates")

        src_basis_local = _get_basis_coords_local(source)
        tgt_basis_local = _get_basis_coords_local(target)

        # Guard against corrupted/non-standard basis data.
        if len(src_basis_local) != len(source.data.vertices):
            return False
        if len(tgt_basis_local) != len(target.data.vertices):
            return False

        # Snapshot world matrices once; reused for every shape key.
        src_mat     = source.matrix_world.copy()
        tgt_mat     = target.matrix_world.copy()
        tgt_mat_inv = tgt_mat.inverted()

        src_basis_world = _to_world(src_mat, src_basis_local)
        tgt_basis_world = _to_world(tgt_mat, tgt_basis_local)

        yield (0.03, "Building source surface")

        bvh, triangles, degenerate_count = _build_source_surface(source, src_basis_world)
        if bvh is None or not triangles:
            return False

        cache, projection_failed = yield from (
            _build_projection_cache_steps(
                tgt_basis_world,
                bvh,
                triangles,
                src_basis_world,
            )
        )

        weights = None
        masked_vertices = 0
        if self._vg_name:
            vg = target.vertex_groups.get(self._vg_name)
            if vg is not None:
                weights = []
                for idx in range(len(tgt_basis_world)):
                    try:
                        w = vg.weight(idx)
                    except RuntimeError:
                        w = 0.0
                    w = _clamp01(w)
                    weights.append(w)
                    if w == 0.0:
                        masked_vertices += 1

        yield (0.25, "Projection ready")

        self._src_mat         = src_mat
        self._tgt_mat         = tgt_mat
        self._tgt_mat_inv     = tgt_mat_inv
        self._src_basis_world = src_basis_world
        self._tgt_basis_world = tgt_basis_world
        self._bvh             = bvh
        self._triangles        = triangles
        self._cache            = cache
        self._weights           = weights
        self._base_stats = {
            "projection_failed":    projection_failed,
            "degenerate_triangles": degenerate_count,
            "masked_vertices":      masked_vertices,
        }
        self._ready = True
        return True

    # ── Public ───────────────────────────────────────────────────────────

    def transfer_shape_keys(self, shapekey_names=None):
        steps = self.iter_transfer_shape_keys(shapekey_names)

        while True:
            try:
                next(steps)
            except StopIteration as completed:
                return bool(completed.value)

    def iter_transfer_shape_keys(self, shapekey_names=None):
        """Transfer one batch with progress, cancellation and rollback."""
        self.last_error = ""
        self.last_stats = {
            "transferred_keys": 0,
            "skipped_keys": 0,
            "projection_failed": 0,
            "degenerate_triangles": 0,
            "barycentric_failed": 0,
            "masked_vertices": 0,
            "rolled_back": False,
            "rollback_failed": False,
            "cancelled": False,
        }

        source = self._source
        target = self._target
        plans = []
        touched = []

        # Complete validation, backups and calculation before target writes.
        try:
            yield (0.0, "Validating transfer")

            if not _is_valid_mesh_object(source):
                raise ValueError("Source must be a mesh object.")

            if not _is_valid_mesh_object(target):
                raise ValueError("Target must be a mesh object.")

            if source == target or source.data == target.data:
                raise ValueError(
                    "Source and target must use different mesh data."
                )

            if source.mode == "EDIT" or target.mode == "EDIT":
                raise ValueError(
                    "Leave Edit Mode before transferring shape keys."
                )

            if not _is_target_data_editable(target):
                raise ValueError("Target data is linked (read-only).")

            source_data = source.data.shape_keys

            if source_data is None or len(source_data.key_blocks) < 2:
                raise ValueError("Source has no transferable shape keys.")

            source_keys = list(source_data.key_blocks)[1:]

            if shapekey_names is not None:
                requested = set(shapekey_names)
                available = {kb.name for kb in source_keys}

                if requested - available:
                    raise ValueError(
                        "The requested list contains missing keys or Basis."
                    )

                source_keys = [
                    kb for kb in source_keys
                    if kb.name in requested
                ]

            if not source_keys:
                raise ValueError("No shape keys were requested.")

            target_data = target.data.shape_keys

            if target_data is not None and target_data == source_data:
                raise ValueError(
                    "Source and target share the same shape key data."
                )

            original_names = (
                {kb.name for kb in target_data.key_blocks}
                if target_data is not None
                else set()
            )
            original_active = int(target.active_shape_key_index)

            if target_data is not None and len(target_data.key_blocks):
                basis_name = target_data.key_blocks[0].name

                if any(kb.name == basis_name for kb in source_keys):
                    raise ValueError(
                        "A source key name matches the target Basis."
                    )

            elif any(kb.name == "Basis" for kb in source_keys):
                raise ValueError(
                    "A source key name conflicts with the new target Basis."
                )

            if not (yield from self._prepare_steps()):
                raise ValueError("Cannot prepare transfer geometry.")

            self.last_stats.update(self._base_stats)

            if self.last_stats["projection_failed"]:
                raise ValueError(
                    "Some target vertices could not be projected."
                )

            vertex_count = len(self._tgt_basis_world)

            target_keys = (
                {
                    kb.name: kb
                    for kb in target_data.key_blocks
                }
                if target_data is not None
                else {}
            )

            for key_number, src_kb in enumerate(source_keys, 1):
                if len(src_kb.data) != len(self._src_basis_world):
                    raise ValueError(
                        f"Invalid source vertex count: {src_kb.name}"
                    )

                dst_kb = target_keys.get(src_kb.name)
                backup = None
                existing_world = None

                if dst_kb is not None:
                    if len(dst_kb.data) != vertex_count:
                        raise ValueError(
                            f"Invalid target vertex count: {src_kb.name}"
                        )

                    backup = {
                        "key": dst_kb,
                        "coords": _read_coords(dst_kb.data),
                        "minimum": float(dst_kb.slider_min),
                        "maximum": float(dst_kb.slider_max),
                        "value": float(dst_kb.value),
                        "mute": bool(dst_kb.mute),
                    }

                    if self._weights is not None:
                        existing_world = _to_world(
                            self._tgt_mat,
                            backup["coords"],
                        )

                source_world = _to_world(
                    self._src_mat,
                    _read_coords(src_kb.data),
                )

                result_world, failed = _compute_new_sk_coords(
                    self._cache,
                    self._src_basis_world,
                    source_world,
                    self._tgt_basis_world,
                    self._weights,
                    existing_world,
                )

                self.last_stats["barycentric_failed"] += failed

                if failed:
                    raise ValueError(
                        f"Cannot calculate deformation: {src_kb.name}"
                    )

                coords = _to_local(
                    self._tgt_mat_inv,
                    result_world,
                )
                coords = np.ascontiguousarray(
                    coords,
                    dtype=np.float32,
                )

                if (
                        len(coords) != vertex_count
                        or not np.isfinite(coords).all()
                ):
                    raise ValueError(
                        f"Invalid transfer coordinates: {src_kb.name}"
                    )

                plans.append({
                    "name": src_kb.name,
                    "coords": coords,
                    "minimum": float(src_kb.slider_min),
                    "maximum": float(src_kb.slider_max),
                    "backup": backup,
                })

                yield (
                    0.25 + 0.50 * key_number / len(source_keys),
                    f"Calculating keys: {key_number}/{len(source_keys)}",
                )

        except Exception as error:
            self.last_error = str(error)
            self.last_stats["cancelled"] = isinstance(
                error,
                TransferCancelled,
            )
            return False

        try:
            if target.data.shape_keys is None:
                target.shape_key_add(
                    name="Basis",
                    from_mix=False,
                )

            for plan_number, plan in enumerate(plans):
                yield (
                    0.75 + 0.25 * plan_number / len(plans),
                    f"Writing keys: {plan_number + 1}/{len(plans)}",
                )

                backup = plan["backup"]

                if backup is not None:
                    kb = backup["key"]

                    # Include the current key even if its first write fails.
                    touched.append(backup)
                else:
                    kb = target.shape_key_add(
                        name=plan["name"],
                        from_mix=False,
                    )

                    if kb.name != plan["name"]:
                        raise RuntimeError(
                            "Blender changed the target key name."
                        )

                if len(kb.data) != len(plan["coords"]):
                    raise RuntimeError(
                        f"Target vertex count changed: {plan['name']}"
                    )

                _write_coords(
                    kb.data,
                    plan["coords"],
                )

                _set_transfer_slider_range(
                    kb,
                    plan["minimum"],
                    plan["maximum"],
                )

                value = (
                    backup["value"]
                    if backup is not None
                    else 0.0
                )
                mute = (
                    backup["mute"]
                    if backup is not None
                    else False
                )

                if kb.value != value:
                    kb.value = value

                if kb.mute != mute:
                    kb.mute = mute

            target.active_shape_key_index = original_active
            target.data.update()

        except Exception as error:
            self.last_error = str(error)
            self.last_stats["cancelled"] = isinstance(
                error,
                TransferCancelled,
            )
            restore_errors = set()

            def restore(label, action):
                try:
                    action()
                except Exception as restore_error:
                    restore_errors.add(
                        f"{label}: {restore_error}"
                    )

            # Compare names to include partially successful key creation.
            current_data = target.data.shape_keys

            if current_data is not None:
                for kb in reversed(list(current_data.key_blocks)):
                    if kb.name not in original_names:
                        restore(
                            f"Remove {kb.name}",
                            lambda kb=kb: target.shape_key_remove(kb),
                        )

            yield (0.0, "Restoring target state")

            for backup_number, backup in enumerate(
                    reversed(touched),
                    1,
            ):
                kb = backup["key"]

                restore(
                    f"Restore coordinates of {kb.name}",
                    lambda kb=kb, backup=backup: _write_coords(
                        kb.data,
                        backup["coords"],
                    ),
                )

                restore(
                    f"Restore range of {kb.name}",
                    lambda kb=kb, backup=backup: _set_transfer_slider_range(
                        kb,
                        backup["minimum"],
                        backup["maximum"],
                    ),
                )

                for attr in ("value", "mute"):
                    restore(
                        f"Restore {attr} of {kb.name}",
                        lambda kb=kb, attr=attr, backup=backup: setattr(
                            kb,
                            attr,
                            backup[attr],
                        ),
                    )

                yield (
                    backup_number / max(len(touched), 1),
                    f"Restoring keys: {backup_number}/{len(touched)}",
                )

            restore(
                "Restore active key",
                lambda: setattr(
                    target,
                    "active_shape_key_index",
                    original_active,
                ),
            )
            restore(
                "Update target mesh",
                target.data.update,
            )

            self.last_stats["skipped_keys"] = len(plans)
            self.last_stats["rolled_back"] = not restore_errors
            self.last_stats["rollback_failed"] = bool(restore_errors)

            if restore_errors:
                details = "; ".join(sorted(restore_errors))
                self.last_error = (
                    f"Transfer failed: {error}. "
                    f"Rollback incomplete: {details}"
                )

                raise TransferRollbackError(
                    self.last_error
                ) from error

            return False

        self.last_stats["transferred_keys"] = len(plans)
        return True

    def free(self):
        # Release cached BVH / projection data. Safe to call multiple times.
        self._bvh             = None
        self._triangles        = None
        self._cache            = None
        self._weights           = None
        self._src_mat           = None
        self._tgt_mat           = None
        self._tgt_mat_inv       = None
        self._src_basis_world   = None
        self._tgt_basis_world   = None
        self._base_stats        = None
        self._ready             = False


# No Blender classes are registered by this module.
CLASSES = ()
