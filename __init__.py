bl_info = {
    "name": "MorphDeck",
    "author": "mokis3k",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > MorphDeck",
    "description": "Shape keys grouping, selection tools, presets, and shape key transfer.",
    "category": "Object",
}

import os
import bpy.utils.previews
import bpy
from bpy.app.handlers import persistent
from bpy.types import Operator, Panel, PropertyGroup
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    PointerProperty,
    CollectionProperty,
    EnumProperty,
)

from .common import (
    get_shape_key_data,
    has_group_storage,
    is_initialized,
    get_selected_group_name,
    count_keys_in_group,
    count_selected_in_group,
    tag_redraw_view3d,
    INIT_GROUP_NAME,
    kd_selected_set,
    kd_set_group,
    skv_shape_key_list_sync_active,
    skv_sync_shape_key_list_indices,
    kd_favorite_names,
    kd_prune_favorites,
    skv_list_key_names,
    kd_keyframed_names,
    _is_basis_name,
)
from . import groups
from . import presets
from . import transfer

_SKV_CUSTOM_ICONS = None


def _unregister_custom_icons():
    global _SKV_CUSTOM_ICONS

    if _SKV_CUSTOM_ICONS is not None:
        bpy.utils.previews.remove(_SKV_CUSTOM_ICONS)
        _SKV_CUSTOM_ICONS = None


def _register_custom_icons():
    global _SKV_CUSTOM_ICONS

    _unregister_custom_icons()

    path = os.path.join(
        os.path.dirname(__file__),
        "icons",
        "icon.png",
    )
    if not os.path.isfile(path):
        return

    collection = bpy.utils.previews.new()

    try:
        collection.load("object", path, "IMAGE")

    except (OSError, RuntimeError, ValueError) as error:
        bpy.utils.previews.remove(collection)
        print(
            f"MorphDeck: cannot load object icon: {error}"
        )
        return

    _SKV_CUSTOM_ICONS = collection

def _poll_mesh_object(self, obj):
    # Accept only mesh objects.
    return bool(obj) and getattr(obj, "type", None) == "MESH"


def _poll_transfer_target(scene, obj):
    # Accept only mesh objects and exclude the current source mesh (stored on Scene).
    if not obj or getattr(obj, "type", None) != "MESH":
        return False
    src_name = getattr(scene, "skv_transfer_source_name", "")
    return (not src_name) or (obj.name != src_name)


def _poll_sync_values_source(scene, obj):
    # Accept only mesh objects and exclude the current target mesh.
    if not obj or getattr(obj, "type", None) != "MESH":
        return False
    target_name = getattr(scene, "skv_sync_values_target_name", "")
    return (not target_name) or (obj.name != target_name)


# -----------------------------
# Automatic object sync + auto-scan
# -----------------------------

_SKV_SYNC_GUARD = False
_SKV_STRUCTURE_TOKEN = None
_SKV_PRESET_SYNC_TOKEN = None


def _skv_pointer(value):
    return value.as_pointer() if value is not None else 0


def _skv_updated_ids(depsgraph):
    pointers = set()

    for update in depsgraph.updates:
        datablock = update.id
        if datablock is not None:
            original = getattr(
                datablock,
                "original",
                datablock,
            )
            pointers.add(_skv_pointer(original))

    return pointers


@persistent
def _skv_reset_runtime_caches(*_args):
    global _SKV_STRUCTURE_TOKEN, _SKV_PRESET_SYNC_TOKEN

    _SKV_STRUCTURE_TOKEN = None
    _SKV_PRESET_SYNC_TOKEN = None

    try:
        presets.migrate_legacy_preset_object_refs()
    except Exception:
        pass


def _shape_key_signature(obj) -> str:
    if not obj or getattr(obj, "type", None) != "MESH":
        return ""

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return "NO_SHAPE_KEYS"

    try:
        names = [kb.name for kb in key_data.key_blocks]
    except Exception:
        return "SHAPE_KEYS_UNKNOWN"

    return "|".join(names)

def _shape_key_names_from_signature(signature: str):
    if not signature:
        return []
    if signature in {"NO_SHAPE_KEYS", "SHAPE_KEYS_UNKNOWN"}:
        return []
    return signature.split("|")


def _rename_collection_entry_names(collection, old_name: str, new_name: str) -> None:
    if not collection or not old_name or not new_name or old_name == new_name:
        return

    for it in collection:
        try:
            if getattr(it, "name", "") == old_name:
                it.name = new_name
        except Exception:
            pass

def _sync_shape_key_rename_mappings(scene, obj, key_data, previous_signature: str, current_signature: str) -> None:
    # Keep addon name-based metadata consistent after direct KeyBlock.name edits.
    old_names = _shape_key_names_from_signature(previous_signature)
    new_names = _shape_key_names_from_signature(current_signature)

    if not old_names or not new_names:
        return
    if len(old_names) != len(new_names):
        return
    if old_names == new_names:
        return

    old_set = set(old_names)
    new_set = set(new_names)

    for old_name, new_name in zip(old_names, new_names):
        if old_name == new_name:
            continue

        # Treat only pure rename at the same index.
        # If both names still exist, this is likely a reorder or ambiguous change.
        if old_name in new_set or new_name in old_set:
            continue

        try:
            if hasattr(key_data, "skv_key_groups"):
                _rename_collection_entry_names(key_data.skv_key_groups, old_name, new_name)
        except Exception:
            pass

        try:
            if hasattr(key_data, "skv_favorites"):
                _rename_collection_entry_names(
                    key_data.skv_favorites,
                    old_name,
                    new_name,
                )
        except Exception:
            pass

        try:
            if hasattr(key_data, "skv_selected"):
                _rename_collection_entry_names(key_data.skv_selected, old_name, new_name)
        except Exception:
            pass

        try:
            if hasattr(key_data, "skv_auto_keyframes"):
                _rename_collection_entry_names(key_data.skv_auto_keyframes, old_name, new_name)
        except Exception:
            pass

        try:
            if scene and hasattr(scene, "skv_global_presets") and obj:
                for preset in scene.skv_global_presets:
                    for it in preset.items:
                        item_obj = getattr(it, "object_ref", None)
                        if (
                                item_obj == obj
                                or (item_obj is None and it.object_name == obj.name)
                        ) and it.key_name == old_name:
                            it.key_name = new_name
        except Exception:
            pass

def _sync_shape_key_sculpt_state(context):
    scene = getattr(context, "scene", None)
    props = getattr(scene, "skv_props", None) if scene else None

    if not props or not props.shape_key_sculpting:
        return

    obj = props.object_pick
    data = get_shape_key_data(obj)
    key_name = props.sculpt_shape_key_name

    kb = (
        data.key_blocks.get(key_name)
        if data and data.key_blocks
        else None
    )

    editing = (
        obj is not None
        and context.active_object == obj
        and obj.mode == "SCULPT"
        and kb is not None
        and getattr(obj.active_shape_key, "name", "") == key_name
    )

    if not editing:
        props.shape_key_sculpting = False
        props.sculpt_shape_key_name = ""
        tag_redraw_view3d(context)

def _auto_process_active_object(scene, updated_ids=None):
    global _SKV_SYNC_GUARD, _SKV_STRUCTURE_TOKEN
    if _SKV_SYNC_GUARD:
        return

    props = getattr(scene, "skv_props", None)
    if not props:
        return

    ctx = bpy.context
    active = getattr(ctx, "active_object", None)

    desired = active if (active and getattr(active, "type", None) == "MESH") else None
    desired_name = desired.name if desired else ""
    data = get_shape_key_data(desired)
    mesh = getattr(desired, "data", None)

    token = (
        _skv_pointer(scene),
        _skv_pointer(desired),
        _skv_pointer(mesh),
        _skv_pointer(data),
        len(data.key_blocks) if data is not None else 0,
        desired_name,
    )

    if (
            updated_ids is not None
            and token == _SKV_STRUCTURE_TOKEN
            and not updated_ids.intersection(token[1:4])
    ):
        return

    desired_signature = _shape_key_signature(desired)
    _SKV_STRUCTURE_TOKEN = token

    if (
        getattr(props, "last_active_object_name", "") == desired_name
        and getattr(props, "last_shape_key_signature", "") == desired_signature
    ):
        return

    _SKV_SYNC_GUARD = True
    try:
        previous_name = getattr(props, "last_active_object_name", "")
        previous_signature = getattr(props, "last_shape_key_signature", "")
        object_changed = previous_name != desired_name

        if desired and previous_name == desired_name:
            rename_key_data = get_shape_key_data(desired)
            if rename_key_data and getattr(rename_key_data, "key_blocks", None):
                _sync_shape_key_rename_mappings(
                    scene,
                    desired,
                    rename_key_data,
                    previous_signature,
                    desired_signature,
                )

        props.last_active_object_name = desired_name
        props.last_shape_key_signature = desired_signature
        props.object_pick = desired

        props.scan_status = ""

        if not desired:
            tag_redraw_view3d(ctx)
            return

        key_data = get_shape_key_data(desired)
        if not key_data or not getattr(key_data, "key_blocks", None):
            props.scan_status = "No Shape Keys found."
            tag_redraw_view3d(ctx)
            return

        if not has_group_storage(key_data):
            tag_redraw_view3d(ctx)
            return

        if getattr(key_data, "library", None) is not None:
            tag_redraw_view3d(ctx)
            return

        if not is_initialized(key_data):
            tag_redraw_view3d(ctx)
            return

        kd_prune_favorites(key_data)

        if object_changed:
            basis_name = ""
            try:
                if getattr(key_data, "key_blocks", None):
                    basis_name = key_data.key_blocks[0].name
            except Exception:
                basis_name = ""

            if basis_name:
                skv_sync_shape_key_list_indices(
                    ctx,
                    desired,
                    basis_name,
                    set_blender_active=True,
                )
            else:
                skv_sync_shape_key_list_indices(ctx, desired, set_blender_active=False)
        else:
            skv_sync_shape_key_list_indices(ctx, desired, set_blender_active=False)

        tag_redraw_view3d(ctx)
    finally:
        _SKV_SYNC_GUARD = False


_SKV_DEPSGRAPH_GUARD = False

@persistent
def _depsgraph_update_post(scene, depsgraph):
    global _SKV_DEPSGRAPH_GUARD, _SKV_PRESET_SYNC_TOKEN

    if transfer.is_transfer_running():
        return

    if _SKV_DEPSGRAPH_GUARD:
        return

    _SKV_DEPSGRAPH_GUARD = True
    try:
        updated_ids = _skv_updated_ids(depsgraph)

        _sync_shape_key_sculpt_state(bpy.context)
        _auto_process_active_object(scene, updated_ids)

        frame = int(scene.frame_current)
        candidates = {}

        def add_candidate(obj):
            if not obj or getattr(obj, "type", None) != "MESH":
                return

            data = get_shape_key_data(obj)
            if not data:
                return

            pointer = _skv_pointer(data)
            if pointer not in candidates:
                candidates[pointer] = {
                    "data": data,
                    "owner_ids": set(),
                }

            candidates[pointer]["owner_ids"].update({
                _skv_pointer(obj),
                _skv_pointer(obj.data),
                pointer,
            })

        for obj in scene.objects:
            add_candidate(obj)

        # Include preset objects even when they are not linked to this scene.
        for preset in getattr(scene, "skv_global_presets", ()):
            for item in preset.items:
                add_candidate(presets.preset_item_object(item))

        for record in candidates.values():
            data = record["data"]
            if data.library is not None:
                continue

            enabled = [
                item
                for item in getattr(data, "skv_auto_keyframes", ())
                if item.enabled
            ]
            if not enabled:
                continue

            frame_changed = any(
                frame != item.last_frame
                for item in enabled
            )
            data_changed = bool(
                updated_ids.intersection(record["owner_ids"])
            )
            if not frame_changed and not data_changed:
                continue

            keys = {kb.name: kb for kb in data.key_blocks}

            for item in enabled:
                kb = keys.get((item.name or "").strip())
                if kb is None:
                    continue

                value = float(kb.value)

                if frame != item.last_frame:
                    item.last_frame = frame
                    item.last_value = value
                    continue

                if abs(value - item.last_value) > 1e-6:
                    try:
                        data.keyframe_insert(
                            data_path=groups._shape_key_value_data_path(
                                kb.name
                            ),
                            frame=frame,
                        )
                    except RuntimeError:
                        pass

                    item.last_value = value

        preset = presets.get_active_global_preset(scene)

        if preset is not None:
            token = (
                _skv_pointer(scene),
                _skv_pointer(preset),
                len(preset.items),
                float(scene.frame_current_final),
            )
            full_sync = (
                token != _SKV_PRESET_SYNC_TOKEN
                or _skv_pointer(scene) in updated_ids
            )

            presets.sync_preset_item_values(
                bpy.context,
                preset,
                updated_ids=(
                    None if full_sync else updated_ids
                ),
            )
            _SKV_PRESET_SYNC_TOKEN = token
        else:
            _SKV_PRESET_SYNC_TOKEN = None
    finally:
        _SKV_DEPSGRAPH_GUARD = False


def _ensure_handler_installed():
    _skv_reset_runtime_caches()

    handlers = bpy.app.handlers
    bindings = (
        (
            handlers.depsgraph_update_post,
            _depsgraph_update_post,
        ),
        (
            handlers.load_post,
            _skv_reset_runtime_caches,
        ),
        (
            handlers.undo_post,
            _skv_reset_runtime_caches,
        ),
        (
            handlers.redo_post,
            _skv_reset_runtime_caches,
        ),
    )

    for collection, callback in bindings:
        if callback not in collection:
            collection.append(callback)


def _ensure_handler_removed():
    handlers = bpy.app.handlers
    bindings = (
        (
            handlers.depsgraph_update_post,
            _depsgraph_update_post,
        ),
        (
            handlers.load_post,
            _skv_reset_runtime_caches,
        ),
        (
            handlers.undo_post,
            _skv_reset_runtime_caches,
        ),
        (
            handlers.redo_post,
            _skv_reset_runtime_caches,
        ),
    )

    for collection, callback in bindings:
        if callback in collection:
            collection.remove(callback)

    _skv_reset_runtime_caches()


# -----------------------------
# UI Lists
# -----------------------------


# -----------------------------
# Operators
# -----------------------------

class SKV_OT_InitializeObject(Operator):
    bl_idname = "skv.initialize_object"
    bl_label = "Initialize Morph Library"
    bl_description = "Create addon groups for this object's shape keys"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "active_object", None)
        if obj is None or obj.type != "MESH":
            return False

        data = get_shape_key_data(obj)

        return bool(
            data
            and data.key_blocks
            and has_group_storage(data)
            and not is_initialized(data)
            and obj.library is None
            and obj.data.library is None
            and data.library is None
        )

    def execute(self, context):
        from .common import ensure_init_setup_write

        if not self.poll(context):
            return {"CANCELLED"}

        obj = context.active_object

        try:
            ensure_init_setup_write(obj)

            if not is_initialized(get_shape_key_data(obj)):
                raise RuntimeError(
                    "Addon storage was not initialized."
                )

            context.scene.skv_props.object_pick = obj

            skv_sync_shape_key_list_indices(
                context,
                obj,
                set_blender_active=False,
            )

        except Exception as error:
            print(
                "MorphDeck: initialization failed: "
                f"{error}"
            )
            self.report(
                {"WARNING"},
                "Initialization could not finish. "
                "Use Undo to restore the previous state.",
            )

            # Keep an undo step if initialization changed some data.
            tag_redraw_view3d(context)
            return {"FINISHED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}

class SKV_OT_SearchClear(Operator):
    bl_idname = "skv.search_clear"
    bl_label = "Clear Search"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if hasattr(context.scene, "skv_props"):
            context.scene.skv_props.search = ""
            context.scene.skv_props.keys_index = -1
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_SynchronizeValues(Operator):
    bl_idname = "skv.synchronize_values"
    bl_label = "Synchronize values"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, "skv_props", None) if hasattr(context, "scene") else None
        obj = getattr(props, "object_pick", None) if props else None
        return bool(obj and getattr(obj, "type", None) == "MESH")

    def invoke(self, context, event):
        props = context.scene.skv_props
        target = getattr(props, "object_pick", None)

        if not target or getattr(target, "type", None) != "MESH":
            return {"CANCELLED"}

        context.scene.skv_sync_values_target_name = target.name

        source = getattr(context.scene, "skv_sync_values_source", None)
        if not source or getattr(source, "type", None) != "MESH" or source.name == target.name:
            context.scene.skv_sync_values_source = None

        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        layout = self.layout
        layout.prop(context.scene, "skv_sync_values_source", text="Source")

    def execute(self, context):

        target_name = getattr(context.scene, "skv_sync_values_target_name", "")
        target = bpy.data.objects.get(target_name) if target_name else None

        if not target or getattr(target, "type", None) != "MESH":
            props = getattr(context.scene, "skv_props", None)
            target = getattr(props, "object_pick", None) if props else None

        if not target or getattr(target, "type", None) != "MESH":
            self.report({"ERROR"}, "Target mesh is not set")
            return {"CANCELLED"}

        source = getattr(context.scene, "skv_sync_values_source", None)
        if not source or getattr(source, "type", None) != "MESH":
            self.report({"ERROR"}, "Source mesh is not set")
            return {"CANCELLED"}

        if source.name == target.name:
            self.report({"ERROR"}, "Source must be different from target")
            return {"CANCELLED"}

        source_key_data = get_shape_key_data(source)
        target_key_data = get_shape_key_data(target)

        if not source_key_data or not getattr(source_key_data, "key_blocks", None):
            self.report({"ERROR"}, "Source has no Shape Keys")
            return {"CANCELLED"}

        if not target_key_data or not getattr(target_key_data, "key_blocks", None):
            self.report({"ERROR"}, "Target has no Shape Keys")
            return {"CANCELLED"}

        if getattr(target_key_data, "library", None) is not None:
            self.report({"ERROR"}, "Target shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        changed = 0

        for src_kb in source_key_data.key_blocks:
            if _is_basis_name(source_key_data, src_kb.name):
                continue

            dst_kb = target_key_data.key_blocks.get(src_kb.name)
            if not dst_kb:
                continue

            try:
                dst_kb.value = float(src_kb.value)
                changed += 1
            except Exception:
                continue

        if changed == 0:
            self.report({"INFO"}, "No matching Shape Keys found")
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        self.report({"INFO"}, f"Synchronized values: {changed}")
        return {"FINISHED"}


class SKV_OT_ShapeKeyAdd(Operator):
    bl_idname = "skv.shape_key_add"
    bl_label = "Add Shape Key"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        props = getattr(scene, "skv_props", None) if scene else None
        obj = getattr(props, "object_pick", None) if props else None

        return bool(obj and obj.type == "MESH")

    def execute(self, context):
        from .common import ensure_init_setup_write

        props = context.scene.skv_props
        obj = props.object_pick

        if not obj or obj.type != "MESH":
            return {"CANCELLED"}

        data = get_shape_key_data(obj)

        if obj.data.library is not None or (
            data is not None and data.library is not None
        ):
            self.report(
                {"ERROR"},
                "Target data is linked (read-only).",
            )
            return {"CANCELLED"}

        if data is not None:
            ensure_init_setup_write(obj)

        # Keep keys created by this operator in a dedicated object-local group.
        target_group = "Created"

        try:
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")

            obj.select_set(True)
            context.view_layer.objects.active = obj

        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        props.shape_key_sculpting = False
        props.sculpt_shape_key_name = ""

        try:
            if get_shape_key_data(obj) is None:
                obj.shape_key_add(
                    name="Basis",
                    from_mix=False,
                )

            ensure_init_setup_write(obj)

            # Blender generates a unique name automatically.
            new_kb = obj.shape_key_add(
                name="Key",
                from_mix=False,
            )

            data = get_shape_key_data(obj)

            if not groups._ensure_group_exists(data, target_group):
                obj.shape_key_remove(new_kb)
                raise RuntimeError(
                    "Cannot create the Created shape key group."
                )

            kd_set_group(data, new_kb.name, target_group)

            self.report(
                {"INFO"},
                f'Shape key "{new_kb.name}" added to group "{target_group}".',
            )

        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        # Reveal the new key and activate its Created group.
        props.keys_open = True
        props.search = ""

        skv_sync_shape_key_list_indices(
            context,
            obj,
            new_kb.name,
            set_blender_active=True,
        )

        new_kb.value = 1.0

        try:
            bpy.ops.object.mode_set(mode="SCULPT")

        except RuntimeError:
            self.report(
                {"WARNING"},
                "Shape key created, but failed to switch to Sculpt Mode.",
            )
            tag_redraw_view3d(context)
            return {"FINISHED"}

        props.shape_key_sculpting = True
        props.sculpt_shape_key_name = new_kb.name

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_FinishShapeKeySculpt(Operator):
    bl_idname = "skv.finish_shape_key_sculpt"
    bl_label = "Finish Sculpting"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        props = getattr(scene, "skv_props", None) if scene else None
        obj = getattr(props, "object_pick", None) if props else None

        return bool(
            props
            and props.shape_key_sculpting
            and obj is not None
            and context.active_object == obj
            and obj.mode == "SCULPT"
            and getattr(obj.active_shape_key, "name", "")
            == props.sculpt_shape_key_name
        )

    def execute(self, context):
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        if obj and getattr(obj, "type", None) == "MESH":
            try:
                context.view_layer.objects.active = obj
            except Exception:
                pass

        try:
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass

        props.shape_key_sculpting = False
        props.sculpt_shape_key_name = ""

        tag_redraw_view3d(context)
        return {"FINISHED"}


# -----------------------------
# Scene Props (UI state)
# -----------------------------
def keys_index_update(self, context):
    # Sync Shape Keys in group list selection to all shape key lists.
    if skv_shape_key_list_sync_active():
        return

    obj = getattr(self, "object_pick", None)
    key_data = get_shape_key_data(obj) if obj else None
    if not key_data or not getattr(key_data, "key_blocks", None):
        return

    idx = int(getattr(self, "keys_index", -1))
    if 0 <= idx < len(key_data.key_blocks):
        skv_sync_shape_key_list_indices(
            context,
            obj,
            key_data.key_blocks[idx].name,
            set_blender_active=True,
        )


def favorites_index_update(self, context):
    if skv_shape_key_list_sync_active():
        return

    obj = self.object_pick
    data = get_shape_key_data(obj)
    if not data or not data.key_blocks:
        return

    index = self.favorites_index
    if 0 <= index < len(data.key_blocks):
        name = data.key_blocks[index].name

        if name in kd_favorite_names(data):
            skv_sync_shape_key_list_indices(
                context,
                obj,
                name,
                set_blender_active=True,
            )

def keyframed_index_update(self, context):
    if skv_shape_key_list_sync_active():
        return

    obj = self.object_pick
    data = get_shape_key_data(obj)
    if not data or not data.key_blocks:
        return

    index = self.keyframed_index

    if 0 <= index < len(data.key_blocks):
        name = data.key_blocks[index].name

        if name in kd_keyframed_names(data):
            skv_sync_shape_key_list_indices(
                context,
                obj,
                name,
                set_blender_active=True,
            )

class SKV_Props(PropertyGroup):
    keys_index: IntProperty(
        name="Keys Index",
        default=-1,
        min=-1,
        update=keys_index_update,
    )
    search: StringProperty(name="Search", default="")


    favorites_search: StringProperty(
        name="Favorites Search",
        default="",
    )
    favorites_index: IntProperty(
        name="Favorites Index",
        default=-1,
        min=-1,
        update=favorites_index_update,
    )

    keyframed_search: StringProperty(
        name="Keyframed Search",
        default="",
    )

    keyframed_index: IntProperty(
        name="Keyframed Index",
        default=-1,
        min=-1,
        update=keyframed_index_update,
    )

    groups_open: BoolProperty(name="Groups", default=True)
    keys_open: BoolProperty(name="Keys", default=True)

    object_pick: PointerProperty(
        name="Object",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
    )

    last_active_object_name: StringProperty(
        name="Last Active Object Name",
        default="",
        options={"SKIP_SAVE"},
    )
    last_shape_key_signature: StringProperty(
        name="Last Shape Key Signature",
        default="",
        options={"SKIP_SAVE"},
    )

    scan_status: StringProperty(name="Scan Status", default="", options={"SKIP_SAVE"})

    presets_open: BoolProperty(name="Presets", default=True)

    shape_key_sculpting: BoolProperty(name="Shape Key Sculpting", default=False)
    sculpt_shape_key_name: StringProperty(name="Sculpt Shape Key Name", default="")

    transfer_inheritance: BoolProperty(
        name="Inherit presets",
        default=False,
        description="Add transferred shape keys to the same presets for the target object",
    )
    transfer_keyframes_inheritance: BoolProperty(
        name="Inherit keyframes",
        default=False,
        description="Copy keyframes from transferred source shape keys to matching target shape keys",
    )

def _draw_shape_key_sculpt_actions(layout, context, obj, key_data=None):
    props = context.scene.skv_props

    layout.separator()

    editing = (
        props.shape_key_sculpting
        and obj is not None
        and context.active_object == obj
        and obj.mode == "SCULPT"
        and getattr(obj.active_shape_key, "name", "")
        == props.sculpt_shape_key_name
    )

    if editing:
        layout.operator(
            "skv.finish_shape_key_sculpt",
            text="Finish Sculpting",
            icon="CHECKMARK",
        )
    else:
        layout.operator(
            "skv.shape_key_add",
            text="Add Shape Key",
            icon="ADD",
        )


class SKV_PT_ShapeKeysPanel(Panel):
    bl_label = "Morph Library"
    bl_idname = "SKV_PT_morph_library_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MorphDeck"
    bl_order = 0

    def draw_header(self, context):
        icon = (
            _SKV_CUSTOM_ICONS.get("object")
            if _SKV_CUSTOM_ICONS is not None
            else None
        )

        if icon is not None and icon.icon_id:
            self.layout.label(
                text="",
                icon_value=icon.icon_id,
            )
        else:
            self.layout.label(
                text="",
                icon="OBJECT_DATA",
            )

    def draw(self, context):
        layout = self.layout.column(align=True)
        props = context.scene.skv_props
        obj = getattr(props, "object_pick", None)

        row = layout.row(align=True)
        row.label(
            text=obj.name if obj else "No selected object",
            icon="MESH_DATA",
        )

        if not obj:
            return

        layout.separator()

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            layout.label(text="No shape keys found", icon="INFO")

            row = layout.row(align=True)
            row.operator("skv.transfer_from", text="Transfer from", icon="IMPORT")

            _draw_shape_key_sculpt_actions(layout, context, obj, key_data)
            return
        if not has_group_storage(key_data):
            return

        if not is_initialized(key_data):
            if (
                    obj.library is not None
                    or obj.data.library is not None
                    or key_data.library is not None
            ):
                layout.label(
                    text="Linked data is read-only",
                    icon="INFO",
                )
            else:
                layout.label(
                    text="Setup is required for this object",
                    icon="INFO",
                )
                layout.operator(
                    "skv.initialize_object",
                    icon="ADD",
                )

            return

        top_actions = layout.row(align=True)
        top_actions.operator("skv.transfer_from", text="Transfer from", icon="IMPORT")
        top_actions.operator("skv.synchronize_values", text="Synchronize values", icon="FILE_REFRESH")

        current_group = get_selected_group_name(key_data) or INIT_GROUP_NAME

        selected_names = set(kd_selected_set(key_data))
        has_selected_valid = any(
            n and not _is_basis_name(key_data, n)
            for n in selected_names
        )
        has_presets = hasattr(context.scene, "skv_global_presets") and (len(context.scene.skv_global_presets) > 0)
        groups_count = len(key_data.skv_groups) if getattr(key_data, "skv_groups", None) else 0
        can_move_to_group = groups_count > 1

        groups_col = layout.column(align=True)
        hg = groups_col.row(align=True)
        ig = "TRIA_DOWN" if props.groups_open else "TRIA_RIGHT"
        hg.prop(props, "groups_open", text="", emboss=False, icon=ig)
        hg.label(text="Groups" if props.groups_open else f"Group: {current_group}")

        if props.groups_open:
            rowg = groups_col.row()
            rowg.template_list(
                "SKV_UL_groups",
                "",
                key_data,
                "skv_groups",
                key_data,
                "skv_group_index",
                rows=max(3, min(groups_count, 5)),
            )
            col = rowg.column(align=True)
            col.operator("skv.group_add", icon="ADD", text="")
            col.operator("skv.group_remove", icon="REMOVE", text="")

            up = col.operator("skv.group_move", icon="TRIA_UP", text="")
            up.direction = "UP"

            down = col.operator("skv.group_move", icon="TRIA_DOWN", text="")
            down.direction = "DOWN"

        keys_col = layout.column(align=True)
        hk = keys_col.row(align=True)
        ik = "TRIA_DOWN" if props.keys_open else "TRIA_RIGHT"
        hk.prop(props, "keys_open", text="", emboss=False, icon=ik)
        hk.label(text=f'Shape Keys in {current_group}')

        if props.keys_open:
            group_count = count_keys_in_group(key_data, current_group)

            if group_count > 0:
                row = keys_col.row(align=True)
                row.prop(props, "search", text="", icon="VIEWZOOM")
                row.operator("skv.search_clear", text="", icon="X")

                row = keys_col.row(align=True)

                row.operator(
                    "skv.select_visible",
                    text="Select All",
                ).mode = "ALL"

                row.operator(
                    "skv.select_visible",
                    text="Deselect",
                ).mode = "CLEAR"

                row.operator(
                    "skv.select_visible",
                    text="Invert",
                ).mode = "INVERT"

                row.operator(
                    "skv.select_visible",
                    text="Non-Zero",
                ).mode = "NONZERO"

            key_rows = max(1, min(group_count, 5))
            keys_col.template_list(
                "SKV_UL_key_blocks",
                "",
                key_data,
                "key_blocks",
                props,
                "keys_index",
                rows=key_rows,
            )


            selected_count = count_selected_in_group(key_data, current_group, getattr(props, "search", ""))

            if selected_count > 0 and group_count > 0:

                r1 = keys_col.row(align=True)
                r1m = r1.row(align=True)
                r1m.enabled = can_move_to_group
                r1m.menu("SKV_MT_move_to_group", text="Move to group", icon="FILE_FOLDER")
                r1.operator("skv.create_group_from_selected", text="Create group", icon="NEWFOLDER")

                r2 = keys_col.row(align=True)
                r2m = r2.row(align=True)
                r2m.enabled = has_presets
                r2m.menu("SKV_MT_add_to_preset", text="Add to preset", icon="PRESET")
                r2.operator("skv.global_preset_add_from_selected", text="Create preset", icon="PRESET")

                r3 = keys_col.row(align=True)
                r3.operator("skv.reset_group_values", text="Zero selected values", icon="RECOVER_LAST")
                r3.operator("skv.delete_selected_shape_keys", text="Delete selected", icon="TRASH")

                r4 = keys_col.row(align=True)
                r4.operator("skv.transfer_to", text="Transfer to...", icon="EXPORT")

        _draw_shape_key_sculpt_actions(layout, context, obj, key_data)


class SKV_PT_FavoritesPanel(Panel):
    bl_label = "Favorites"
    bl_idname = "SKV_PT_favorites_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MorphDeck"
    bl_order = 20

    def draw_header(self, context):
        self.layout.label(text="", icon="SOLO_ON")

    def draw(self, context):
        layout = self.layout.column(align=True)
        props = context.scene.skv_props
        obj = props.object_pick
        data = get_shape_key_data(obj)

        if not obj:
            layout.label(text="No selected object", icon="INFO")
            return

        if not data or not data.key_blocks or not is_initialized(data):
            layout.label(text="No shape keys found", icon="INFO")
            return

        count = len(kd_favorite_names(data))

        if count == 0:
            layout.label(text="No favorites found", icon="INFO")
            return

        layout.enabled = data.library is None

        row = layout.row(align=True)
        row.prop(
            props,
            "favorites_search",
            text="",
            icon="VIEWZOOM",
        )
        row.operator(
            "skv.favorites_search_clear",
            text="",
            icon="X",
        )

        row = layout.row(align=True)
        row.operator(
            "skv.favorites_select_visible",
            text="Select All",
        ).mode = "ALL"

        row.operator(
            "skv.favorites_select_visible",
            text="Deselect",
        ).mode = "CLEAR"

        row.operator(
            "skv.favorites_select_visible",
            text="Invert",
        ).mode = "INVERT"

        layout.operator(
            "skv.favorites_clear",
            text="Clear",
        )

        layout.template_list(
            "SKV_UL_favorites",
            "",
            data,
            "key_blocks",
            props,
            "favorites_index",
            rows=max(1, min(count, 5)),
        )

        visible = set(
            skv_list_key_names(data, props, "FAVORITES")
        )

        if (
            data.skv_selection_source != "FAVORITES"
            or not (visible & kd_selected_set(data))
        ):
            return

        row = layout.row(align=True)
        row.menu(
            "SKV_MT_move_to_group",
            text="Move to group",
            icon="FILE_FOLDER",
        )
        row.operator(
            "skv.create_group_from_selected",
            text="Create group",
            icon="NEWFOLDER",
        )

        row = layout.row(align=True)

        presets_row = row.row(align=True)
        presets_row.enabled = bool(context.scene.skv_global_presets)
        presets_row.menu(
            "SKV_MT_add_to_preset",
            text="Add to preset",
            icon="PRESET",
        )

        row.operator(
            "skv.global_preset_add_from_selected",
            text="Create preset",
            icon="PRESET",
        )

        row = layout.row(align=True)
        row.operator(
            "skv.reset_group_values",
            text="Zero selected values",
            icon="RECOVER_LAST",
        )
        row.operator(
            "skv.delete_selected_shape_keys",
            text="Delete selected",
            icon="TRASH",
        )

        layout.operator(
            "skv.transfer_to",
            text="Transfer to...",
            icon="EXPORT",
        )

class SKV_PT_KeyframedPanel(Panel):
    bl_label = "Keyframed Shape Keys"
    bl_idname = "SKV_PT_keyframed_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MorphDeck"
    bl_order = 25

    def draw_header(self, context):
        self.layout.label(text="", icon="KEYINGSET")

    def draw(self, context):
        layout = self.layout.column(align=True)
        props = context.scene.skv_props
        obj = props.object_pick
        data = get_shape_key_data(obj)

        if not obj:
            layout.label(text="No selected object", icon="INFO")
            return

        if not data or not data.key_blocks:
            layout.label(text="No shape keys found", icon="INFO")
            return

        count = len(kd_keyframed_names(data))

        if count == 0:
            layout.label(
                text="No keyframed shape keys found",
                icon="INFO",
            )
            return

        layout.enabled = data.library is None

        row = layout.row(align=True)
        row.prop(
            props,
            "keyframed_search",
            text="",
            icon="VIEWZOOM",
        )
        row.operator(
            "skv.keyframed_search_clear",
            text="",
            icon="X",
        )

        row = layout.row(align=True)

        for label, mode in (
            ("Select All", "ALL"),
            ("Deselect", "CLEAR"),
            ("Invert", "INVERT"),
        ):
            row.operator(
                "skv.keyframed_select_visible",
                text=label,
            ).mode = mode

        layout.template_list(
            "SKV_UL_keyframed_keys",
            "",
            data,
            "key_blocks",
            props,
            "keyframed_index",
            rows=max(1, min(count, 5)),
        )

        visible = set(
            skv_list_key_names(data, props, "KEYFRAMED")
        )

        if (
            data.skv_selection_source != "KEYFRAMED"
            or not (visible & kd_selected_set(data))
        ):
            return

        row = layout.row(align=True)
        row.menu(
            "SKV_MT_move_to_group",
            text="Move to group",
            icon="FILE_FOLDER",
        )
        row.operator(
            "skv.create_group_from_selected",
            text="Create group",
            icon="NEWFOLDER",
        )

        row = layout.row(align=True)
        presets_row = row.row(align=True)
        presets_row.enabled = bool(context.scene.skv_global_presets)
        presets_row.menu(
            "SKV_MT_add_to_preset",
            text="Add to preset",
            icon="PRESET",
        )
        row.operator(
            "skv.global_preset_add_from_selected",
            text="Create preset",
            icon="PRESET",
        )

        row = layout.row(align=True)
        row.operator(
            "skv.reset_group_values",
            text="Zero selected values",
            icon="RECOVER_LAST",
        )
        row.operator(
            "skv.transfer_to",
            text="Transfer to...",
            icon="EXPORT",
        )

class SKV_PT_PresetsPanel(Panel):
    bl_label = "Presets"
    bl_idname = "SKV_PT_shape_keys_presets_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MorphDeck"
    bl_order = 30

    def draw_header(self, context):
        self.layout.label(text="", icon="PRESET")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        props = scene.skv_props

        if len(scene.skv_global_presets) == 0:
            layout.label(text="No presets found", icon="INFO")
            layout.operator("skv.global_preset_add_empty", text="Add Preset", icon="ADD")
            return

        gpreset = presets.get_active_global_preset(scene)
        preset_name = gpreset.name if gpreset else "None"

        presets_col = layout.column(align=True)

        hp = presets_col.row(align=True)
        ip = "TRIA_DOWN" if props.presets_open else "TRIA_RIGHT"
        hp.prop(props, "presets_open", text="", emboss=False, icon=ip)
        hp.label(text="Presets" if props.presets_open else f"Preset: {preset_name}")

        if props.presets_open:
            row = presets_col.row()
            row.template_list(
                "SKV_UL_global_presets",
                "",
                scene,
                "skv_global_presets",
                scene,
                "skv_global_preset_index",
                rows=max(1, min(len(scene.skv_global_presets), 5)),
            )
            col = row.column(align=True)
            col.operator("skv.global_preset_add_empty", icon="ADD", text="")
            col.operator("skv.global_preset_remove", icon="REMOVE", text="")

        gpreset = presets.get_active_global_preset(scene)
        if gpreset:
            grouped_items = list(presets.iter_preset_items_grouped(gpreset))
            if grouped_items:
                presets_col.separator()
                presets_col.label(text=f"Shape Keys in {preset_name}")

                active_obj = context.view_layer.objects.active

                for object_name, object_items, controller_item in grouped_items:
                    if not controller_item:
                        continue

                    is_active = bool(
                        active_obj and active_obj.name == object_name
                    )
                    object_col = presets_col.column(align=True)

                    # Keep the header accessible for object switching.
                    header = object_col.row(align=True)
                    header.prop(
                        controller_item,
                        "object_open",
                        text="",
                        emboss=False,
                        icon=(
                            "TRIA_DOWN"
                            if controller_item.object_open
                            else "TRIA_RIGHT"
                        ),
                    )

                    select_op = header.operator(
                        "skv.preset_object_select",
                        text=object_name,
                        icon="OBJECT_DATA",
                        emboss=False,
                    )
                    select_op.object_name = object_name

                    transfer_row = header.row(align=True)
                    transfer_row.enabled = is_active
                    transfer_op = transfer_row.operator(
                        "skv.preset_object_transfer",
                        text="",
                        icon="EXPORT",
                        emboss=False,
                    )
                    transfer_op.object_name = object_name
                    transfer_op.preset_index = scene.skv_global_preset_index

                    remove_row = header.row(align=True)
                    remove_row.enabled = is_active
                    remove_op = remove_row.operator(
                        "skv.preset_object_remove",
                        text="",
                        icon="TRASH",
                        emboss=False,
                    )
                    remove_op.object_name = object_name

                    if controller_item.object_open:
                        keys_col = object_col.column(align=True)
                        keys_col.enabled = is_active
                        presets.set_preset_list_filter_object(object_name)
                        keys_col.template_list(
                            "SKV_UL_global_preset_key_sliders",
                            object_name,
                            gpreset,
                            "items",
                            gpreset,
                            "items_index",
                            rows=max(1, min(len(object_items), 5)),
                        )


_LOCAL_CLASSES = (
    SKV_OT_InitializeObject,
    SKV_OT_SearchClear,
    SKV_OT_SynchronizeValues,
    SKV_OT_ShapeKeyAdd,
    SKV_OT_FinishShapeKeySculpt,
    SKV_Props,
    SKV_PT_ShapeKeysPanel,
    SKV_PT_FavoritesPanel,
    SKV_PT_KeyframedPanel,
    SKV_PT_PresetsPanel,
)

_ALL_CLASSES = _LOCAL_CLASSES + groups.CLASSES + presets.CLASSES + transfer.CLASSES


_SKV_OPERATOR_DESCRIPTIONS = {
    "skv.search_clear":
        "Clear the search filter in the current group",

    "skv.synchronize_values":
        "Copy matching shape key values from the source mesh to this object",

    "skv.shape_key_add":
        "Create a shape key in the Created group and enter Sculpt Mode",

    "skv.finish_shape_key_sculpt":
        "Finish editing the shape key and return to Object Mode",


    "skv.shape_key_toggle_visibility":
        "Enable or disable this shape key's contribution to the mesh",

    "skv.auto_keyframe_toggle":
        "Enable or disable automatic keyframes for this shape key",

    "skv.key_toggle_select":
        "Select or deselect this shape key for list actions",

    "skv.select_visible":
        "Change selection in the current group",

    "skv.favorites_select_visible":
        "Change selection in Favorites",

    "skv.keyframed_select_visible":
        "Change selection in Keyframed Shape Keys",


    "skv.move_selected_to_group":
        "Move selected shape keys to the chosen group",

    "skv.reset_group_values":
        "Set the selected shape key values to zero",

    "skv.delete_selected_shape_keys":
        "Delete the selected shape keys from the object after confirmation",

    "skv.group_add":
        "Create an empty shape key group",

    "skv.group_remove":
        "Remove this group and move its shape keys to another group",

    "skv.group_move":
        "Move this group up or down in the group list",

    "skv.create_group_from_selected":
        "Create a group and move the selected shape keys into it",

    "skv.transfer_to":
        "Transfer selected shape keys to another mesh",

    "skv.transfer_from":
        "Transfer all non-Basis shape keys from another mesh to this object",

    "skv.favorite_toggle":
        "Add this shape key to Favorites or remove it",

    "skv.favorites_clear":
        "Remove all entries from Favorites after confirmation; keep the shape keys",

    "skv.favorites_search_clear":
        "Clear the Favorites search filter",

    "skv.keyframed_search_clear":
        "Clear the Keyframed Shape Keys search filter",

    "skv.preset_item_capture_max":
        "Save this shape key's current value as its reference value in the preset",

    "skv.preset_item_remove":
        "Remove this shape key from the preset without deleting the shape key",

    "skv.preset_object_remove":
        "Remove all entries for this object from the preset; keep its shape keys",

    "skv.preset_object_select":
        "Select this mesh and make it the active object",

    "skv.preset_object_transfer":
        "Transfer this object's shape keys from the selected preset to another mesh",

    "skv.preset_toggle_visibility":
        "Enable or disable all shape keys belonging to this preset",

    "skv.preset_toggle_auto_keyframe":
        "Enable or disable Auto Keyframe for all shape keys in this preset",

    "skv.add_selected_to_preset":
        "Add the selected shape keys to the chosen preset",

    "skv.global_preset_add_empty":
        "Create an empty preset",

    "skv.global_preset_remove":
        "Remove this preset without deleting its shape keys",

    "skv.global_preset_add_from_selected":
        "Create a preset from the selected shape keys and their current values",
}


_SKV_SELECTION_SCOPES = {
    "skv.select_visible": "the current group",
    "skv.favorites_select_visible": "Favorites",
    "skv.keyframed_select_visible": "Keyframed Shape Keys",
}


def _skv_operator_description(cls, context, properties):
    operator_id = cls.bl_idname
    scope = _SKV_SELECTION_SCOPES.get(operator_id)

    if scope is not None:
        mode = getattr(properties, "mode", "")

        return {
            "ALL": (
                f"Select all shape keys matching the search in {scope}"
            ),
            "CLEAR": (
                f"Deselect shape keys matching the search in {scope}"
            ),
            "INVERT": (
                f"Invert selection for shape keys matching the search in {scope}"
            ),
            "NONZERO": (
                f"Select shape keys with non-zero values matching the search "
                f"in {scope}; deselect matching keys with zero values"
            ),
        }.get(mode, cls.bl_description)

    if operator_id == "skv.group_move":
        direction = getattr(properties, "direction", "")

        return {
            "UP": "Move the selected group one position up",
            "DOWN": "Move the selected group one position down",
        }.get(direction, cls.bl_description)

    if operator_id == "skv.favorite_toggle":
        scene = getattr(context, "scene", None)
        props = getattr(scene, "skv_props", None) if scene else None
        obj = getattr(props, "object_pick", None) if props else None
        data = get_shape_key_data(obj)
        name = getattr(properties, "key_name", "")

        if not data or not name:
            return cls.bl_description

        if name in kd_favorite_names(data):
            return "Remove this shape key from Favorites after confirmation"

        return "Add this shape key to Favorites"

    return cls.bl_description

def register():
    _register_custom_icons()
    for cls in _ALL_CLASSES:
        if issubclass(cls, Operator):
            operator_id = cls.bl_idname

            cls.bl_description = _SKV_OPERATOR_DESCRIPTIONS.get(
                operator_id,
                getattr(cls, "bl_description", "")
                or getattr(cls, "bl_label", operator_id),
            )

            if (
                    operator_id in _SKV_SELECTION_SCOPES
                    or operator_id in {
                "skv.group_move",
                "skv.favorite_toggle",
            }
            ):
                cls.description = classmethod(
                    _skv_operator_description
                )

        bpy.utils.register_class(cls)

    bpy.types.Scene.skv_props = PointerProperty(type=SKV_Props)

    bpy.types.Scene.skv_global_presets = CollectionProperty(type=presets.SKV_GlobalPreset)
    bpy.types.Scene.skv_global_preset_index = IntProperty(
        name="Preset Index",
        default=0,
        min=0,
        update=presets.global_preset_index_update,
    )

    bpy.types.Scene.skv_transfer_source_name = StringProperty(options={"SKIP_SAVE"})
    bpy.types.Scene.skv_transfer_target = PointerProperty(type=bpy.types.Object, poll=_poll_transfer_target)

    bpy.types.Scene.skv_sync_values_target_name = StringProperty(options={"SKIP_SAVE"})
    bpy.types.Scene.skv_sync_values_source = PointerProperty(
        type=bpy.types.Object,
        poll=_poll_sync_values_source,
    )


    bpy.types.Key.skv_groups = CollectionProperty(type=groups.SKV_Group)
    bpy.types.Key.skv_group_index = IntProperty(
        name="Group Index",
        default=0,
        min=0,
        update=groups.group_index_update,
    )
    bpy.types.Key.skv_last_group_index = IntProperty(
        name="Last Group Index",
        default=0,
        min=0,
        options={"SKIP_SAVE"},
    )

    bpy.types.Key.skv_selected = CollectionProperty(type=groups.SKV_SelectedName)
    bpy.types.Key.skv_selection_source = EnumProperty(
        name="Selection Source",
        items=[
            (
                "GROUP",
                "Group",
                "Selection belongs to the group list",
            ),
            (
                "FAVORITES",
                "Favorites",
                "Selection belongs to Favorites",
            ),
            (
                "KEYFRAMED",
                "Keyframed",
                "Selection belongs to the keyframed list",
            ),
        ],
        default="GROUP",
    )
    bpy.types.Key.skv_favorites = CollectionProperty(
        type=groups.SKV_SelectedName,
    )
    bpy.types.Key.skv_key_groups = CollectionProperty(type=groups.SKV_KeyGroupEntry)

    bpy.types.Key.skv_auto_keyframes = CollectionProperty(type=groups.SKV_AutoKeyframeEntry)


    _ensure_handler_installed()
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    if scene is not None:
        _auto_process_active_object(scene)


def unregister():
    _ensure_handler_removed()


    if hasattr(bpy.types.Key, "skv_auto_keyframes"):
        del bpy.types.Key.skv_auto_keyframes

    del bpy.types.Key.skv_key_groups
    del bpy.types.Key.skv_favorites
    if hasattr(bpy.types.Key, "skv_selection_source"):
        del bpy.types.Key.skv_selection_source
    del bpy.types.Key.skv_selected
    if hasattr(bpy.types.Key, "skv_last_group_index"):
        del bpy.types.Key.skv_last_group_index
    del bpy.types.Key.skv_groups
    del bpy.types.Key.skv_group_index

    if hasattr(bpy.types.Scene, "skv_sync_values_source"):
        del bpy.types.Scene.skv_sync_values_source
    if hasattr(bpy.types.Scene, "skv_sync_values_target_name"):
        del bpy.types.Scene.skv_sync_values_target_name

    if hasattr(bpy.types.Scene, "skv_transfer_target"):
        del bpy.types.Scene.skv_transfer_target
    if hasattr(bpy.types.Scene, "skv_transfer_source_name"):
        del bpy.types.Scene.skv_transfer_source_name

    if hasattr(bpy.types.Scene, "skv_global_preset_index"):
        del bpy.types.Scene.skv_global_preset_index
    if hasattr(bpy.types.Scene, "skv_global_presets"):
        del bpy.types.Scene.skv_global_presets

    del bpy.types.Scene.skv_props

    for cls in reversed(_ALL_CLASSES):
        bpy.utils.unregister_class(cls)

    _unregister_custom_icons()


if __name__ == "__main__":
    register()