import bpy
from bpy.types import (
    Operator,
    PropertyGroup,
    UIList,
    Menu,
)
from bpy.props import (
    BoolProperty,
    IntProperty,
    StringProperty,
    FloatProperty,
)

from .transfer import TransferModalMixin

from .common import (
    INIT_GROUP_NAME,
    get_active_object,
    get_shape_key_data,
    has_group_storage,
    group_names,
    get_fallback_group_name,
    is_initialized,
    get_selected_group_name,
    tag_redraw_view3d,
    clear_selection_ui,
    kd_get_group,
    kd_set_group,
    kd_selected_set,
    kd_is_selected,
    kd_set_selected,
    kd_clear_selected,
    count_keys_in_group,
    ensure_init_setup_write,
    skv_sync_shape_key_list_indices,
    kd_group_map,
    kd_autokf_get_entry as _autokf_get_entry,
    kd_favorite_names,
    skv_list_key_names,
    kd_activate_selection_source,
    skv_shape_key_filter_flags,
    SKV_ROW_SELECTED,
    SKV_ROW_FAVORITE,
    SKV_ROW_AUTO_KEYFRAME,
    kd_visible_selected_names,
    _is_basis_name,
)


def _make_unique_name(base_name: str, existing_names) -> str:
    base = (base_name or "").strip()
    if not base:
        base = "New Group"

    existing = set(existing_names or [])
    if base not in existing:
        return base

    index = 1
    while f"{base} {index}" in existing:
        index += 1
    return f"{base} {index}"


def _make_transfer_group_name(source_group_name: str, source_obj, target_key_data) -> str:
    # Build deterministic target group name for transferred shape keys.
    # Reuse it if it already exists on target.
    base_name = (source_group_name or INIT_GROUP_NAME).strip() or INIT_GROUP_NAME
    source_name = getattr(source_obj, "name", "") if source_obj else ""
    transfer_name = f"{base_name} ({source_name})" if source_name else base_name

    existing = set(group_names(target_key_data))
    if transfer_name in existing:
        return transfer_name

    return _make_unique_name(transfer_name, existing)


def _ensure_group_exists(key_data, group_name: str) -> bool:
    # Ensure that a group with the given name exists in target key data.
    if not key_data or not has_group_storage(key_data):
        return False

    group_name = (group_name or INIT_GROUP_NAME).strip() or INIT_GROUP_NAME

    for g in key_data.skv_groups:
        if g.name == group_name:
            return True

    try:
        g = key_data.skv_groups.add()
        g.name = group_name
        try:
            g.last_name = group_name
        except Exception:
            pass
        return True
    except Exception:
        return False


def inherit_transferred_groups(source_obj, target_obj, key_names) -> int:
    # Copy source group mapping to target for transferred shape keys.
    if not source_obj or not target_obj or source_obj == target_obj:
        return 0
    if getattr(source_obj, "type", None) != "MESH" or getattr(target_obj, "type", None) != "MESH":
        return 0

    source_key_data = get_shape_key_data(source_obj)

    names = [n for n in (key_names or []) if n and not _is_basis_name(source_key_data, n)]
    if not names:
        return 0

    target_key_data = get_shape_key_data(target_obj)

    if not source_key_data or not target_key_data:
        return 0
    if not getattr(source_key_data, "key_blocks", None) or not getattr(target_key_data, "key_blocks", None):
        return 0
    if not has_group_storage(source_key_data) or not has_group_storage(target_key_data):
        return 0
    if getattr(target_key_data, "library", None) is not None:
        return 0

    ensure_init_setup_write(target_obj)

    source_to_target_group = {}
    changed = 0

    for key_name in names:
        if not source_key_data.key_blocks.get(key_name):
            continue
        if not target_key_data.key_blocks.get(key_name):
            continue

        source_group = kd_get_group(source_key_data, key_name)

        if source_group not in source_to_target_group:
            target_group = _make_transfer_group_name(source_group, source_obj, target_key_data)
            if not _ensure_group_exists(target_key_data, target_group):
                continue
            source_to_target_group[source_group] = target_group

        kd_set_group(target_key_data, key_name, source_to_target_group[source_group])
        changed += 1

    ensure_init_setup_write(target_obj)
    _remove_empty_transfer_fallback_group(target_key_data)
    return changed


def _remove_empty_transfer_fallback_group(target_key_data) -> None:
    # Remove the auto-created fallback group if transferred groups already exist
    # and the fallback group has no non-Basis shape keys assigned to it.
    if not target_key_data or not has_group_storage(target_key_data):
        return

    groups = getattr(target_key_data, "skv_groups", None)
    if not groups or len(groups) <= 1:
        return

    fallback_index = -1
    for i, g in enumerate(groups):
        if g.name == INIT_GROUP_NAME:
            fallback_index = i
            break

    if fallback_index < 0:
        return

    for kb in list(getattr(target_key_data, "key_blocks", []) or [])[1:]:
        if kd_get_group(target_key_data, kb.name) == INIT_GROUP_NAME:
            return

    try:
        groups.remove(fallback_index)
    except Exception:
        return

    try:
        if target_key_data.skv_group_index >= len(groups):
            target_key_data.skv_group_index = max(0, len(groups) - 1)
    except Exception:
        pass

    try:
        if hasattr(target_key_data, "skv_last_group_index"):
            target_key_data.skv_last_group_index = int(target_key_data.skv_group_index)
    except Exception:
        pass


# -----------------------------
# Data Model (groups + selection + group mapping)
# -----------------------------

def group_name_update(self, context):
    # Keep shape key -> group mapping synchronized after inline group rename.
    key_data = getattr(self, "id_data", None)
    if not key_data or not has_group_storage(key_data):
        return

    old_name = (getattr(self, "last_name", "") or "").strip()
    new_name = (getattr(self, "name", "") or "").strip()

    if not new_name:
        try:
            self.name = old_name or "Group"
        except Exception:
            pass
        return

    if not old_name:
        try:
            self.last_name = new_name
        except Exception:
            pass
        return

    if old_name == new_name:
        return

    # Prevent duplicate group names on inline rename.
    try:
        duplicate = False
        for g in key_data.skv_groups:
            if g == self:
                continue
            if g.name == new_name:
                duplicate = True
                break

        if duplicate:
            self.name = old_name
            return
    except Exception:
        pass


    if getattr(key_data, "library", None) is not None:
        try:
            self.name = old_name
        except Exception:
            pass
        return

    try:
        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == old_name:
                kd_set_group(key_data, kb.name, new_name)
    except Exception:
        pass

    try:
        self.last_name = new_name
    except Exception:
        pass

    tag_redraw_view3d(context)

class SKV_Group(PropertyGroup):
    name: StringProperty(name="Name", default="Group", update=group_name_update)
    last_name: StringProperty(name="Last Name", default="", options={"SKIP_SAVE"})


class SKV_SelectedName(PropertyGroup):
    name: StringProperty(name="Name", default="")


class SKV_KeyGroupEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    group: StringProperty(name="Group", default=INIT_GROUP_NAME)

class SKV_AutoKeyframeEntry(PropertyGroup):
    name: StringProperty(name="Key Name", default="")
    enabled: BoolProperty(name="Enabled", default=False)
    last_frame: IntProperty(name="Last Frame", default=-999999)
    last_value: FloatProperty(name="Last Value", default=0.0)

def group_index_update(self, context):
    # Clear selected shape keys only when the active group index actually changes.
    key_data = self
    if not key_data or not hasattr(key_data, "skv_selected"):
        return

    current_index = int(getattr(key_data, "skv_group_index", 0))
    previous_index = int(getattr(key_data, "skv_last_group_index", -1))

    if current_index == previous_index:
        return

    try:
        key_data.skv_last_group_index = current_index
    except Exception:
        pass

    # Group navigation must preserve selection owned by Favorites.
    if getattr(key_data, "skv_selection_source", "GROUP") == "GROUP":
        kd_clear_selected(key_data)

    tag_redraw_view3d(context)

def _shape_key_value_data_path(key_name: str) -> str:
    # Build a valid RNA path for a shape key value FCurve.
    try:
        escaped = bpy.utils.escape_identifier(key_name)
    except Exception:
        escaped = (key_name or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'key_blocks["{escaped}"].value'


def _action_get_slot_for_datablock(action, datablock, create: bool = False):
    # Return the action slot assigned to the datablock animation data.
    if not action or not datablock:
        return None

    anim_data = getattr(datablock, "animation_data", None)
    slot = getattr(anim_data, "action_slot", None) if anim_data else None
    if slot:
        return slot

    slots = getattr(action, "slots", None)
    if slots is None:
        return None

    if len(slots) > 0:
        return slots[0]

    if not create:
        return None

    try:
        name = getattr(datablock, "name", "") or "ShapeKeys"
        slot = action.slots.new(id_type="KEY", name=name)
    except Exception:
        return None

    try:
        if anim_data:
            anim_data.action_slot = slot
    except Exception:
        pass

    return slot


def _action_get_channelbag(action, datablock, create: bool = False):
    # Return an ActionChannelbag for Blender 5.0+ slotted actions.
    if not action or not datablock:
        return None

    slot = _action_get_slot_for_datablock(action, datablock, create=create)
    if not slot:
        return None

    layers = getattr(action, "layers", None)
    if layers is None:
        return None

    if len(layers) > 0:
        layer = layers[0]
    elif create:
        try:
            layer = layers.new("Layer")
        except Exception:
            return None
    else:
        return None

    strips = getattr(layer, "strips", None)
    if strips is None:
        return None

    if len(strips) > 0:
        strip = strips[0]
    elif create:
        try:
            strip = strips.new(type="KEYFRAME")
        except Exception:
            return None
    else:
        return None

    try:
        return strip.channelbag(slot, ensure=create)
    except Exception:
        return None


def _iter_action_fcurves(action, datablock):
    # Yield FCurves from both legacy actions and Blender 5.0+ channelbags.
    if not action:
        return

    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        for fc in legacy_fcurves:
            yield fc
        return

    channelbag = _action_get_channelbag(action, datablock, create=False)
    if not channelbag:
        return

    for fc in getattr(channelbag, "fcurves", []) or []:
        yield fc


def _action_fcurves_find(action, datablock, data_path: str, index: int = 0):
    # Find FCurve in both legacy and Blender 5.0+ action APIs.
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        try:
            return legacy_fcurves.find(data_path, index=index)
        except Exception:
            return None

    channelbag = _action_get_channelbag(action, datablock, create=False)
    if not channelbag:
        return None

    try:
        return channelbag.fcurves.find(data_path, index=index)
    except Exception:
        return None


def _action_fcurves_remove(action, datablock, fcurve) -> None:
    # Remove FCurve from both legacy and Blender 5.0+ action APIs.
    if not action or not fcurve:
        return

    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        try:
            legacy_fcurves.remove(fcurve)
        except Exception:
            pass
        return

    channelbag = _action_get_channelbag(action, datablock, create=False)
    if not channelbag:
        return

    try:
        channelbag.fcurves.remove(fcurve)
    except Exception:
        pass


def _action_fcurves_new(action, datablock, data_path: str, index: int = 0, group_name: str = ""):
    # Create FCurve in both legacy and Blender 5.0+ action APIs.
    legacy_fcurves = getattr(action, "fcurves", None)
    if legacy_fcurves is not None:
        try:
            if group_name:
                return legacy_fcurves.new(data_path=data_path, index=index, action_group=group_name)
            return legacy_fcurves.new(data_path=data_path, index=index)
        except Exception:
            return None

    channelbag = _action_get_channelbag(action, datablock, create=True)
    if not channelbag:
        return None

    try:
        if group_name:
            return channelbag.fcurves.new(data_path=data_path, index=index, group_name=group_name)
        return channelbag.fcurves.new(data_path=data_path, index=index)
    except TypeError:
        try:
            if group_name:
                return channelbag.fcurves.new(data_path=data_path, index=index, action_group=group_name)
            return channelbag.fcurves.new(data_path=data_path, index=index)
        except Exception:
            return None
    except Exception:
        return None


def _activate_target_if_available(context, target):
    view_layer = getattr(context, "view_layer", None)
    if not view_layer or not target:
        return False

    layer_target = view_layer.objects.get(target.name)
    if layer_target != target:
        return False

    try:
        for obj in view_layer.objects:
            if obj.select_get():
                obj.select_set(False)

        target.select_set(True)
        view_layer.objects.active = target
        return True
    except (RuntimeError, TypeError):
        return False


def _copy_keyframe_point(dst_kp, src_kp) -> None:
    # Copy keyframe point settings.
    try:
        dst_kp.co = src_kp.co
    except Exception:
        pass

    try:
        dst_kp.handle_left = src_kp.handle_left
        dst_kp.handle_right = src_kp.handle_right
    except Exception:
        pass

    for attr in (
        "interpolation",
        "easing",
        "amplitude",
        "back",
        "period",
        "handle_left_type",
        "handle_right_type",
    ):
        try:
            setattr(dst_kp, attr, getattr(src_kp, attr))
        except Exception:
            pass


def _copy_fcurve_keyframes(src_fc, dst_action, dst_datablock, dst_data_path: str) -> bool:
    # Replace target FCurve keyframes with source FCurve keyframes.
    if not src_fc or not dst_action or not dst_datablock or not dst_data_path:
        return False

    try:
        existing = _action_fcurves_find(
            dst_action,
            dst_datablock,
            dst_data_path,
            index=int(getattr(src_fc, "array_index", 0)),
        )
        if existing is not None:
            return False
    except Exception:
        pass

    group_name = ""
    try:
        group_name = src_fc.group.name if src_fc.group else ""
    except Exception:
        group_name = ""

    dst_fc = _action_fcurves_new(
        dst_action,
        dst_datablock,
        dst_data_path,
        index=int(getattr(src_fc, "array_index", 0)),
        group_name=group_name,
    )
    if not dst_fc:
        return False

    try:
        dst_fc.extrapolation = src_fc.extrapolation
    except Exception:
        pass

    try:
        dst_fc.color_mode = src_fc.color_mode
        dst_fc.color = src_fc.color
    except Exception:
        pass

    src_points = list(getattr(src_fc, "keyframe_points", []) or [])
    if not src_points:
        return True

    try:
        dst_fc.keyframe_points.add(len(src_points))
    except Exception:
        return False

    try:
        for dst_kp, src_kp in zip(dst_fc.keyframe_points, src_points):
            _copy_keyframe_point(dst_kp, src_kp)
        dst_fc.update()
    except Exception:
        return False

    return True


def inherit_transferred_keyframes(source_obj, target_obj, key_names) -> int:
    # Copy shape key value keyframes from source to target for transferred keys.
    if not source_obj or not target_obj or source_obj == target_obj:
        return 0
    if getattr(source_obj, "type", None) != "MESH" or getattr(target_obj, "type", None) != "MESH":
        return 0

    names = [n for n in (key_names or []) if n]
    if not names:
        return 0

    source_key_data = get_shape_key_data(source_obj)
    target_key_data = get_shape_key_data(target_obj)

    if not source_key_data or not target_key_data:
        return 0
    if not getattr(source_key_data, "key_blocks", None) or not getattr(target_key_data, "key_blocks", None):
        return 0

    source_anim = getattr(source_key_data, "animation_data", None)
    source_action = getattr(source_anim, "action", None) if source_anim else None
    if not source_action:
        return 0

    try:
        target_key_data.animation_data_create()
    except Exception:
        return 0

    target_anim = getattr(target_key_data, "animation_data", None)
    if not target_anim:
        return 0

    if not target_anim.action:
        try:
            target_anim.action = bpy.data.actions.new(name=f"{target_obj.name}_ShapeKeysAction")
        except Exception:
            return 0

    target_action = target_anim.action
    copied = 0

    for key_name in names:
        if not source_key_data.key_blocks.get(key_name):
            continue
        if not target_key_data.key_blocks.get(key_name):
            continue

        src_path = _shape_key_value_data_path(key_name)
        dst_path = _shape_key_value_data_path(key_name)

        matched = False
        for src_fc in _iter_action_fcurves(source_action, source_key_data):
            if src_fc.data_path != src_path:
                continue

            if _copy_fcurve_keyframes(src_fc, target_action, target_key_data, dst_path):
                matched = True

        if matched:
            copied += 1

    return copied

def _group_shape_keys(key_data, group_name):
    if not key_data or not key_data.key_blocks:
        return []

    # Build the name -> group map once: kd_get_group() does a linear
    # collection lookup per call, which made this O(n^2) per UI row.
    mapping = kd_group_map(key_data)
    fallback = get_fallback_group_name(key_data)

    return [
        kb
        for kb in list(key_data.key_blocks)[1:]
        if mapping.get(kb.name, fallback) == group_name
    ]


class SKV_OT_GroupToggleVisibility(Operator):
    bl_idname = "skv.group_toggle_visibility"
    bl_label = "Show / Hide Group"
    bl_description = (
        "Show or hide all shape keys in this group "
        "without changing their values"
    )
    bl_options = {"REGISTER", "UNDO"}

    group_name: StringProperty(name="Group", default="")

    def execute(self, context):
        props = getattr(context.scene, "skv_props", None)
        obj = getattr(props, "object_pick", None)
        key_data = get_shape_key_data(obj)

        if (
            not key_data
            or not key_data.key_blocks
            or key_data.library is not None
            or not is_initialized(key_data)
        ):
            return {"CANCELLED"}

        if not any(
            group.name == self.group_name
            for group in key_data.skv_groups
        ):
            return {"CANCELLED"}

        keys = _group_shape_keys(key_data, self.group_name)

        if not keys:
            return {"CANCELLED"}

        # Hide a visible or partially hidden group; show a fully hidden group.
        hide = not all(kb.mute for kb in keys)

        for kb in keys:
            if kb.mute != hide:
                kb.mute = hide

        tag_redraw_view3d(context)
        return {"FINISHED"}

# -----------------------------
# UI Lists
# -----------------------------
class SKV_UL_Groups(UIList):
    bl_idname = "SKV_UL_groups"

    def draw_item(
        self,
        context,
        layout,
        data,
        item,
        icon,
        active_data,
        active_propname,
        index,
    ):
        group = item
        key_data = data

        # Writing ID data from draw() is not allowed in Blender (it always
        # raised and was silently swallowed), so last_name is no longer set here.
        keys = _group_shape_keys(key_data, group.name)
        hidden = bool(keys) and all(kb.mute for kb in keys)

        row = layout.row(align=True)
        row.prop(
            group,
            "name",
            text="",
            emboss=False,
            icon="FILE_FOLDER",
        )
        row.label(text=str(len(keys)))

        visibility = row.row(align=True)
        visibility.enabled = (
            bool(keys)
            and key_data.library is None
        )

        operator = visibility.operator(
            "skv.group_toggle_visibility",
            text="",
            icon="HIDE_ON" if hidden else "HIDE_OFF",
            emboss=False,
        )
        operator.group_name = group.name


class SKV_UL_key_blocks(UIList):
    bl_idname = "SKV_UL_key_blocks"

    def filter_items(self, context, data, propname):
        return skv_shape_key_filter_flags(
            self,
            context,
            data,
            propname,
        )

    def draw_item(
            self, context, layout, data, item, icon,
            active_data, active_propname, index, flt_flag=0,
    ):
        kb = item
        row = layout.row(align=True)

        source = {
            "SKV_UL_favorites": "FAVORITES",
            "SKV_UL_keyframed_keys": "KEYFRAMED",
        }.get(self.bl_idname, "GROUP")
        selected = bool(flt_flag & SKV_ROW_SELECTED)
        icon_id = "CHECKBOX_HLT" if selected else "CHECKBOX_DEHLT"

        op = row.operator(
            "skv.key_toggle_select",
            text="",
            icon=icon_id,
            emboss=False,
        )
        op.key_index = index
        op.source = source

        row.prop(kb, "name", text="", emboss=False)
        row.prop(kb, "value", text="", slider=True)

        vis_icon = "HIDE_ON" if kb.mute else "HIDE_OFF"
        opv = row.operator("skv.shape_key_toggle_visibility", text="", icon=vis_icon, emboss=False)
        opv.key_name = kb.name

        favorite = bool(flt_flag & SKV_ROW_FAVORITE)

        opf = row.operator(
            "skv.favorite_toggle",
            text="",
            icon="SOLO_ON" if favorite else "SOLO_OFF",
            emboss=False,
        )
        opf.key_name = kb.name

        kf_enabled = bool(
            flt_flag & SKV_ROW_AUTO_KEYFRAME
        )
        opk = row.operator(
            "skv.auto_keyframe_toggle",
            text="",
            icon="KEYINGSET",
            emboss=True,
            depress=kf_enabled,
        )
        opk.key_name = kb.name

class SKV_UL_favorites(UIList):
    bl_idname = "SKV_UL_favorites"

    def filter_items(self, context, data, propname):
        return skv_shape_key_filter_flags(
            self,
            context,
            data,
            propname,
        )

    def draw_item(
        self, context, layout, data, item, icon,
        active_data, active_propname, index, flt_flag=0,
    ):
        SKV_UL_key_blocks.draw_item(
            self,
            context,
            layout,
            data,
            item,
            icon,
            active_data,
            active_propname,
            index,
            flt_flag,
        )


# -----------------------------
# Menus
# -----------------------------
class SKV_MT_MoveToGroup(Menu):
    bl_label = "Move to group"
    bl_idname = "SKV_MT_move_to_group"

    def draw(self, context):
        layout = self.layout
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not key_data or not has_group_storage(key_data) or not key_data.skv_groups:
            layout.label(text="No groups")
            return

        current_group = get_selected_group_name(key_data)

        has_target_groups = False
        for g in key_data.skv_groups:
            if (
                    key_data.skv_selection_source == "GROUP"
                    and g.name == current_group
            ):
                continue
            has_target_groups = True
            op = layout.operator("skv.move_selected_to_group", text=g.name, icon="FILE_FOLDER")
            op.group = g.name

        if not has_target_groups:
            layout.label(text="No other groups")


# -----------------------------
# Operators (required by CLASSES)
# -----------------------------

class SKV_OT_ShapeKeyToggleVisibility(Operator):
    bl_idname = "skv.shape_key_toggle_visibility"
    bl_label = "Toggle Shape Key Visibility"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(self.key_name)
        if not kb:
            return {"CANCELLED"}

        kb.mute = not kb.mute
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_AutoKeyframeToggle(Operator):
    bl_idname = "skv.auto_keyframe_toggle"
    bl_label = "Toggle Auto Keyframe"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not getattr(key_data, "key_blocks", None):
            return {"CANCELLED"}
        if getattr(key_data, "library", None) is not None:
            return {"CANCELLED"}

        name = (self.key_name or "").strip()
        if not name:
            return {"CANCELLED"}

        kb = key_data.key_blocks.get(name)
        if not kb:
            return {"CANCELLED"}

        entry = _autokf_get_entry(key_data, name, create=True)
        entry.enabled = not bool(entry.enabled)

        frame = int(context.scene.frame_current)
        entry.last_frame = frame
        try:
            entry.last_value = float(kb.value)
        except Exception:
            entry.last_value = 0.0

        if entry.enabled:
            try:
                key_data.keyframe_insert(data_path=_shape_key_value_data_path(name), frame=frame)
            except Exception:
                pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_KeyToggleSelect(Operator):
    bl_idname = "skv.key_toggle_select"
    bl_label = "Toggle Select"
    bl_options = {"REGISTER", "UNDO"}

    key_index: IntProperty(name="Key Index", default=-1)
    source: StringProperty(name="List Source", default="GROUP")

    def execute(self, context):
        data = get_shape_key_data(get_active_object(context))

        if not data or not data.key_blocks or data.library is not None:
            return {"CANCELLED"}

        if self.source not in {
            "GROUP",
            "FAVORITES",
            "KEYFRAMED",
        }:
            return {"CANCELLED"}

        if not 0 <= self.key_index < len(data.key_blocks):
            return {"CANCELLED"}

        kb = data.key_blocks[self.key_index]
        names = skv_list_key_names(
            data,
            context.scene.skv_props,
            self.source,
        )
        if kb.name not in names:
            return {"CANCELLED"}

        # Clear the other list before toggling the requested key.
        kd_activate_selection_source(data, self.source)
        kd_set_selected(
            data,
            kb.name,
            not kd_is_selected(data, kb.name),
        )

        tag_redraw_view3d(context)
        return {"FINISHED"}


def _select_visible_from_list(operator, context, source):
    data = get_shape_key_data(get_active_object(context))

    if not data or not data.key_blocks or data.library is not None:
        return {"CANCELLED"}

    if not is_initialized(data):
        return {"CANCELLED"}

    if operator.mode not in {"ALL", "CLEAR", "INVERT", "NONZERO"}:
        return {"CANCELLED"}

    names = skv_list_key_names(
        data,
        context.scene.skv_props,
        source,
    )

    if operator.mode == "CLEAR":
        # Deselect only entries owned by this list.
        if data.skv_selection_source == source:
            for name in names:
                kd_set_selected(data, name, False)

    elif names:
        # Activating this list clears selection in the other list.
        kd_activate_selection_source(data, source)

        for name in names:
            if operator.mode == "ALL":
                state = True
            elif operator.mode == "NONZERO":
                state = float(data.key_blocks[name].value) != 0.0
            else:
                state = not kd_is_selected(data, name)

            kd_set_selected(data, name, state)

    tag_redraw_view3d(context)
    return {"FINISHED"}


class SKV_OT_SelectVisible(Operator):
    bl_idname = "skv.select_visible"
    bl_label = "Select Group Keys"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(name="Mode", default="ALL")

    def execute(self, context):
        return _select_visible_from_list(self, context, "GROUP")


class SKV_OT_FavoritesSelectVisible(Operator):
    bl_idname = "skv.favorites_select_visible"
    bl_label = "Select Favorite Keys"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(name="Mode", default="ALL")

    def execute(self, context):
        return _select_visible_from_list(self, context, "FAVORITES")


class SKV_OT_MoveSelectedToGroup(Operator):
    bl_idname = "skv.move_selected_to_group"
    bl_label = "Move Selected To Group"
    bl_options = {"REGISTER", "UNDO"}

    group: StringProperty(name="Group", default=INIT_GROUP_NAME)

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data:
            return {"CANCELLED"}

        group_name = (self.group or INIT_GROUP_NAME).strip() or INIT_GROUP_NAME
        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )
        for n in selected:
            if n and not _is_basis_name(key_data, n):
                kd_set_group(key_data, n, group_name)

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_ResetGroupValues(Operator):
    bl_idname = "skv.reset_group_values"
    bl_label = "Zero Values (Selected)"
    bl_description = "Set value=0 only for selected shape keys in the current group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        if key_data.library is not None:
            self.report(
                {"ERROR"},
                "Shape key datablock is linked (read-only).",
            )
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )
        group_name = get_selected_group_name(key_data)
        mapping = kd_group_map(key_data)
        fallback = get_fallback_group_name(key_data)
        basis_name = key_data.key_blocks[0].name

        changed = 0
        for kb in key_data.key_blocks:
            if kb.name == basis_name or kb.name not in selected:
                continue
            if (
                    key_data.skv_selection_source == "GROUP"
                    and mapping.get(kb.name, fallback) != group_name
            ):
                continue

            try:
                if float(kb.value) != 0.0:
                    kb.value = 0.0
                changed += 1
            except RuntimeError:
                continue

        if changed == 0:
            self.report({"INFO"}, "No selected keys were reset.")
            return {"CANCELLED"}

        kd_clear_selected(key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_DeleteSelectedShapeKeys(Operator):
    bl_idname = "skv.delete_selected_shape_keys"
    bl_label = "Delete selected"
    bl_description = "Delete selected shape keys in the current group"
    bl_options = {"REGISTER", "UNDO"}
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=340)
    def draw(self, context):
        self.layout.label(text="Delete selected Shape Keys?", icon="QUESTION")
    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        if not obj or not key_data or not key_data.key_blocks:
            return {"CANCELLED"}

        if obj.data.library is not None or key_data.library is not None:
            self.report({"ERROR"}, "Target data is linked (read-only).")
            return {"CANCELLED"}

        group_name = get_selected_group_name(key_data)
        source = key_data.skv_selection_source
        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )

        basis_name = key_data.key_blocks[0].name

        names = [
            kb.name
            for kb in key_data.key_blocks
            if kb.name != basis_name
               and kb.name in selected
               and (
                       source != "GROUP"
                       or kd_get_group(key_data, kb.name) == group_name
               )
        ]
        if not names:
            self.report({"INFO"}, "No selected shape keys in this list.")
            return {"CANCELLED"}

        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except RuntimeError as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}

        active_name = obj.active_shape_key.name if obj.active_shape_key else ""
        deleted = set()
        for name in reversed(names):
                kb = key_data.key_blocks.get(name)
                if kb is None:
                    continue
                try:
                    obj.shape_key_remove(kb)
                except RuntimeError as error:
                    self.report({"WARNING"}, f"Cannot delete {name}: {error}")
                    continue
                deleted.add(name)

        if not deleted:
            return {"CANCELLED"}

        # Remove references only for successfully deleted keys.
        for attr in (
            "skv_key_groups",
            "skv_selected",
            "skv_auto_keyframes",
            "skv_favorites",
            "skv_key_defaults",
            "skv_active_keys",
        ):
            collection = getattr(key_data, attr, None)
            if collection is None:
                continue
            for index in range(len(collection) - 1, -1, -1):
                if collection[index].name in deleted:
                    collection.remove(index)

        # Shared mesh data can be referenced by several objects and scenes.
        owner_names = {
            candidate.name for candidate in bpy.data.objects
            if get_shape_key_data(candidate) == key_data
        }
        for scene in bpy.data.scenes:
            for preset in getattr(scene, "skv_global_presets", ()):
                changed = False
                for index in range(len(preset.items) - 1, -1, -1):
                    item = preset.items[index]
                    item_obj = getattr(item, "object_ref", None)
                    if (
                            (
                                    item_obj is not None
                                    and item_obj.name in owner_names
                            )
                            or (
                                    item_obj is None
                                    and item.object_name in owner_names
                            )
                    ) and item.key_name in deleted:
                        preset.items.remove(index)
                        changed = True
                if changed:
                    preset.items_index = -1

        props = context.scene.skv_props
        if props.sculpt_shape_key_name in deleted:
            props.shape_key_sculpting = False
            props.sculpt_shape_key_name = ""

        # Keep the active key when it survives; otherwise use a nearby key.
        if active_name in deleted or key_data.key_blocks.get(active_name) is None:
            active_name = next(
                (
                    kb.name for kb in key_data.key_blocks
                    if kb.name != basis_name
                    and kd_get_group(key_data, kb.name) == group_name
                ),
                basis_name,
            )

        group_index = key_data.skv_group_index
        skv_sync_shape_key_list_indices(
            context, obj, active_name, set_blender_active=True,
        )
        key_data.skv_group_index = group_index

        tag_redraw_view3d(context)
        self.report({"INFO"}, f"Deleted Shape Keys: {len(deleted)}")
        return {"FINISHED"}


class SKV_OT_GroupAdd(Operator):
    bl_idname = "skv.group_add"
    bl_label = "Add Group"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Name", default="")

    def invoke(self, context, event):
        self.name = "New Group"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "No supported active object.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        new_name = _make_unique_name(self.name, group_names(key_data))
        if not new_name:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        prev_idx = int(key_data.skv_group_index)

        g = key_data.skv_groups.add()
        g.name = new_name
        g.last_name = new_name

        if 0 <= prev_idx < len(key_data.skv_groups):
            key_data.skv_group_index = prev_idx

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GroupRemove(Operator):
    bl_idname = "skv.group_remove"
    bl_label = "Remove Group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            self.report({"WARNING"}, "No supported active object.")
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        idx = int(key_data.skv_group_index)
        if not (0 <= idx < len(key_data.skv_groups)):
            return {"CANCELLED"}

        if len(key_data.skv_groups) <= 1:
            self.report({"WARNING"}, "Object must have at least one group.")
            return {"CANCELLED"}

        removed_name = key_data.skv_groups[idx].name

        fallback_name = ""
        for i, g in enumerate(key_data.skv_groups):
            if i != idx:
                fallback_name = g.name
                break

        fallback_name = fallback_name or get_fallback_group_name(key_data)

        for kb in key_data.key_blocks:
            if kd_get_group(key_data, kb.name) == removed_name:
                kd_set_group(key_data, kb.name, fallback_name)

        key_data.skv_groups.remove(idx)

        if key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = max(0, len(key_data.skv_groups) - 1)

        try:
            if hasattr(key_data, "skv_last_group_index"):
                key_data.skv_last_group_index = int(key_data.skv_group_index)
        except Exception:
            pass

        ensure_init_setup_write(obj)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_GroupMove(Operator):
    bl_idname = "skv.group_move"
    bl_label = "Move Group"
    bl_options = {"REGISTER", "UNDO"}

    direction: StringProperty(name="Direction", default="UP")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None
        return bool(
            obj
            and key_data
            and has_group_storage(key_data)
            and getattr(key_data, "skv_groups", None)
            and len(key_data.skv_groups) > 1
        )

    def execute(self, context):
        obj = get_active_object(context)
        key_data = get_shape_key_data(obj) if obj else None

        if not obj or not key_data or not has_group_storage(key_data):
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        groups = key_data.skv_groups
        idx = int(getattr(key_data, "skv_group_index", 0))

        if not (0 <= idx < len(groups)):
            return {"CANCELLED"}

        if self.direction == "UP":
            new_idx = idx - 1
        elif self.direction == "DOWN":
            new_idx = idx + 1
        else:
            return {"CANCELLED"}

        if not (0 <= new_idx < len(groups)):
            return {"CANCELLED"}

        try:
            groups.move(idx, new_idx)
            key_data.skv_group_index = new_idx
        except Exception:
            return {"CANCELLED"}

        try:
            if hasattr(key_data, "skv_last_group_index"):
                key_data.skv_last_group_index = new_idx
        except Exception:
            pass

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_CreateGroupFromSelected(Operator):
    bl_idname = "skv.create_group_from_selected"
    bl_label = "Create Group From Selected"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Group Name", default="New Group")

    def invoke(self, context, event):
        self.name = "New Group"
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        key_data = get_shape_key_data(obj)
        if not key_data or not has_group_storage(key_data) or not key_data.key_blocks:
            return {"CANCELLED"}

        if getattr(key_data, "library", None) is not None:
            self.report({"ERROR"}, "Shape key datablock is linked (read-only).")
            return {"CANCELLED"}

        if not is_initialized(key_data):
            self.report({"INFO"}, "Not initialized.")
            return {"CANCELLED"}

        ensure_init_setup_write(obj)

        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )
        if not selected:
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        new_name = _make_unique_name(self.name, group_names(key_data))
        if not new_name:
            self.report({"WARNING"}, "Group name is empty.")
            return {"CANCELLED"}

        prev_idx = int(key_data.skv_group_index)

        g = key_data.skv_groups.add()
        g.name = new_name
        g.last_name = new_name

        if 0 <= prev_idx < len(key_data.skv_groups):
            key_data.skv_group_index = prev_idx

        moved = 0
        for kb in key_data.key_blocks:
            if kb.name in selected:
                kd_set_group(key_data, kb.name, new_name)
                moved += 1

        if moved == 0:
            for i, gg in enumerate(key_data.skv_groups):
                if gg.name == new_name:
                    key_data.skv_groups.remove(i)
                    break
            self.report({"INFO"}, "No selected shape keys.")
            return {"CANCELLED"}

        clear_selection_ui(context, key_data)
        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_TransferTo(TransferModalMixin, Operator):
    bl_idname = "skv.transfer_to"
    bl_label = "Transfer to"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    def invoke(self, context, event):
        obj = get_active_object(context)
        if not obj:
            return {"CANCELLED"}

        context.scene.skv_transfer_source_name = obj.name

        tgt = getattr(context.scene, "skv_transfer_target", None)
        if (
            not tgt
            or getattr(tgt, "type", None) != "MESH"
            or tgt.name == obj.name
        ):
            context.scene.skv_transfer_target = None

        return context.window_manager.invoke_props_dialog(
            self,
            width=320,
        )

    def draw(self, context):
        layout = self.layout

        layout.prop(
            context.scene,
            "skv_transfer_target",
            text="Target",
        )
        layout.prop(
            context.scene.skv_props,
            "transfer_inheritance",
            text="Inherit presets",
        )
        layout.prop(
            context.scene.skv_props,
            "transfer_keyframes_inheritance",
            text="Inherit keyframes",
        )

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        if not obj:
            return False

        key_data = get_shape_key_data(obj)
        if not key_data or not getattr(key_data, "key_blocks", None):
            return False

        selected = kd_visible_selected_names(
            key_data,
            context.scene.skv_props,
        )
        return any(not _is_basis_name(key_data, name) for name in selected)

    def execute(self, context):
        from .transfer import Transfer

        source = get_active_object(context)
        if not source:
            return {"CANCELLED"}

        target = getattr(
            context.scene,
            "skv_transfer_target",
            None,
        )
        if not target or getattr(target, "type", None) != "MESH":
            self.report({"ERROR"}, "Target mesh is not set")
            return {"CANCELLED"}

        if target.name == source.name:
            self.report(
                {"ERROR"},
                "Target must be different from source",
            )
            return {"CANCELLED"}

        key_data = get_shape_key_data(source)
        selected_names = [
            name
            for name in kd_visible_selected_names(
                key_data,
                context.scene.skv_props,
            )
            if not _is_basis_name(key_data, name)
        ]

        if not selected_names:
            self.report({"ERROR"}, "No selected Shape Keys")
            return {"CANCELLED"}

        self._job_inherit_presets = bool(
            context.scene.skv_props.transfer_inheritance
        )
        self._job_inherit_keyframes = bool(
            context.scene.skv_props.transfer_keyframes_inheritance
        )

        transfer_data = Transfer(
            source=source,
            target=target,
            vertex_group=None,
        )

        return self._transfer_begin(
            context,
            transfer_data,
            selected_names,
        )

    def _finish_transfer(self, context):
        from . import presets

        source = self._job_source
        target = self._job_target
        selected_names = self._job_names

        key_data = get_shape_key_data(source)
        target_key_data = get_shape_key_data(target)

        if target_key_data and getattr(
            target_key_data,
            "key_blocks",
            None,
        ):
            for key_name in selected_names:
                src_kb = key_data.key_blocks.get(key_name)
                dst_kb = target_key_data.key_blocks.get(key_name)

                if not src_kb or not dst_kb:
                    continue

                try:
                    dst_kb.value = float(src_kb.value)
                except Exception:
                    pass

        inherit_transferred_groups(
            source,
            target,
            selected_names,
        )

        if self._job_inherit_presets:
            presets.inherit_transferred_keys_to_presets(
                source,
                target,
                selected_names,
            )

        if self._job_inherit_keyframes:
            inherit_transferred_keyframes(
                source,
                target,
                selected_names,
            )

        clear_selection_ui(context, key_data)

        view_layer = context.view_layer

        try:
            for ob in list(view_layer.objects):
                if ob.select_get():
                    ob.select_set(False)
        except Exception:
            pass

        try:
            target.select_set(True)
        except Exception:
            pass

        _activate_target_if_available(context, target)

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_TransferFrom(TransferModalMixin, Operator):
    bl_idname = "skv.transfer_from"
    bl_label = "Transfer from"
    bl_options = {"REGISTER", "UNDO", "BLOCKING"}

    def invoke(self, context, event):
        target = get_active_object(context)
        if not target:
            return {"CANCELLED"}

        context.scene.skv_transfer_source_name = target.name

        source = getattr(context.scene, "skv_transfer_target", None)
        if not source or getattr(source, "type", None) != "MESH" or source.name == target.name:
            context.scene.skv_transfer_target = None

        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        layout = self.layout

        target = get_active_object(context)
        target_key_data = get_shape_key_data(target) if target else None
        target_has_shape_keys = bool(
            target_key_data
            and getattr(target_key_data, "key_blocks", None)
            and bool(list(target_key_data.key_blocks)[1:])
        )

        if target_has_shape_keys:
            box = layout.box()
            box.label(text="Warning: matching Shape Keys on target will be replaced.", icon="ERROR")

        layout.prop(context.scene, "skv_transfer_target", text="Source")
        layout.prop(context.scene.skv_props, "transfer_inheritance", text="Inherit presets")
        layout.prop(context.scene.skv_props, "transfer_keyframes_inheritance", text="Inherit keyframes")

    @classmethod
    def poll(cls, context):
        obj = get_active_object(context)
        return bool(obj and getattr(obj, "type", None) == "MESH")

    def execute(self, context):
        from .transfer import Transfer

        target = get_active_object(context)
        if not target:
            return {"CANCELLED"}

        source = getattr(context.scene, "skv_transfer_target", None)
        if not source or getattr(source, "type", None) != "MESH":
            self.report({"ERROR"}, "Source mesh is not set")
            return {"CANCELLED"}

        if source.name == target.name:
            self.report({"ERROR"}, "Source must be different from target")
            return {"CANCELLED"}

        source_key_data = get_shape_key_data(source)
        if not source_key_data or not getattr(source_key_data, "key_blocks", None):
            self.report({"ERROR"}, "Source has no Shape Keys")
            return {"CANCELLED"}

        selected_names = [
            kb.name
            for kb in source_key_data.key_blocks[1:]
        ]

        if not selected_names:
            self.report({"ERROR"}, "Source has no transferable Shape Keys")
            return {"CANCELLED"}

        self._job_inherit_presets = bool(
            context.scene.skv_props.transfer_inheritance
        )
        self._job_inherit_keyframes = bool(
            context.scene.skv_props.transfer_keyframes_inheritance
        )

        transfer_data = Transfer(
            source=source,
            target=target,
            vertex_group=None,
        )
        return self._transfer_begin(
            context,
            transfer_data,
            selected_names,
        )

    def _finish_transfer(self, context):
        from . import presets

        source = self._job_source
        target = self._job_target
        selected_names = self._job_names

        key_data = get_shape_key_data(source)
        source_key_data = key_data

        target_key_data = get_shape_key_data(target)
        if target_key_data and getattr(target_key_data, "key_blocks", None):
            for key_name in selected_names:
                    src_kb = source_key_data.key_blocks.get(key_name)
                    dst_kb = target_key_data.key_blocks.get(key_name)
                    if not src_kb or not dst_kb:
                        continue
                    try:
                        dst_kb.value = float(src_kb.value)
                    except Exception:
                        pass

        inherit_transferred_groups(source, target, selected_names)

        if self._job_inherit_presets:
            presets.inherit_transferred_keys_to_presets(source, target, selected_names)

        if self._job_inherit_keyframes:
            inherit_transferred_keyframes(source, target, selected_names)

        clear_selection_ui(context, source_key_data)

        view_layer = context.view_layer
        try:
            for ob in list(view_layer.objects):
                if ob.select_get():
                    ob.select_set(False)
        except Exception:
            pass

        try:
            target.select_set(True)
        except Exception:
            pass

        _activate_target_if_available(context, target)

        tag_redraw_view3d(context)
        return {"FINISHED"}

class SKV_OT_FavoriteToggle(Operator):
    bl_idname = "skv.favorite_toggle"
    bl_label = "Add / Remove Favorite"
    bl_description = "Add this shape key to Favorites or remove it"
    bl_options = {"REGISTER", "UNDO"}

    key_name: StringProperty(name="Key Name", default="")

    def invoke(self, context, event):
        data = get_shape_key_data(get_active_object(context))

        if not data or not data.key_blocks or data.library is not None:
            return {"CANCELLED"}

        if self.key_name in kd_favorite_names(data):
            return context.window_manager.invoke_props_dialog(
                self,
                width=360,
            )

        return self.execute(context)

    def draw(self, context):
        self.layout.label(
            text="Remove this shape key from Favorites?",
            icon="QUESTION",
        )
        self.layout.label(text=self.key_name)

    def execute(self, context):
        data = get_shape_key_data(get_active_object(context))

        if not data or not data.key_blocks or data.library is not None:
            return {"CANCELLED"}

        kb = data.key_blocks.get(self.key_name)
        if kb is None or kb == data.key_blocks[0]:
            return {"CANCELLED"}

        entries = data.skv_favorites
        indices = [
            index
            for index, item in enumerate(entries)
            if item.name == kb.name
        ]

        if indices:
            for index in reversed(indices):
                entries.remove(index)

            if data.skv_selection_source == "FAVORITES":
                kd_set_selected(data, kb.name, False)
        else:
            entries.add().name = kb.name

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_FavoritesClear(Operator):
    bl_idname = "skv.favorites_clear"
    bl_label = "Clear Favorites"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(
            self,
            width=340,
        )

    def draw(self, context):
        self.layout.label(
            text="Remove all entries from Favorites?",
            icon="QUESTION",
        )

    def execute(self, context):
        data = get_shape_key_data(get_active_object(context))

        if not data or data.library is not None:
            return {"CANCELLED"}

        data.skv_favorites.clear()

        if data.skv_selection_source == "FAVORITES":
            kd_clear_selected(data)

        context.scene.skv_props.favorites_index = -1

        tag_redraw_view3d(context)
        return {"FINISHED"}


class SKV_OT_FavoritesSearchClear(Operator):
    bl_idname = "skv.favorites_search_clear"
    bl_label = "Clear Favorites Search"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.skv_props
        props.favorites_search = ""
        props.favorites_index = -1

        tag_redraw_view3d(context)
        return {"FINISHED"}

class SKV_UL_keyframed_keys(UIList):
    bl_idname = "SKV_UL_keyframed_keys"

    def filter_items(self, context, data, propname):
        return skv_shape_key_filter_flags(
            self,
            context,
            data,
            propname,
        )

    def draw_item(
        self, context, layout, data, item, icon,
        active_data, active_propname, index, flt_flag=0,
    ):
        SKV_UL_key_blocks.draw_item(
            self,
            context,
            layout,
            data,
            item,
            icon,
            active_data,
            active_propname,
            index,
            flt_flag,
        )


class SKV_OT_KeyframedSelectVisible(Operator):
    bl_idname = "skv.keyframed_select_visible"
    bl_label = "Select Keyframed Shape Keys"
    bl_options = {"REGISTER", "UNDO"}

    mode: StringProperty(name="Mode", default="ALL")

    def execute(self, context):
        return _select_visible_from_list(
            self,
            context,
            "KEYFRAMED",
        )


class SKV_OT_KeyframedSearchClear(Operator):
    bl_idname = "skv.keyframed_search_clear"
    bl_label = "Clear Keyframed Search"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.skv_props
        props.keyframed_search = ""
        props.keyframed_index = -1

        tag_redraw_view3d(context)
        return {"FINISHED"}


CLASSES = (
    SKV_Group,
    SKV_SelectedName,
    SKV_KeyGroupEntry,
    SKV_AutoKeyframeEntry,
    SKV_UL_Groups,
    SKV_UL_key_blocks,
    SKV_UL_favorites,
    SKV_OT_FavoriteToggle,
    SKV_OT_FavoritesClear,
    SKV_OT_FavoritesSearchClear,
    SKV_MT_MoveToGroup,
    SKV_OT_GroupToggleVisibility,
    SKV_OT_ShapeKeyToggleVisibility,
    SKV_OT_AutoKeyframeToggle,
    SKV_OT_KeyToggleSelect,
    SKV_OT_SelectVisible,
    SKV_OT_FavoritesSelectVisible,
    SKV_OT_MoveSelectedToGroup,
    SKV_OT_ResetGroupValues,
    SKV_OT_DeleteSelectedShapeKeys,
    SKV_OT_GroupAdd,
    SKV_OT_GroupRemove,
    SKV_OT_GroupMove,
    SKV_OT_CreateGroupFromSelected,
    SKV_OT_TransferTo,
    SKV_OT_TransferFrom,
    SKV_UL_keyframed_keys,
    SKV_OT_KeyframedSelectVisible,
    SKV_OT_KeyframedSearchClear,
)