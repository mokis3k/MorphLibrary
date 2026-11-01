import bpy
from bpy.types import (
    Operator,
    PropertyGroup,
    UIList,
    Menu,
)
from bpy.props import (
    IntProperty,
    FloatProperty,
    StringProperty,
    CollectionProperty,
    BoolProperty,
    PointerProperty,
)

from .transfer import TransferModalMixin
from .groups import _shape_key_value_data_path

from .common import (
    get_active_object,
    get_shape_key_data,
    is_initialized,
    tag_redraw_view3d,
    clear_selection_ui,
    ensure_init_setup_write,
    kd_selected_set,
    _is_basis_name,
    skv_shape_key_list_sync_active,
    skv_sync_shape_key_list_indices,
    kd_autokf_get_entry as _autokf_get_entry,
    kd_visible_selected_names
)

_PRESET_ITEM_SYNC_GUARD = False
_GLOBAL_PRESET_APPLY_GUARD = False
_PRESET_LIST_FILTER_OBJECT = ""


def _make_unique_preset_name(scene, base_name: str) -> str:
    base = (base_name or "").strip()
    if not base:
        base = "Preset"

    existing = set()
    if scene and hasattr(scene, "skv_global_presets"):
        existing = {p.name for p in scene.skv_global_presets}

    if base not in existing:
        return base

    index = 1
    while f"{base} {index}" in existing:
        index += 1
    return f"{base} {index}"


def set_preset_list_filter_object(object_name: str) -> None:
    global _PRESET_LIST_FILTER_OBJECT
    _PRESET_LIST_FILTER_OBJECT = (object_name or "").strip()


def get_active_global_preset(scene):
    if not scene or not hasattr(scene, "skv_global_presets") or not hasattr(scene, "skv_global_preset_index"):
        return None
    idx = int(scene.skv_global_preset_index)
    if 0 <= idx < len(scene.skv_global_presets):
        return scene.skv_global_presets[idx]
    return None


def sync_preset_item_values(
    context,
    preset,
    updated_ids=None,
) -> None:
    global _PRESET_ITEM_SYNC_GUARD

    previous_guard = _PRESET_ITEM_SYNC_GUARD
    _PRESET_ITEM_SYNC_GUARD = True
    try:
        for item, data, kb in _iter_resolved_preset_items(
                preset,
                updated_ids=updated_ids,
        ):
            value = float(kb.value)
            if float(item.value) != value:
                try:
                    item.value = value
                except (RuntimeError, AttributeError):
                    continue
    finally:
        _PRESET_ITEM_SYNC_GUARD = previous_guard


def preset_item_object(item):
    obj = getattr(item, "object_ref", None)
    if obj is not None:
        return obj

    # Resolve old files that only store object_name.
    name = (getattr(item, "object_name", "") or "").strip()
    return bpy.data.objects.get(name) if name else None


def migrate_legacy_preset_object_refs():
    for scene in bpy.data.scenes:
        for preset in getattr(scene, "skv_global_presets", ()):
            for item in preset.items:
                if getattr(item, "object_ref", None) is not None:
                    continue

                name = (getattr(item, "object_name", "") or "").strip()
                obj = bpy.data.objects.get(name) if name else None
                if obj is not None:
                    item.object_ref = obj


def _preset_item_value_update(self, context):
    global _PRESET_ITEM_SYNC_GUARD
    if _PRESET_ITEM_SYNC_GUARD:
        return

    key_name = (self.key_name or "").strip()
    if not key_name:
        return

    obj = preset_item_object(self)
    if not obj or getattr(obj, "type", None) != "MESH":
        return

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if getattr(key_data, "library", None) is not None:
        return

    kb = key_data.key_blocks.get(key_name)
    if not kb:
        return

    try:
        new_val = float(self.value)
    except Exception:
        return

    try:
        kb.value = new_val
    except Exception:
        return

    tag_redraw_view3d(context)


def global_preset_apply(preset, context) -> None:
    global _GLOBAL_PRESET_APPLY_GUARD, _PRESET_ITEM_SYNC_GUARD

    if _GLOBAL_PRESET_APPLY_GUARD:
        return

    previous_guard = _PRESET_ITEM_SYNC_GUARD
    _GLOBAL_PRESET_APPLY_GUARD = True
    _PRESET_ITEM_SYNC_GUARD = True

    try:
        factor = float(preset.value)

        for item, data, kb in _iter_resolved_preset_items(preset):
            if data.library is not None:
                continue

            try:
                value = factor * float(item.max_value)

                if float(kb.value) != value:
                    kb.value = value

                actual = float(kb.value)
                if float(item.value) != actual:
                    item.value = actual
            except (RuntimeError, ValueError, TypeError):
                continue
    finally:
        _PRESET_ITEM_SYNC_GUARD = previous_guard
        _GLOBAL_PRESET_APPLY_GUARD = False

    tag_redraw_view3d(context)

def global_preset_index_update(self, context):
    preset = get_active_global_preset(self)
    if preset is not None:
        sync_preset_item_values(context, preset)

    tag_redraw_view3d(context)

def global_preset_value_update(self, context):
    global_preset_apply(self, context)


def _iter_resolved_preset_items(preset, updated_ids=None):
    objects = {}
    key_maps = {}

    for item in preset.items:
        obj = preset_item_object(item)
        if obj is None:
            continue

        object_pointer = obj.as_pointer()

        if object_pointer not in objects:
            data = (
                get_shape_key_data(obj)
                if obj.type == "MESH"
                else None
            )

            relevant = data is not None and (
                updated_ids is None
                or obj.as_pointer() in updated_ids
                or obj.data.as_pointer() in updated_ids
                or data.as_pointer() in updated_ids
            )

            if relevant:
                pointer = data.as_pointer()

                if pointer not in key_maps:
                    key_maps[pointer] = {
                        kb.name: kb
                        for kb in data.key_blocks
                    }

                keys = key_maps[pointer]
            else:
                keys = {}

            objects[object_pointer] = (data, keys)

        data, keys = objects[object_pointer]
        kb = keys.get(item.key_name)

        if kb is not None:
            yield item, data, kb


def _iter_preset_key_blocks(preset):
    for item, data, kb in _iter_resolved_preset_items(preset):
        yield data, kb

def _preset_all_muted(preset) -> bool:
    found = False
    for _key_data, kb in _iter_preset_key_blocks(preset):
        found = True
        if not bool(getattr(kb, "mute", False)):
            return False
    return found and True


def _preset_all_autokey_enabled(preset) -> bool:
    maps = {}
    found = False

    for data, kb in _iter_preset_key_blocks(preset):
        found = True
        pointer = data.as_pointer()

        if pointer not in maps:
            maps[pointer] = {
                item.name: bool(item.enabled)
                for item in getattr(data, "skv_auto_keyframes", ())
            }

        if not maps[pointer].get(kb.name, False):
            return False

    return found


def preset_item_capture_max_enabled(it) -> bool:
    obj = preset_item_object(it)
    if not obj or getattr(obj, "type", None) != "MESH":
        return False

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return False

    kb = key_data.key_blocks.get(it.key_name)
    if not kb:
        return False

    try:
        return abs(float(kb.value) - float(it.max_value)) > 1e-6
    except Exception:
        return False


def iter_preset_items_grouped(preset):
    grouped = {}
    order = []
    for idx, it in enumerate(getattr(preset, "items", [])):
        obj = preset_item_object(it)
        obj_name = (
            obj.name
            if obj is not None
            else (it.object_name or "").strip()
        )
        if not obj_name:
            obj_name = "Unknown Object"
        if obj_name not in grouped:
            grouped[obj_name] = []
            order.append(obj_name)
        grouped[obj_name].append((idx, it))

    for obj_name in order:
        items = grouped[obj_name]
        controller_item = items[0][1] if items else None
        yield obj_name, items, controller_item


def _set_item_value_from_key_block(it, kb) -> None:
    global _PRESET_ITEM_SYNC_GUARD
    try:
        it.max_value = float(kb.value)
        _PRESET_ITEM_SYNC_GUARD = True
        try:
            it.value = float(kb.value)
        finally:
            _PRESET_ITEM_SYNC_GUARD = False
    except Exception:
        it.max_value = 0.0
        _PRESET_ITEM_SYNC_GUARD = True
        try:
            it.value = 0.0
        finally:
            _PRESET_ITEM_SYNC_GUARD = False


def add_shape_key_to_preset(preset, obj, key_name: str):
    if not preset or not obj or getattr(obj, "type", None) != "MESH" or not key_name:
        return False

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return False

    kb = key_data.key_blocks.get(key_name)
    if not kb:
        return False

    for it in preset.items:
        if preset_item_object(it) == obj and it.key_name == key_name:
            return False

    it = preset.items.add()
    it.object_ref = obj
    it.object_name = obj.name
    it.key_name = key_name
    it.object_open = True
    _set_item_value_from_key_block(it, kb)
    return True


def inherit_transferred_keys_to_presets(source_obj, target_obj, key_names) -> int:
    if not source_obj or not target_obj or source_obj == target_obj:
        return 0
    if getattr(source_obj, "type", None) != "MESH" or getattr(target_obj, "type", None) != "MESH":
        return 0

    names = [n for n in (key_names or []) if n]
    if not names:
        return 0

    scene = bpy.context.scene
    if not scene or not hasattr(scene, "skv_global_presets"):
        return 0

    selected_names = set(names)
    inherited_count = 0

    for preset in scene.skv_global_presets:
        source_keys_in_preset = {
            it.key_name
            for it in preset.items
            if preset_item_object(it) == source_obj
            and it.key_name in selected_names
        }
        if not source_keys_in_preset:
            continue

        for key_name in source_keys_in_preset:
            if add_shape_key_to_preset(preset, target_obj, key_name):
                inherited_count += 1

    return inherited_count


class SKV_GlobalPresetItem(PropertyGroup):
    object_ref: PointerProperty(
        name="Object",
        type=bpy.types.Object,
    )
    object_name: StringProperty(name="Object", default="")
    key_name: StringProperty(name="Shape Key", default="")
    object_open: BoolProperty(name="Object Open", default=True)
    max_value: FloatProperty(name="Max", default=1.0)

    value: FloatProperty(
        name="Value",
        default=0.0,
        min=-10.0,
        max=10.0,
        soft_min=0.0,
        soft_max=1.0,
        update=_preset_item_value_update,
    )


def global_preset_items_index_update(self, context):
    # Sync Shape Keys in preset list selection to all shape key lists.
    if skv_shape_key_list_sync_active():
        return

    gpreset = self

    idx = int(getattr(gpreset, "items_index", -1))
    if idx < 0 or idx >= len(gpreset.items):
        return

    it = gpreset.items[idx]
    obj = preset_item_object(it)
    if not obj or getattr(obj, "type", None) != "MESH":
        return

    key_name = (it.key_name or "").strip()
    if not key_name:
        return

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return

    if not key_data.key_blocks.get(key_name):
        return

    try:
        for ob in context.view_layer.objects:
            if ob.select_get():
                ob.select_set(False)
    except Exception:
        pass

    try:
        obj.select_set(True)
        context.view_layer.objects.active = obj
        context.scene.skv_props.object_pick = obj
    except Exception:
        pass

    skv_sync_shape_key_list_indices(
        context,
        obj,
        key_name,
        set_blender_active=True,
    )


class SKV_GlobalPreset(PropertyGroup):
    name: StringProperty(name="Preset Name", default="Preset")
    value: FloatProperty(
        name="Value",
        default=0.0,
        min=0.0,
        max=1.0,
        update=global_preset_value_update,
    )
    items: CollectionProperty(type=SKV_GlobalPresetItem)
    items_index: IntProperty(
        name="Items Index",
        default=-1,
        min=-1,
        update=global_preset_items_index_update,
    )


class SKV_OT_preset_item_capture_max(Operator):
    bl_idname = "skv.preset_item_capture_max"
    bl_label = "Capture Max (Item)"
    bl_options = {"REGISTER", "UNDO"}

    item_index: IntProperty(name="Item Index", default=0)

    def execute(self, context):
        scene = context.scene
        preset = get_active_global_preset(scene)
        if not preset:
            return {"CANCELLED"}

        idx = int(self.item_index)
        if idx < 0 or idx >= len(preset.items):
            return {"CANCELLED"}

        it = preset.items[idx]

        obj = preset_item_object(it)
        if not obj or getattr(obj, "type", None) != "MESH":
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(it.key_name)
        if not kb:
            return {"CANCELLED"}

        try:
            it.max_value = float(kb.value)
        except Exception:
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_preset_item_remove(Operator):
    bl_idname = "skv.preset_item_remove"
    bl_label = "Remove From Preset"
    bl_description = "Remove this entry from the preset without deleting the shape key"
    bl_options = {"REGISTER", "UNDO"}

    item_index: IntProperty(name="Item Index", default=-1)

    def execute(self, context):
        preset = get_active_global_preset(context.scene)
        if preset is None:
            return {"CANCELLED"}

        index = int(self.item_index)
        if index < 0 or index >= len(preset.items):
            return {"CANCELLED"}

        # Clear the active row before collection indices shift.
        preset.items_index = -1
        preset.items.remove(index)

        tag_redraw_view3d(context)
        return {"FINISHED"}

class SKV_OT_preset_object_remove(Operator):
    bl_idname = "skv.preset_object_remove"
    bl_label = "Delete object from preset"
    bl_description = "Delete object from preset"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(name="Object Name", default="")

    def execute(self, context):
        preset = get_active_global_preset(context.scene)
        if preset is None or not self.object_name:
            return {"CANCELLED"}

        indices = []
        for index, item in enumerate(preset.items):
            obj = preset_item_object(item)
            if (
                (obj is not None and obj.name == self.object_name)
                or (
                    obj is None
                    and item.object_name == self.object_name
                )
            ):
                indices.append(index)
        if not indices:
            return {"CANCELLED"}

        # Clear the active row before removing entries and shifting indices.
        preset.items_index = -1
        for index in reversed(indices):
            preset.items.remove(index)

        tag_redraw_view3d(context)
        return {"FINISHED"}

class SKV_OT_preset_object_select(Operator):
    bl_idname = "skv.preset_object_select"
    bl_label = "Select Object"
    bl_description = "Select this object and make it active"
    bl_options = {"REGISTER", "UNDO"}

    object_name: StringProperty(name="Object Name", default="")

    def execute(self, context):
        obj = context.view_layer.objects.get(self.object_name)
        if obj is None or obj.type != "MESH":
            self.report(
                {"WARNING"},
                "Object is not available in the current view layer.",
            )
            return {"CANCELLED"}

        if obj.hide_select or obj.hide_get():
            self.report(
                {"WARNING"},
                "Object is hidden or selection is disabled.",
            )
            return {"CANCELLED"}

        try:
            if context.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")

            obj.select_set(True)
            for other in context.view_layer.objects:
                if other != obj and other.select_get():
                    other.select_set(False)

            context.view_layer.objects.active = obj
        except RuntimeError as error:
            self.report({"WARNING"}, str(error))
            return {"CANCELLED"}

        context.scene.skv_props.object_pick = obj
        tag_redraw_view3d(context)
        return {"FINISHED"}

class SKV_OT_preset_object_transfer(
    TransferModalMixin,
    Operator,
):
    bl_idname = "skv.preset_object_transfer"
    bl_label = "Transfer Shape Keys"
    bl_description = "Transfer this object's shape keys from the selected preset"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    object_name: StringProperty(name="Source Object", default="")
    preset_index: IntProperty(name="Preset Index", default=-1)

    def invoke(self, context, event):
        context.scene.skv_transfer_source_name = self.object_name

        target = context.scene.skv_transfer_target
        if target and target.name == self.object_name:
            context.scene.skv_transfer_target = None

        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        self.layout.prop(context.scene, "skv_transfer_target", text="Target")

    def execute(self, context):
        from .transfer import Transfer

        scene = context.scene
        index = int(self.preset_index)
        if index < 0 or index >= len(scene.skv_global_presets):
            self.report({"ERROR"}, "Preset is no longer available.")
            return {"CANCELLED"}

        preset = scene.skv_global_presets[index]
        source = bpy.data.objects.get(self.object_name)
        target = scene.skv_transfer_target

        if not source or source.type != "MESH":
            self.report({"ERROR"}, "Source mesh is not available.")
            return {"CANCELLED"}

        if not target or target.type != "MESH" or target == source:
            self.report({"ERROR"}, "Choose a target mesh different from source.")
            return {"CANCELLED"}

        source_data = get_shape_key_data(source)
        target_data = get_shape_key_data(target)

        if not source_data or not source_data.key_blocks:
            self.report({"ERROR"}, "Source has no Shape Keys.")
            return {"CANCELLED"}

        if target.data.library is not None or (
            target_data and target_data.library is not None
        ):
            self.report({"ERROR"}, "Target data is linked (read-only).")
            return {"CANCELLED"}

        # Snapshot the source preset entries before adding target entries.
        source_max = {
            item.key_name: float(item.max_value)
            for item in preset.items
            if preset_item_object(item) == source
        }
        names = [
            kb.name for kb in source_data.key_blocks
            if kb.name in source_max
            and not _is_basis_name(source_data, kb.name)
        ]

        if not names:
            self.report({"ERROR"}, "No valid source Shape Keys in this preset.")
            return {"CANCELLED"}

        if target_data and any(
            _is_basis_name(target_data, name) for name in names
        ):
            self.report({"ERROR"}, "A source key name matches the target Basis.")
            return {"CANCELLED"}

        existing = [
            name for name in names
            if target_data and target_data.key_blocks.get(name) is not None
        ]
        missing = [name for name in names if name not in existing]

        self._job_source = source
        self._job_target = target
        self._preset_names = names
        self._preset_existing = existing
        self._preset_max = source_max
        self._preset_pointer = preset.as_pointer()
        self._preset_index = index

        transfer = (
            Transfer(
                source=source,
                target=target,
                vertex_group=None,
            )
            if missing else None
        )

        return self._transfer_begin(
            context,
            transfer,
            missing,
        )

    def _finish_transfer(self, context):
        from .groups import (
            inherit_transferred_groups,
            inherit_transferred_keyframes,
        )

        scene = self._job_scene
        preset = scene.skv_global_presets[
            self._preset_index
        ]

        if preset.as_pointer() != self._preset_pointer:
            raise RuntimeError(
                "The selected preset changed during transfer."
            )

        source = self._job_source
        target = self._job_target
        source_data = get_shape_key_data(source)

        names = self._preset_names
        existing = self._preset_existing
        source_max = self._preset_max
        missing = self._job_names
        created = list(missing)

        target_data = get_shape_key_data(target)
        resolved = [
            name for name in names
            if name in existing or name in created
        ]

        if not target_data or not resolved:
            self.report(
                {"WARNING"},
                "No Shape Keys were transferred or matched.",
            )
            return {"CANCELLED"}

        # Existing target geometry, values and groups are preserved.
        # Synchronize values for both new and existing target keys.
        for name in resolved:
                target_data.key_blocks[name].value = (
                    source_data.key_blocks[name].value
                )

        if created:
            inherit_transferred_groups(source, target, created)

        # Inherit membership and maxima only in the selected preset.
        for name in resolved:
            add_shape_key_to_preset(preset, target, name)
            for item in preset.items:
                if (
                    preset_item_object(item) == target
                    and item.key_name == name
                ):
                    item.max_value = source_max[name]
                    break

        copied = 0
        if target_data != source_data:
            # Isolate shared target actions before adding missing FCurves.
            anim = getattr(target_data, "animation_data", None)
            action = getattr(anim, "action", None) if anim else None

            if action and action.users > 1:
                slot = getattr(anim, "action_slot", None)
                identifier = getattr(slot, "identifier", "")
                anim.action = action.copy()

                if identifier:
                    for new_slot in getattr(anim.action, "slots", ()):
                        if new_slot.identifier == identifier:
                            anim.action_slot = new_slot
                            break

            copied = inherit_transferred_keyframes(
                source, target, resolved,
            )

        preset.items_index = -1
        sync_preset_item_values(context, preset)
        tag_redraw_view3d(context)

        failed = len(missing) - len(created)
        level = {"WARNING"} if failed else {"INFO"}
        self.report(
            level,
            f"Created: {len(created)}, existing: {len(existing)}, "
            f"animated keys copied: {copied}, failed: {failed}",
        )
        return {"FINISHED"}

class SKV_OT_preset_toggle_visibility(Operator):
    bl_idname = "skv.preset_toggle_visibility"
    bl_label = "Toggle Preset Visibility"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0)

    def execute(self, context):
        scene = context.scene
        idx = int(self.preset_index)
        if idx < 0 or idx >= len(scene.skv_global_presets):
            return {"CANCELLED"}

        preset = scene.skv_global_presets[idx]
        target_mute = not _preset_all_muted(preset)

        changed = False
        for _key_data, kb in _iter_preset_key_blocks(preset):
            try:
                kb.mute = target_mute
                changed = True
            except Exception:
                pass

        if not changed:
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_preset_toggle_auto_keyframe(Operator):
    bl_idname = "skv.preset_toggle_auto_keyframe"
    bl_label = "Toggle Preset Auto Keyframe"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0)

    def execute(self, context):
        scene = context.scene
        idx = int(self.preset_index)
        if idx < 0 or idx >= len(scene.skv_global_presets):
            return {"CANCELLED"}

        preset = scene.skv_global_presets[idx]
        target_enabled = not _preset_all_autokey_enabled(preset)
        frame = int(context.scene.frame_current)

        changed = False
        for key_data, kb in _iter_preset_key_blocks(preset):
            if getattr(key_data, "library", None) is not None:
                continue
            entry = _autokf_get_entry(key_data, kb.name, create=True)
            if not entry:
                continue
            try:
                entry.enabled = target_enabled
                entry.last_frame = frame
                try:
                    entry.last_value = float(kb.value)
                except Exception:
                    entry.last_value = 0.0

                if target_enabled:
                    try:
                        key_data.keyframe_insert(data_path=_shape_key_value_data_path(kb.name), frame=frame)
                    except Exception:
                        pass

                changed = True
            except Exception:
                pass

        if not changed:
            return {"CANCELLED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_UL_global_presets(UIList):
    bl_idname = "SKV_UL_global_presets"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        preset = item
        row = layout.row(align=True)

        row.prop(preset, "name", text="", emboss=False, icon="PRESET")
        row.prop(preset, "value", text="", slider=True)

        vis_icon = "HIDE_ON" if _preset_all_muted(preset) else "HIDE_OFF"
        opv = row.operator("skv.preset_toggle_visibility", text="", icon=vis_icon, emboss=False)
        opv.preset_index = index

        opk = row.operator(
            "skv.preset_toggle_auto_keyframe",
            text="",
            icon="KEYINGSET",
            emboss=True,
            depress=_preset_all_autokey_enabled(preset),
        )
        opk.preset_index = index


class SKV_UL_global_preset_key_sliders(UIList):
    bl_idname = "SKV_UL_global_preset_key_sliders"

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt_flags = []
        flt_neworder = []

        target = _PRESET_LIST_FILTER_OBJECT
        bitflag = self.bitflag_filter_item

        for it in items:
            obj = preset_item_object(it)
            item_object_name = obj.name if obj else it.object_name
            if not target or item_object_name == target:
                flt_flags.append(bitflag)
            else:
                flt_flags.append(0)

        return flt_flags, flt_neworder

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        it = item
        row = layout.row(align=True)

        kb = None
        obj = preset_item_object(it)
        if obj and getattr(obj, "type", None) == "MESH":
            key_data = get_shape_key_data(obj)
            if key_data and getattr(key_data, "key_blocks", None):
                kb = key_data.key_blocks.get(it.key_name)

        if kb:
            row.prop(kb, "name", text="", emboss=False)
        else:
            row.label(text=it.key_name or "Invalid", icon="ERROR")

        row.prop(it, "value", text="", slider=True)

        op_row = row.row(align=True)
        op_row.enabled = preset_item_capture_max_enabled(it)
        op = op_row.operator("skv.preset_item_capture_max", text="", icon="COPYDOWN", emboss=True)
        op.item_index = index

        remove_op = row.operator(
            "skv.preset_item_remove",
            text="",
            icon="REMOVE",
            emboss=False,
        )
        remove_op.item_index = index


class SKV_MT_add_to_preset(Menu):
    bl_label = "Add to preset"
    bl_idname = "SKV_MT_add_to_preset"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        if not hasattr(scene, "skv_global_presets") or len(scene.skv_global_presets) == 0:
            layout.label(text="No presets")
            return
        for i, p in enumerate(scene.skv_global_presets):
            op = layout.operator("skv.add_selected_to_preset", text=p.name, icon="PRESET")
            op.preset_index = i


class SKV_OT_add_selected_to_preset(Operator):
    bl_idname = "skv.add_selected_to_preset"
    bl_label = "Add Selected To Preset"
    bl_options = {"REGISTER", "UNDO"}

    preset_index: IntProperty(name="Preset Index", default=0, min=0)

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not getattr(key_data, "key_blocks", None):
            return False
        if getattr(key_data, "library", None) is not None:
            return False
        scene = context.scene
        if not hasattr(scene, "skv_global_presets") or len(scene.skv_global_presets) == 0:
            return False
        return any(not _is_basis_name(key_data, n) for n in kd_selected_set(key_data))

    def execute(self, context):
        scene = context.scene
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        if self.preset_index < 0 or self.preset_index >= len(scene.skv_global_presets):
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )
        selected = [n for n in selected if n and not _is_basis_name(key_data, n)]
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        preset = scene.skv_global_presets[self.preset_index]
        added = 0
        for kname in selected:
            if add_shape_key_to_preset(preset, obj, kname):
                added += 1

        if added == 0:
            self.report({"INFO"}, "Nothing added (already present or invalid).")
            return {"CANCELLED"}

        try:
            context.scene.skv_props.presets_open = True
        except Exception:
            pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetAddEmpty(Operator):
    bl_idname = "skv.global_preset_add_empty"
    bl_label = "Add Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene

        preset = scene.skv_global_presets.add()
        preset.name = _make_unique_preset_name(scene, "Preset")
        preset.value = 0.0
        scene.skv_global_preset_index = max(0, len(scene.skv_global_presets) - 1)

        try:
            context.scene.skv_props.presets_open = True
        except Exception:
            pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GlobalPresetRemove(Operator):
    bl_idname = "skv.global_preset_remove"
    bl_label = "Remove Preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        idx = int(getattr(scene, "skv_global_preset_index", 0))
        if 0 <= idx < len(scene.skv_global_presets):
            scene.skv_global_presets.remove(idx)
            scene.skv_global_preset_index = max(0, min(idx, len(scene.skv_global_presets) - 1))
            tag_redraw_view3d(context)
            return {"FINISHED"}
        return {"CANCELLED"}


class SKV_OT_GlobalPresetAddFromSelected(Operator):
    bl_idname = "skv.global_preset_add_from_selected"
    bl_label = "Create Preset"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Preset Name", default="Preset")

    def invoke(self, context, event):
        self.name = "Preset"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        scene = context.scene
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )
        if not selected:
            self.report({"INFO"}, "Select shape keys first (Select mode).")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        name = _make_unique_preset_name(scene, self.name)
        preset = scene.skv_global_presets.add()
        preset.name = name
        preset.items.clear()

        added = 0
        for kname in selected:
            if _is_basis_name(key_data, kname):
                continue
            if add_shape_key_to_preset(preset, obj, kname):
                added += 1

        if added == 0:
            scene.skv_global_presets.remove(len(scene.skv_global_presets) - 1)
            self.report({"INFO"}, "No valid shape keys selected for preset.")
            return {"CANCELLED"}

        scene.skv_global_preset_index = len(scene.skv_global_presets) - 1
        preset.value = 1.0

        try:
            context.scene.skv_props.presets_open = True
        except Exception:
            pass

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


CLASSES = (
    SKV_GlobalPresetItem,
    SKV_GlobalPreset,
    SKV_OT_preset_object_transfer,
    SKV_OT_preset_item_capture_max,
    SKV_OT_preset_object_select,
    SKV_OT_preset_object_remove,
    SKV_OT_preset_item_remove,
    SKV_OT_preset_toggle_visibility,
    SKV_OT_preset_toggle_auto_keyframe,
    SKV_UL_global_presets,
    SKV_UL_global_preset_key_sliders,
    SKV_MT_add_to_preset,
    SKV_OT_add_selected_to_preset,
    SKV_OT_GlobalPresetAddEmpty,
    SKV_OT_GlobalPresetRemove,
    SKV_OT_GlobalPresetAddFromSelected,
)
