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
    if _CONTROL_SYNC_GUARD:
        return
    preset = get_active_global_preset(self)
    if preset is not None:
        sync_preset_item_values(context, preset)

    tag_redraw_view3d(context)

def global_preset_value_update(self, context):
    if _CONTROL_SYNC_GUARD:
        return

    global_preset_apply(self, context)
    _push_value_to_control(self)


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
    # [ANIM-FIX] Новое: при добавлении шейпкея в пресет создаём контрол
    # на шейпкеях первого объекта пресета (если его ещё нет).
    # Create a control only for a preset that has never had one.
    if not preset.control_uid:
        ensure_preset_control(preset)

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
    if _CONTROL_SYNC_GUARD:
        return
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


# [ANIM-FIX] Новый класс. Анимируемый слайдер пресета, который хранится на
# шейпкеях объекта: Key.skv_preset_controls[...].value.
# Поэтому ключи (клавиша I на слайдере) попадают в Shape Key action объекта
# и переносятся вместе с мешем. name = uid пресета (связь с SKV_GlobalPreset).
class SKV_PresetControl(PropertyGroup):
    value: FloatProperty(
        name="Preset",
        default=0.0,
        min=0.0,
        max=1.0,
        update=lambda self, context: _control_value_update(self, context),
    )


class SKV_GlobalPreset(PropertyGroup):
    name: StringProperty(name="Preset Name", default="Preset")
    # [ANIM-FIX] Новое поле: постоянный ID пресета. По нему контрол на объекте
    # находит свой пресет (не зависит от порядка и имени пресета).
    uid: StringProperty(name="Preset ID", default="")
    control_uid: StringProperty(
        name="Control ID",
        default="",
    )
    control_owner: PointerProperty(
        name="Control Owner",
        type=bpy.types.Key,
    )
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

    copy_preset_animation: BoolProperty(
        name="Copy preset animation",
        description=(
            "Create a separate target preset and copy supported "
            "animation from the source preset control"
        ),
        default=False,
    )

    def invoke(self, context, event):
        context.scene.skv_transfer_source_name = self.object_name

        target = context.scene.skv_transfer_target
        if target and target.name == self.object_name:
            context.scene.skv_transfer_target = None

        return context.window_manager.invoke_props_dialog(self, width=340)

    def draw(self, context):
        layout = self.layout

        layout.prop(
            context.scene,
            "skv_transfer_target",
            text="Target",
        )
        layout.prop(self, "copy_preset_animation")

        if self.copy_preset_animation:
            layout.label(
                text="A separate target preset will be created.",
                icon="INFO",
            )

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

        self._job_copy_preset_animation = bool(
            self.copy_preset_animation
        )
        self._job_control_snapshot = None

        if self._job_copy_preset_animation:
            try:
                validate_target_preset_animation(target, names)
                self._job_control_snapshot = _animation_snapshot(
                    scene,
                    preset,
                )
            except Exception as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

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

        copied = 0
        created_preset = None

        if self._job_copy_preset_animation:
            created_preset = create_transferred_preset(
                scene=scene,
                source_preset=preset,
                target=target,
                key_names=resolved,
                maxima=source_max,
                animation=self._job_control_snapshot,
            )

        else:
            # Keep the existing shared-preset transfer behavior.
            for name in resolved:
                add_shape_key_to_preset(preset, target, name)

                for item in preset.items:
                    if (
                        preset_item_object(item) == target
                        and item.key_name == name
                    ):
                        item.max_value = source_max[name]
                        break

            if target_data != source_data:
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
                    source,
                    target,
                    resolved,
                )

        preset.items_index = -1
        sync_preset_item_values(context, preset)
        if created_preset is not None:
            sync_preset_item_values(context, created_preset)
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
        # [ANIM-FIX] Было: row.prop(preset, "value", ...)
        # Стало: рисуем слайдер контрола на объекте, чтобы ключ (I) писался
        # в объект, а не в сцену. Если контрола нет — старый слайдер.
        found = find_preset_control(preset)
        if found is not None:
            row.prop(found[1], "value", text="", slider=True)
        else:
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
    bl_description = (
        "Remove the preset and its exclusive control animation"
    )
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        self.layout.label(
            text="Remove this preset and its control animation?",
            icon="QUESTION",
        )
        self.layout.label(
            text="Controls shared with other presets are preserved."
        )

    def execute(self, context):
        scene = context.scene
        index = int(scene.skv_global_preset_index)

        if not 0 <= index < len(scene.skv_global_presets):
            return {"CANCELLED"}

        preset = scene.skv_global_presets[index]
        found = find_preset_control(preset)
        keys = []

        if found is not None:
            key, ctrl = found

            other_users = [
                other
                for _owner_scene, other in _control_users(key, ctrl.name)
                if other.as_pointer() != preset.as_pointer()
            ]

            if not other_users:
                keys.append(key)

        try:
            with ControlTransaction(scene, keys):
                remove_preset_control(preset)
                remove_scene_preset_animation(scene, preset)

                scene.skv_global_presets.remove(index)
                scene.skv_global_preset_index = max(
                    0,
                    min(index, len(scene.skv_global_presets) - 1),
                )

        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            # Keep an undo step available if rollback itself failed.
            return {"FINISHED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


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
    SKV_PresetControl,  # [ANIM-FIX] новый класс, регистрируется первым
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


import math as _math
import re as _re
import uuid as _uuid


CONTROL_SCHEMA_VERSION = 2

_CONTROL_SYNC_GUARD = False
_CONTROL_FRAME_GUARD = False
_CONTROL_TRANSACTION = None

_SCENE_PRESET_PATH_RE = _re.compile(
    r'^skv_global_presets\[(?:(\d+)|"((?:[^"\\]|\\.)*)")\]\.value$'
)
_KEY_CONTROL_PATH_RE = _re.compile(
    r'^skv_preset_controls\[(?:(\d+)|"((?:[^"\\]|\\.)*)")\]\.value$'
)


def _unescape(name):
    return name.replace('\\"', '"').replace("\\\\", "\\")


def _escape(name):
    return bpy.utils.escape_identifier(name)


def ensure_preset_uid(preset):
    if not preset.uid:
        preset.uid = _uuid.uuid4().hex
    return preset.uid


def _preset_keys(preset):
    seen = set()

    for item in preset.items:
        obj = preset_item_object(item)
        key = get_shape_key_data(obj)

        if key is None:
            continue

        pointer = key.as_pointer()
        if pointer in seen:
            continue

        seen.add(pointer)
        yield key


def find_preset_control(preset):
    key = preset.control_owner
    uid = preset.control_uid

    if key is None or not uid:
        return None

    controls = getattr(key, "skv_preset_controls", None)
    if controls is None:
        return None

    ctrl = controls.get(uid)
    return (key, ctrl) if ctrl is not None else None


def control_data_path(ctrl):
    return f'skv_preset_controls["{_escape(ctrl.name)}"].value'


def control_data_paths(key, ctrl):
    paths = {control_data_path(ctrl)}
    index = key.skv_preset_controls.find(ctrl.name)

    if index >= 0:
        paths.add(f"skv_preset_controls[{index}].value")

    return paths


def _control_users(key, uid):
    for scene in bpy.data.scenes:
        for preset in getattr(scene, "skv_global_presets", ()):
            if (
                preset.control_owner == key
                and preset.control_uid == uid
            ):
                yield scene, preset


def _check_writable(id_data):
    if id_data is None:
        raise RuntimeError("The datablock is missing.")

    if id_data.library is not None:
        raise RuntimeError("The datablock is linked and read-only.")


def _create_control(key, value):
    _check_writable(key)

    global _CONTROL_SYNC_GUARD

    ctrl = key.skv_preset_controls.add()
    ctrl.name = _uuid.uuid4().hex

    previous = _CONTROL_SYNC_GUARD
    _CONTROL_SYNC_GUARD = True
    try:
        ctrl.value = float(value)
    finally:
        _CONTROL_SYNC_GUARD = previous

    return ctrl


def ensure_preset_control(preset):
    ensure_preset_uid(preset)

    found = find_preset_control(preset)
    if found is not None:
        return found

    # A non-empty identifier means that the original control is missing.
    if preset.control_uid:
        return None

    for key in _preset_keys(preset):
        if key.library is not None:
            continue

        ctrl = _create_control(key, preset.value)
        preset.control_owner = key
        preset.control_uid = ctrl.name
        return key, ctrl

    return None


def prepare_preset_metadata():
    """Upgrade legacy bindings without treating new empty presets as broken."""
    seen_uids = set()

    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue

        old_version = int(
            getattr(scene, "skv_control_schema_version", 0)
        )
        upgrade_legacy = old_version < CONTROL_SCHEMA_VERSION

        for preset in getattr(scene, "skv_global_presets", ()):
            old_uid = preset.uid

            if upgrade_legacy and not preset.control_uid and old_uid:
                matches = [
                    key
                    for key in bpy.data.shape_keys
                    if getattr(key, "skv_preset_controls", None) is not None
                    and key.skv_preset_controls.get(old_uid) is not None
                ]

                if len(matches) == 1:
                    preset.control_owner = matches[0]
                    preset.control_uid = old_uid

                elif len(matches) > 1:
                    print(
                        "MorphDeck: ambiguous legacy control binding "
                        f"for preset {preset.name!r}"
                    )

                # No match means that no binding can be recovered.
                # Keep control_uid empty instead of inventing a lost owner.

            if not preset.uid or preset.uid in seen_uids:
                preset.uid = _uuid.uuid4().hex

            seen_uids.add(preset.uid)

        scene.skv_control_schema_version = CONTROL_SCHEMA_VERSION


def _set_preset_value_without_apply(preset, value):
    global _CONTROL_SYNC_GUARD

    previous = _CONTROL_SYNC_GUARD
    _CONTROL_SYNC_GUARD = True
    try:
        preset.value = float(value)
    finally:
        _CONTROL_SYNC_GUARD = previous


def _control_value_update(ctrl, context):
    global _CONTROL_SYNC_GUARD

    if _CONTROL_SYNC_GUARD:
        return

    key = ctrl.id_data
    scene = getattr(context, "scene", None)

    previous = _CONTROL_SYNC_GUARD
    _CONTROL_SYNC_GUARD = True
    try:
        # Apply shared bindings only in the current scene.
        for owner_scene, preset in _control_users(key, ctrl.name):
            if scene is not None and owner_scene != scene:
                continue

            preset.value = float(ctrl.value)
            global_preset_apply(preset, context)
    finally:
        _CONTROL_SYNC_GUARD = previous


def _push_value_to_control(preset):
    global _CONTROL_SYNC_GUARD

    if _CONTROL_SYNC_GUARD:
        return

    found = find_preset_control(preset)
    if found is None:
        return

    key, ctrl = found
    if key.library is not None:
        return

    previous = _CONTROL_SYNC_GUARD
    _CONTROL_SYNC_GUARD = True
    try:
        ctrl.value = float(preset.value)
    finally:
        _CONTROL_SYNC_GUARD = previous


def _action_bindings(id_data, include_muted=True):
    """Yield Action owners, including nested NLA strips."""
    anim = getattr(id_data, "animation_data", None)
    if anim is None:
        return

    if anim.action is not None:
        yield anim

    def walk(strips):
        for strip in strips:
            if not include_muted and strip.mute:
                continue

            if getattr(strip, "action", None) is not None:
                yield strip

            yield from walk(getattr(strip, "strips", ()))

    for track in anim.nla_tracks:
        if not include_muted and track.mute:
            continue
        yield from walk(track.strips)


def _action_containers(action, slot):
    if getattr(action, "is_action_layered", False):
        if slot is None:
            return

        for layer in action.layers:
            for strip in layer.strips:
                getter = getattr(strip, "channelbag", None)
                if getter is None:
                    continue

                bag = getter(slot, ensure=False)
                if bag is not None:
                    yield bag
    else:
        if getattr(action, "fcurves", None) is not None:
            yield action


def _all_curves(id_data, include_muted=True, drivers=True):
    seen = set()

    for holder in _action_bindings(id_data, include_muted):
        action = holder.action
        slot = getattr(holder, "action_slot", None)

        binding = (
            action.as_pointer(),
            slot.as_pointer() if slot is not None else 0,
        )
        if binding in seen:
            continue
        seen.add(binding)

        for container in _action_containers(action, slot):
            for fc in container.fcurves:
                yield container.fcurves, fc

    if drivers:
        anim = getattr(id_data, "animation_data", None)
        if anim is not None:
            for fc in anim.drivers:
                yield anim.drivers, fc


def _active_curves(id_data):
    anim = getattr(id_data, "animation_data", None)
    if anim is None or anim.action is None:
        return []

    result = []
    for container in _action_containers(
        anim.action,
        getattr(anim, "action_slot", None),
    ):
        result.extend(container.fcurves)

    return result


_PRESET_FIELDS = (
    "name",
    "uid",
    "control_uid",
    "control_owner",
    "value",
    "items_index",
)

_PRESET_ITEM_FIELDS = (
    "object_ref",
    "object_name",
    "key_name",
    "object_open",
    "max_value",
    "value",
)


def _snapshot_presets(scene):
    return [
        (
            {
                name: getattr(preset, name)
                for name in _PRESET_FIELDS
            },
            [
                {
                    name: getattr(item, name)
                    for name in _PRESET_ITEM_FIELDS
                }
                for item in preset.items
            ],
        )
        for preset in scene.skv_global_presets
    ]


def _restore_presets(scene, snapshot, active_index):
    global _CONTROL_SYNC_GUARD
    global _PRESET_ITEM_SYNC_GUARD
    global _GLOBAL_PRESET_APPLY_GUARD

    previous = (
        _CONTROL_SYNC_GUARD,
        _PRESET_ITEM_SYNC_GUARD,
        _GLOBAL_PRESET_APPLY_GUARD,
    )

    _CONTROL_SYNC_GUARD = True
    _PRESET_ITEM_SYNC_GUARD = True
    _GLOBAL_PRESET_APPLY_GUARD = True

    try:
        scene.skv_global_presets.clear()

        for fields, items in snapshot:
            preset = scene.skv_global_presets.add()

            for name, value in fields.items():
                if name != "items_index":
                    setattr(preset, name, value)

            for values in items:
                item = preset.items.add()
                for name, value in values.items():
                    setattr(item, name, value)

            preset.items_index = fields["items_index"]

        scene.skv_global_preset_index = active_index

    finally:
        (
            _CONTROL_SYNC_GUARD,
            _PRESET_ITEM_SYNC_GUARD,
            _GLOBAL_PRESET_APPLY_GUARD,
        ) = previous


def _restore_controls(key, snapshot):
    global _CONTROL_SYNC_GUARD

    previous = _CONTROL_SYNC_GUARD
    _CONTROL_SYNC_GUARD = True

    try:
        key.skv_preset_controls.clear()

        for name, value in snapshot:
            ctrl = key.skv_preset_controls.add()
            ctrl.name = name
            ctrl.value = value

    finally:
        _CONTROL_SYNC_GUARD = previous


def _restore_action_binding(holder, action, slot):
    holder.action = action

    if action is not None and slot is not None:
        holder.action_slot = slot


def _remove_unused_transaction_action(action):
    """Remove a transaction-created Action without real users."""
    fake_users = int(bool(action.use_fake_user))

    if action.users == fake_users:
        bpy.data.actions.remove(action)

class ControlTransaction:
    """Restore control data and original Action bindings on failure."""

    def __init__(self, scene, keys=()):
        self.scene = scene

        unique = {}
        for key in keys:
            if key is not None:
                unique[key.as_pointer()] = key

        self.keys = list(unique.values())
        self.ids = [scene, *self.keys]
        self.id_pointers = {
            item.as_pointer()
            for item in self.ids
        }

        self.bindings = []
        self.anim_states = []
        self.control_states = []
        self.edits = []
        self.created_actions = []
        self.created_action_pointers = set()
        self.prepared = False

    def __enter__(self):
        global _CONTROL_TRANSACTION

        if _CONTROL_TRANSACTION is not None:
            raise RuntimeError("Nested control transactions are not supported.")

        for id_data in self.ids:
            _check_writable(id_data)

        self.preset_state = _snapshot_presets(self.scene)
        self.preset_index = self.scene.skv_global_preset_index

        for key in self.keys:
            self.control_states.append((
                key,
                [
                    (ctrl.name, float(ctrl.value))
                    for ctrl in key.skv_preset_controls
                ],
            ))

        for id_data in self.ids:
            anim = getattr(id_data, "animation_data", None)

            self.anim_states.append((
                id_data,
                anim is not None,
                anim.action if anim is not None else None,
                getattr(anim, "action_slot", None) if anim else None,
            ))

            if anim is not None:
                for holder in _action_bindings(
                    id_data,
                    include_muted=True,
                ):
                    self.bindings.append((
                        holder,
                        holder.action,
                        getattr(holder, "action_slot", None),
                    ))

        self.prepared = True
        _CONTROL_TRANSACTION = self
        return self

    def track_created_action(self, action):
        if action is None:
            return

        pointer = action.as_pointer()
        if pointer in self.created_action_pointers:
            return

        self.created_action_pointers.add(pointer)
        self.created_actions.append(action)

    def set_attr(self, owner, name, value):
        old_value = getattr(owner, name)
        if old_value == value:
            return

        self.edits.append((owner, name, old_value))
        setattr(owner, name, value)

    def rollback(self):
        errors = []

        def attempt(label, callback):
            try:
                callback()
            except Exception as exc:
                errors.append(f"{label}: {exc}")

        for owner, name, value in reversed(self.edits):
            attempt(
                "Restore driver reference",
                lambda owner=owner, name=name, value=value:
                    setattr(owner, name, value),
            )

        for key, snapshot in self.control_states:
            attempt(
                "Restore controls",
                lambda key=key, snapshot=snapshot:
                    _restore_controls(key, snapshot),
            )

        attempt(
            "Restore presets",
            lambda: _restore_presets(
                self.scene,
                self.preset_state,
                self.preset_index,
            ),
        )

        for holder, action, slot in reversed(self.bindings):
            attempt(
                "Restore Action binding",
                lambda holder=holder, action=action, slot=slot:
                    _restore_action_binding(holder, action, slot),
            )

        for id_data, had_animation, action, slot in self.anim_states:
            def restore_anim(
                id_data=id_data,
                had_animation=had_animation,
                action=action,
                slot=slot,
            ):
                if not had_animation:
                    id_data.animation_data_clear()
                    return

                anim = id_data.animation_data
                if anim is not None:
                    _restore_action_binding(anim, action, slot)

            attempt("Restore animation data", restore_anim)

        for action in reversed(self.created_actions):
            attempt(
                "Remove temporary Action",
                lambda action=action:
                    _remove_unused_transaction_action(action),
            )

        if errors:
            raise RuntimeError(
                "Rollback incomplete: " + "; ".join(errors)
            )

    def __exit__(self, exc_type, exc, traceback):
        global _CONTROL_TRANSACTION

        try:
            if exc_type is not None:
                self.rollback()
        finally:
            _CONTROL_TRANSACTION = None

        return False

def _isolate_actions(id_data, paths=None, active_only=False):
    """Copy only Actions affected by the requested edit."""
    _check_writable(id_data)
    transaction = _CONTROL_TRANSACTION

    if transaction is not None:
        if id_data.as_pointer() not in transaction.id_pointers:
            raise RuntimeError(
                "The datablock is outside the active control transaction."
            )

    anim = getattr(id_data, "animation_data", None)
    if anim is None:
        return

    if paths is not None:
        paths = set(paths)

    holders = list(_action_bindings(id_data, include_muted=True))
    grouped = {}

    for holder in holders:
        action = holder.action
        grouped.setdefault(action.as_pointer(), []).append(holder)

    for group in grouped.values():
        action = group[0].action

        if active_only:
            affected = any(holder == anim for holder in group)
        elif paths is None:
            affected = True
        else:
            affected = any(
                fc.data_path in paths
                for holder in group
                for container in _action_containers(
                    holder.action,
                    getattr(holder, "action_slot", None),
                )
                for fc in container.fcurves
            )

        if not affected:
            continue

        if transaction is not None:
            if action.as_pointer() in transaction.created_action_pointers:
                continue
        elif action.library is None and action.users <= 1:
            continue

        slot_ids = [
            getattr(
                getattr(holder, "action_slot", None),
                "identifier",
                "",
            )
            for holder in group
        ]

        action_copy = action.copy()

        if transaction is not None:
            transaction.track_created_action(action_copy)

        copied_slots = {
            slot.identifier: slot
            for slot in getattr(action_copy, "slots", ())
        }

        if any(
            identifier and identifier not in copied_slots
            for identifier in slot_ids
        ):
            raise RuntimeError("Cannot restore the copied Action slot.")

        # Preserve relationships between this datablock's bindings.
        for holder, identifier in zip(group, slot_ids):
            holder.action = action_copy

            if identifier:
                holder.action_slot = copied_slots[identifier]


def _matching_curves(id_data, paths):
    return [
        fc
        for _collection, fc in _all_curves(id_data)
        if fc.data_path in paths
    ]


def _animated_paths(id_data, regex):
    result = set()

    for _collection, fc in _all_curves(
        id_data,
        include_muted=False,
    ):
        if fc.mute:
            continue

        group = getattr(fc, "group", None)
        if group is not None and group.mute:
            continue

        if regex.match(fc.data_path or ""):
            result.add(fc.data_path)

    return result


def _scene_preset_paths(scene, preset):
    index = next(
        (
            i
            for i, candidate in enumerate(scene.skv_global_presets)
            if candidate.as_pointer() == preset.as_pointer()
        ),
        -1,
    )

    paths = {
        f'skv_global_presets["{_escape(preset.name)}"].value'
    }
    if index >= 0:
        paths.add(f"skv_global_presets[{index}].value")

    return paths


def _reject_nla_and_drivers(id_data, paths):
    """Inspect bindings before deduplicating Action-slot pairs."""
    anim = getattr(id_data, "animation_data", None)
    if anim is None:
        return

    for fc in anim.drivers:
        if fc.data_path in paths:
            raise RuntimeError(
                "The requested property is controlled by a driver."
            )

    for holder in _action_bindings(id_data, include_muted=True):
        # The animation-data binding is the active Action.
        if holder == anim:
            continue

        action = holder.action
        slot = getattr(holder, "action_slot", None)

        for container in _action_containers(action, slot):
            if any(
                fc.data_path in paths
                for fc in container.fcurves
            ):
                raise RuntimeError(
                    "The requested property has NLA animation. "
                    "This operation supports active-Action keyframes only."
                )

def _simple_curve(id_data, paths):
    """Accept one ordinary F-Curve from the active Action only."""
    _reject_nla_and_drivers(id_data, paths)
    curves = _matching_curves(id_data, paths)

    if not curves:
        return None

    active = {
        fc.as_pointer()
        for fc in _active_curves(id_data)
    }

    if len(curves) != 1 or curves[0].as_pointer() not in active:
        raise RuntimeError(
            "Only one curve in the active Action is supported; "
            "NLA and drivers must be handled separately."
        )

    fc = curves[0]

    if len(fc.modifiers):
        raise RuntimeError("The F-Curve has modifiers.")

    if len(fc.sampled_points):
        raise RuntimeError("The F-Curve contains sampled points.")

    if not len(fc.keyframe_points):
        raise RuntimeError("The F-Curve has no keyframe points.")

    return fc


_POINT_ATTRIBUTES = (
    "co",
    "interpolation",
    "easing",
    "handle_left_type",
    "handle_right_type",
    "handle_left",
    "handle_right",
    "back",
    "amplitude",
    "period",
    "type",
)


def _snapshot_curve(fc):
    if fc is None:
        return None

    points = []

    for point in fc.keyframe_points:
        values = {}

        for name in _POINT_ATTRIBUTES:
            value = getattr(point, name)
            if name in {"co", "handle_left", "handle_right"}:
                value = tuple(value)
            values[name] = value

        points.append(values)

    return {
        "points": points,
        "mute": bool(fc.mute),
        "extrapolation": fc.extrapolation,
        "color_mode": fc.color_mode,
        "color": tuple(fc.color),
    }


def _curve_matches_snapshot(fc, snapshot):
    if len(fc.keyframe_points) != len(snapshot["points"]):
        return False

    if (
        bool(fc.mute) != snapshot["mute"]
        or fc.extrapolation != snapshot["extrapolation"]
    ):
        return False

    for point, expected in zip(fc.keyframe_points, snapshot["points"]):
        for name in _POINT_ATTRIBUTES:
            actual = getattr(point, name)
            value = expected[name]

            if isinstance(value, tuple):
                if not all(
                    _math.isclose(a, b, rel_tol=1e-5, abs_tol=1e-5)
                    for a, b in zip(actual, value)
                ):
                    return False
            elif isinstance(value, float):
                if not _math.isclose(
                    actual,
                    value,
                    rel_tol=1e-5,
                    abs_tol=1e-5,
                ):
                    return False
            elif actual != value:
                return False

    return True


def _delete_paths(id_data, paths):
    _check_writable(id_data)
    _isolate_actions(id_data, paths=paths)

    for collection, fc in list(_all_curves(id_data)):
        if fc.data_path in paths:
            collection.remove(fc)


def _write_control_curve(key, ctrl, snapshot):
    if snapshot is None:
        return

    paths = control_data_paths(key, ctrl)

    if _matching_curves(key, paths):
        raise RuntimeError("The destination control already has animation.")

    _check_writable(key)
    _isolate_actions(key, active_only=True)

    path = control_data_path(ctrl)
    first_frame = snapshot["points"][0]["co"][0]

    try:
        anim_before = key.animation_data
        action_before = (
            anim_before.action
            if anim_before is not None
            else None
        )

        try:
            inserted = key.keyframe_insert(
                data_path=path,
                frame=first_frame,
            )
        finally:
            # Track a newly created Action even if insertion raises.
            anim_after = key.animation_data
            action_after = (
                anim_after.action
                if anim_after is not None
                else None
            )

            transaction = _CONTROL_TRANSACTION
            if (
                transaction is not None
                and action_after is not None
                and action_after != action_before
            ):
                transaction.track_created_action(action_after)

        if not inserted:
            raise RuntimeError("Cannot create the destination F-Curve.")

        fc = next(
            (
                curve
                for curve in _active_curves(key)
                if curve.data_path in control_data_paths(key, ctrl)
            ),
            None,
        )

        if fc is None:
            raise RuntimeError("Cannot find the destination F-Curve.")

        # Use the stable control identifier for newly written animation.
        fc.data_path = path
        fc.keyframe_points.clear()
        fc.keyframe_points.add(len(snapshot["points"]))

        for point, values in zip(fc.keyframe_points, snapshot["points"]):
            for name in _POINT_ATTRIBUTES:
                setattr(point, name, values[name])

        fc.mute = snapshot["mute"]
        fc.extrapolation = snapshot["extrapolation"]
        fc.color_mode = snapshot["color_mode"]
        fc.color = snapshot["color"]
        fc.update()

        if not _curve_matches_snapshot(fc, snapshot):
            raise RuntimeError("The copied F-Curve did not pass verification.")

    except Exception:
        _delete_paths(key, control_data_paths(key, ctrl))
        raise


def _driver_owner_ids():
    """Visit datablocks and their embedded node trees once."""
    seen = set()

    def emit(id_data):
        if not isinstance(id_data, bpy.types.ID):
            return

        pointer = id_data.as_pointer()
        if pointer in seen:
            return

        seen.add(pointer)
        yield id_data

        node_tree = getattr(id_data, "node_tree", None)
        if node_tree is not None:
            yield from emit(node_tree)

    for prop in bpy.data.bl_rna.properties:
        if prop.identifier == "rna_type" or prop.type != "COLLECTION":
            continue

        collection = getattr(bpy.data, prop.identifier, None)
        if collection is None:
            continue

        for id_data in collection:
            yield from emit(id_data)


def _reject_owned_driver_deletion(id_data, paths):
    anim = getattr(id_data, "animation_data", None)
    if anim is None:
        return

    if any(fc.data_path in paths for fc in anim.drivers):
        raise RuntimeError(
            "Remove the driver that controls this property "
            "before deleting the control or preset."
        )


def _driver_target_rewrites(target_id, rewrite_path):
    changes = []

    for owner in _driver_owner_ids():
        anim = getattr(owner, "animation_data", None)
        if anim is None:
            continue

        for fc in anim.drivers:
            for variable in fc.driver.variables:
                for target in variable.targets:
                    if getattr(target, "id", None) != target_id:
                        continue

                    old_path = target.data_path
                    new_path = rewrite_path(old_path)

                    if old_path == new_path:
                        continue

                    _check_writable(owner)
                    changes.append((target, "data_path", new_path))

    return changes


def _apply_driver_rewrites(changes):
    transaction = _CONTROL_TRANSACTION
    if transaction is None:
        raise RuntimeError("Driver rewriting requires a control transaction.")

    for owner, name, value in changes:
        transaction.set_attr(owner, name, value)


def _edit_collection_animation(id_data, deleted_paths, rewrite_path):
    """Edit Action copies and journal existing driver-path changes."""
    transaction = _CONTROL_TRANSACTION
    if transaction is None:
        raise RuntimeError("Animation editing requires a control transaction.")

    _reject_owned_driver_deletion(id_data, deleted_paths)

    # Validate external owners before changing their references.
    changes = _driver_target_rewrites(id_data, rewrite_path)

    affected_paths = {
        fc.data_path
        for _collection, fc in _all_curves(id_data, drivers=False)
        if (
                fc.data_path in deleted_paths
                or rewrite_path(fc.data_path) != fc.data_path
        )
    }

    if affected_paths:
        _isolate_actions(id_data, paths=affected_paths)

    for collection, fc in list(_all_curves(id_data, drivers=False)):
        if fc.data_path in deleted_paths:
            collection.remove(fc)
        else:
            new_path = rewrite_path(fc.data_path)
            if new_path != fc.data_path:
                fc.data_path = new_path

    anim = getattr(id_data, "animation_data", None)
    if anim is not None:
        for fc in anim.drivers:
            new_path = rewrite_path(fc.data_path)
            if new_path != fc.data_path:
                transaction.set_attr(fc, "data_path", new_path)

    _apply_driver_rewrites(changes)

def _delete_control(key, ctrl):
    _check_writable(key)

    index = key.skv_preset_controls.find(ctrl.name)
    if index < 0:
        return

    uid = ctrl.name
    names = [
        item.name
        for item in key.skv_preset_controls
    ]
    deleted_paths = control_data_paths(key, ctrl)

    missing_path = (
        f'skv_preset_controls["__removed_{_uuid.uuid4().hex}"].value'
    )

    def rewrite(path):
        if path in deleted_paths:
            return missing_path

        match = _KEY_CONTROL_PATH_RE.match(path or "")
        if match is None or match.group(1) is None:
            return path

        old_index = int(match.group(1))
        if not 0 <= old_index < len(names):
            return path

        # Resolve the old index before removing the collection item.
        name = names[old_index]
        if name == uid:
            return missing_path

        return f'skv_preset_controls["{_escape(name)}"].value'

    _edit_collection_animation(key, deleted_paths, rewrite)
    key.skv_preset_controls.remove(index)


def remove_preset_control(preset):
    found = find_preset_control(preset)

    if found is None:
        return

    key, ctrl = found
    others = [
        other
        for _scene, other in _control_users(key, ctrl.name)
        if other.as_pointer() != preset.as_pointer()
    ]

    # Other presets still own this shared control.
    if others:
        return

    _delete_control(key, ctrl)


def remove_scene_preset_animation(scene, preset):
    _check_writable(scene)

    index = next(
        i
        for i, item in enumerate(scene.skv_global_presets)
        if item.as_pointer() == preset.as_pointer()
    )
    paths = _scene_preset_paths(scene, preset)
    missing_path = (
        f'skv_global_presets["__removed_{_uuid.uuid4().hex}"].value'
    )

    def rewrite(path):
        if path in paths:
            return missing_path

        match = _SCENE_PRESET_PATH_RE.match(path or "")
        if match is None or match.group(1) is None:
            return path

        old_index = int(match.group(1))

        if old_index > index:
            return f"skv_global_presets[{old_index - 1}].value"

        return path

    _edit_collection_animation(scene, paths, rewrite)


def _animation_snapshot(scene, preset):
    found = find_preset_control(preset)
    scene_paths = _scene_preset_paths(scene, preset)
    scene_curves = _matching_curves(scene, scene_paths)

    if found is not None:
        key, ctrl = found
        control_paths = control_data_paths(key, ctrl)
        control_curves = _matching_curves(key, control_paths)

        if control_curves and scene_curves:
            raise RuntimeError(
                "Both the scene preset and its control have animation."
            )

        if control_curves:
            return float(ctrl.value), _snapshot_curve(
                _simple_curve(key, control_paths)
            )

        if not scene_curves:
            return float(ctrl.value), None

    elif preset.control_uid:
        raise RuntimeError("The original control is missing.")

    return float(preset.value), _snapshot_curve(
        _simple_curve(scene, scene_paths)
    )

def _retarget_control_readers(
    old_key,
    old_paths,
    new_key,
    new_path,
):
    """Retarget driver variables and journal both reference fields."""
    transaction = _CONTROL_TRANSACTION
    if transaction is None:
        raise RuntimeError(
            "Control retargeting requires a control transaction."
        )

    changes = []

    for owner in _driver_owner_ids():
        anim = getattr(owner, "animation_data", None)
        if anim is None:
            continue

        for fc in anim.drivers:
            for variable in fc.driver.variables:
                for target in variable.targets:
                    if getattr(target, "id", None) != old_key:
                        continue

                    if target.data_path not in old_paths:
                        continue

                    _check_writable(owner)
                    changes.append(target)

    # Validate all owners before changing any reference.
    for target in changes:
        transaction.set_attr(target, "id", new_key)
        transaction.set_attr(target, "data_path", new_path)

def relocate_preset_control(scene, preset, target_key, independent=False):
    _check_writable(target_key)

    found = find_preset_control(preset)
    if found is None:
        raise RuntimeError("The original control is missing.")

    old_key, old_ctrl = found
    old_uid = old_ctrl.name
    old_paths = set(control_data_paths(old_key, old_ctrl))

    if old_key == target_key and not independent:
        raise RuntimeError("The selected datablock already owns the control.")

    value, snapshot = _animation_snapshot(scene, preset)

    if _matching_curves(scene, _scene_preset_paths(scene, preset)):
        raise RuntimeError("Migrate scene animation before moving the control.")

    other_users = [
        item
        for _owner_scene, item in _control_users(old_key, old_uid)
        if item.as_pointer() != preset.as_pointer()
    ]

    keys = [target_key]
    if not other_users:
        _check_writable(old_key)
        keys.append(old_key)

    with ControlTransaction(scene, keys):
        new_ctrl = _create_control(target_key, value)
        new_uid = new_ctrl.name
        new_path = control_data_path(new_ctrl)

        _write_control_curve(target_key, new_ctrl, snapshot)

        preset.control_owner = target_key
        preset.control_uid = new_uid
        _set_preset_value_without_apply(preset, value)

        if not other_users:
            _retarget_control_readers(
                old_key,
                old_paths,
                target_key,
                new_path,
            )

            # Resolve again because adding a collection item can invalidate
            # previously acquired RNA wrappers.
            old_ctrl = old_key.skv_preset_controls.get(old_uid)
            if old_ctrl is None:
                raise RuntimeError("The original control disappeared.")

            _delete_control(old_key, old_ctrl)

def restore_missing_control(preset, key):
    if find_preset_control(preset) is not None:
        raise RuntimeError("The preset already has a valid control.")

    ctrl = _create_control(key, preset.value)
    ensure_preset_uid(preset)
    preset.control_owner = key
    preset.control_uid = ctrl.name


def migrate_scene_preset_animation(scene):
    moved = 0
    skipped = []

    # Reacquire collection items after any rollback.
    count = len(scene.skv_global_presets)

    for index in range(count):
        preset = scene.skv_global_presets[index]
        preset_name = preset.name
        paths = _scene_preset_paths(scene, preset)

        if not _matching_curves(scene, paths):
            continue

        try:
            snapshot = _snapshot_curve(_simple_curve(scene, paths))
            found = find_preset_control(preset)

            if found is not None:
                key, ctrl = found

                if _matching_curves(key, control_data_paths(key, ctrl)):
                    raise RuntimeError("The control already has animation.")

            else:
                if preset.control_uid:
                    raise RuntimeError("The original control is missing.")

                key = next(
                    (
                        candidate
                        for candidate in _preset_keys(preset)
                        if candidate.library is None
                    ),
                    None,
                )

                if key is None:
                    raise RuntimeError("No writable control owner is available.")

            with ControlTransaction(scene, [key]):
                found = find_preset_control(preset)

                if found is None:
                    ctrl = _create_control(key, preset.value)
                    preset.control_owner = key
                    preset.control_uid = ctrl.name
                else:
                    key, ctrl = found

                _write_control_curve(key, ctrl, snapshot)
                _delete_paths(scene, paths)

            moved += 1

        except Exception as exc:
            skipped.append(f"{preset_name}: {exc}")

    return moved, skipped


def _create_transferred_preset_impl(
    scene,
    source_preset,
    target,
    key_names,
    maxima,
    animation,
):
    key = get_shape_key_data(target)
    _check_writable(key)

    value, snapshot = animation
    result = scene.skv_global_presets.add()
    result.name = _make_unique_preset_name(
        scene,
        f"{source_preset.name} ({target.name})",
    )
    ensure_preset_uid(result)

    try:
        for name in key_names:
            if not add_shape_key_to_preset(result, target, name):
                raise RuntimeError(f"Cannot add target shape key: {name}")

            result.items[-1].max_value = maxima[name]

        found = find_preset_control(result)
        if found is None:
            raise RuntimeError("Cannot create the target preset control.")

        owner, ctrl = found

        global _CONTROL_SYNC_GUARD
        previous = _CONTROL_SYNC_GUARD
        _CONTROL_SYNC_GUARD = True
        try:
            result.value = value
            ctrl.value = value
        finally:
            _CONTROL_SYNC_GUARD = previous

        _write_control_curve(owner, ctrl, snapshot)
        return result
    except Exception:
        raise

def create_transferred_preset(
    scene,
    source_preset,
    target,
    key_names,
    maxima,
    animation,
):
    key = get_shape_key_data(target)
    _check_writable(key)

    with ControlTransaction(scene, [key]):
        return _create_transferred_preset_impl(
            scene,
            source_preset,
            target,
            key_names,
            maxima,
            animation,
        )

def validate_target_preset_animation(target, names):
    key = get_shape_key_data(target)
    if key is None:
        return

    paths = {
        _shape_key_value_data_path(name)
        for name in names
    }

    if _matching_curves(key, paths):
        raise RuntimeError(
            "Target shape keys already have animation or drivers. "
            "A copied preset controller would conflict with them."
        )


def apply_animated_presets(scene):
    global _CONTROL_FRAME_GUARD

    if _CONTROL_FRAME_GUARD or scene is None:
        return

    _CONTROL_FRAME_GUARD = True
    try:
        scene_paths = _animated_paths(scene, _SCENE_PRESET_PATH_RE)
        key_paths = {}

        # List order defines priority: the last applied preset wins.
        for preset in getattr(scene, "skv_global_presets", ()):
            found = find_preset_control(preset)

            if found is not None:
                key, ctrl = found
                pointer = key.as_pointer()

                if pointer not in key_paths:
                    key_paths[pointer] = _animated_paths(
                        key,
                        _KEY_CONTROL_PATH_RE,
                    )

                if control_data_paths(key, ctrl) & key_paths[pointer]:
                    _set_preset_value_without_apply(
                        preset,
                        ctrl.value,
                    )
                    global_preset_apply(preset, bpy.context)
                    continue

            if _scene_preset_paths(scene, preset) & scene_paths:
                global_preset_apply(preset, bpy.context)

    finally:
        _CONTROL_FRAME_GUARD = False


def preset_overlap_names(scene, preset):
    own = {
        (data.as_pointer(), kb.name)
        for _item, data, kb in _iter_resolved_preset_items(preset)
    }
    result = []

    for other in scene.skv_global_presets:
        if other.as_pointer() == preset.as_pointer():
            continue

        keys = {
            (data.as_pointer(), kb.name)
            for _item, data, kb in _iter_resolved_preset_items(other)
        }

        if own & keys:
            result.append(other.name)

    return result

class SKV_OT_ControlMigrate(Operator):
    bl_idname = "skv.control_migrate"
    bl_label = "Migrate Preset Animation"
    bl_description = (
        "Move supported preset animation from the scene "
        "to shape key controls"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        moved, skipped = migrate_scene_preset_animation(context.scene)

        for message in skipped:
            print("MorphDeck:", message)

        self.report(
            {"WARNING"} if skipped else {"INFO"},
            f"Migrated: {moved}; skipped: {len(skipped)}",
        )

        if skipped:
            self.report({"WARNING"}, skipped[0])

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ControlMove(Operator):
    bl_idname = "skv.control_move"
    bl_label = "Move Preset Control"
    bl_description = (
        "Move the preset control and supported active-Action animation "
        "to another object's shape keys"
    )
    bl_options = {"REGISTER", "UNDO"}

    target: PointerProperty(
        name="Target",
        type=bpy.types.Object,
        poll=lambda self, obj: (
            obj.type == "MESH"
            and get_shape_key_data(obj) is not None
        ),
    )

    def invoke(self, context, event):
        self.target = context.active_object
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        self.layout.prop(self, "target")

    def execute(self, context):
        preset = get_active_global_preset(context.scene)
        key = get_shape_key_data(self.target)

        if preset is None or key is None:
            self.report({"ERROR"}, "Choose an object with shape keys.")
            return {"CANCELLED"}

        try:
            relocate_preset_control(context.scene, preset, key)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            # Preserve an undo step if Action isolation already occurred.
            return {"FINISHED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ControlIndependent(Operator):
    bl_idname = "skv.control_independent"
    bl_label = "Make Preset Control Independent"
    bl_description = (
        "Create a separate control and copy supported animation "
        "for this preset"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        preset = get_active_global_preset(context.scene)
        found = find_preset_control(preset) if preset else None

        if found is None:
            self.report({"ERROR"}, "The control is missing.")
            return {"CANCELLED"}

        try:
            relocate_preset_control(
                context.scene,
                preset,
                found[0],
                independent=True,
            )
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"FINISHED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ControlRestore(Operator):
    bl_idname = "skv.control_restore"
    bl_label = "Create Preset Control"
    bl_description = (
        "Create a control on the active object's shape keys; "
        "lost animation is not recovered"
    )
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        self.layout.label(
            text="Create a control on the active object?"
        )
        self.layout.label(
            text="This does not recover deleted animation.",
            icon="INFO",
        )

    def execute(self, context):
        preset = get_active_global_preset(context.scene)
        key = get_shape_key_data(context.active_object)

        if preset is None or key is None:
            self.report({"ERROR"}, "Select an object with shape keys.")
            return {"CANCELLED"}

        try:
            restore_missing_control(preset, key)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"FINISHED"}

        tag_redraw_view3d(context)
        return {"FINISHED"}


CLASSES += (
    SKV_OT_ControlMigrate,
    SKV_OT_ControlMove,
    SKV_OT_ControlIndependent,
    SKV_OT_ControlRestore,
)