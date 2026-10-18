import re

INIT_GROUP_NAME = "Main"


# Utilities
def get_active_object(context):
    obj = context.active_object
    if not obj:
        return None
    data = getattr(obj, "data", None)
    if data is None:
        return None
    if not hasattr(data, "shape_keys"):
        return None
    return obj


def get_shape_key_data(obj):
    if obj is None:
        return None
    data = getattr(obj, "data", None)
    if data is None:
        return None
    if not hasattr(data, "shape_keys"):
        return None
    return data.shape_keys  # bpy.types.Key or None


def has_group_storage(key_data) -> bool:
    return bool(key_data) and hasattr(key_data, "skv_groups") and hasattr(key_data, "skv_group_index")


def group_names(key_data):
    if not has_group_storage(key_data):
        return []
    return [g.name for g in key_data.skv_groups]


def get_fallback_group_name(key_data) -> str:
    if has_group_storage(key_data) and getattr(key_data, "skv_groups", None) and len(key_data.skv_groups) > 0:
        name = (key_data.skv_groups[0].name or "").strip()
        return name or INIT_GROUP_NAME
    return INIT_GROUP_NAME


def is_initialized(key_data) -> bool:
    """Return True when addon storage exists and at least one group is present."""
    if not has_group_storage(key_data):
        return False
    return bool(getattr(key_data, "skv_groups", None)) and len(key_data.skv_groups) > 0


def cleanup_legacy_init_group(key_data) -> None:
    if not has_group_storage(key_data) or not getattr(key_data, "skv_groups", None):
        return
    # Do not modify linked data.
    if getattr(key_data, "library", None) is not None:
        return

    init_indices = [i for i, g in enumerate(key_data.skv_groups) if g.name == "Init"]
    if not init_indices:
        if getattr(key_data, "key_blocks", None):
            for kb in key_data.key_blocks:
                try:
                    if "skv_group" in kb:
                        del kb["skv_group"]
                except Exception:
                    pass
        return

    main_indices = [i for i, g in enumerate(key_data.skv_groups) if g.name == INIT_GROUP_NAME]

    # Remap mapping entries first.
    if hasattr(key_data, "skv_key_groups"):
        for it in key_data.skv_key_groups:
            if getattr(it, "group", "") == "Init":
                it.group = INIT_GROUP_NAME

    if main_indices:
        for i in reversed(init_indices):
            try:
                key_data.skv_groups.remove(i)
            except Exception:
                pass
    else:
        try:
            key_data.skv_groups[init_indices[0]].name = INIT_GROUP_NAME
        except Exception:
            pass
        for i in reversed(init_indices[1:]):
            try:
                key_data.skv_groups.remove(i)
            except Exception:
                pass

    try:
        if key_data.skv_group_index < 0 or key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = 0
    except Exception:
        pass

    if getattr(key_data, "key_blocks", None):
        for kb in key_data.key_blocks:
            try:
                if "skv_group" in kb:
                    del kb["skv_group"]
            except Exception:
                pass


def get_selected_group_name(key_data):
    if not has_group_storage(key_data) or not key_data.skv_groups:
        return INIT_GROUP_NAME

    idx = int(getattr(key_data, "skv_group_index", 0))
    if 0 <= idx < len(key_data.skv_groups):
        name = (key_data.skv_groups[idx].name or "").strip()
        return name if name else get_fallback_group_name(key_data)

    return get_fallback_group_name(key_data)


def enum_groups_for_active_object(self, context):
    obj = get_active_object(context)
    key_data = get_shape_key_data(obj) if obj else None
    if not key_data or not has_group_storage(key_data) or not key_data.skv_groups:
        return [(INIT_GROUP_NAME, INIT_GROUP_NAME, "Not initialized")]
    return [(g.name, g.name, "") for g in key_data.skv_groups if g.name]


def tag_redraw_view3d(context):
    scr = getattr(context, "screen", None)
    if not scr:
        return
    for area in scr.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


def parse_tokens(text: str):
    if not text:
        return []
    parts = re.split(r"[;,]+", text)
    out = []
    for p in parts:
        t = p.strip()
        if t:
            out.append(t)
    return out


def clear_selection_ui(context, key_data):
    kd_clear_selected(key_data)


def kd_get_group(key_data, kb_name: str) -> str:
    fallback = get_fallback_group_name(key_data)
    entries = getattr(key_data, "skv_key_groups", None)

    if entries is None or not kb_name:
        return fallback

    item = entries.get(kb_name)
    if item is None:
        return fallback

    return (item.group or "").strip() or fallback


def kd_set_group(key_data, kb_name: str, group_name: str) -> None:
    entries = getattr(key_data, "skv_key_groups", None)
    if entries is None or not kb_name:
        return

    group_name = (
        (group_name or "").strip()
        or get_fallback_group_name(key_data)
    )

    item = entries.get(kb_name)
    if item is None:
        item = entries.add()
        item.name = kb_name

    item.group = group_name


def kd_is_selected(key_data, kb_name: str) -> bool:
    entries = getattr(key_data, "skv_selected", None)
    return bool(
        kb_name
        and entries is not None
        and entries.get(kb_name) is not None
    )

def kd_prune_group_map(key_data, valid_names) -> None:
    if not key_data or not hasattr(key_data, "skv_key_groups"):
        return
    i = 0
    while i < len(key_data.skv_key_groups):
        if key_data.skv_key_groups[i].name not in valid_names:
            key_data.skv_key_groups.remove(i)
        else:
            i += 1


def kd_selected_set(key_data):
    if not key_data or not hasattr(key_data, "skv_selected"):
        return set()

    names = {
        item.name
        for item in key_data.skv_selected
        if item.name
    }

    source = getattr(key_data, "skv_selection_source", "GROUP")

    if source == "KEYFRAMED":
        names &= kd_keyframed_names(key_data)

    return names


def kd_set_selected(key_data, kb_name: str, state: bool) -> None:
    if not kb_name or not key_data or not hasattr(key_data, "skv_selected"):
        return

    if state:
        if kd_is_selected(key_data, kb_name):
            return
        it = key_data.skv_selected.add()
        it.name = kb_name
    else:
        for i, it in enumerate(key_data.skv_selected):
            if it.name == kb_name:
                key_data.skv_selected.remove(i)
                return


def kd_clear_selected(key_data) -> None:
    if not key_data or not hasattr(key_data, "skv_selected"):
        return
    key_data.skv_selected.clear()


def kd_group_map(key_data):
    fallback = get_fallback_group_name(key_data)
    result = {}
    for item in getattr(key_data, "skv_key_groups", ()):
        result.setdefault(
            item.name,
            (item.group or "").strip() or fallback,
        )
    return result


def kd_group_counts(key_data):
    if not key_data or not getattr(key_data, "key_blocks", None):
        return {}

    mapping = kd_group_map(key_data)
    fallback = get_fallback_group_name(key_data)
    counts = {}

    for kb in key_data.key_blocks:
        if kb.name == "Basis":
            continue
        name = mapping.get(kb.name, fallback)
        counts[name] = counts.get(name, 0) + 1

    return counts


def kd_autokf_get_entry(key_data, key_name: str, create: bool = False):
    entries = getattr(key_data, "skv_auto_keyframes", None)
    if entries is None or not key_name:
        return None

    item = entries.get(key_name)
    if item is None and create:
        item = entries.add()
        item.name = key_name

    return item


def count_keys_in_group(key_data, group_name: str) -> int:
    return kd_group_counts(key_data).get(group_name, 0)


def count_selected_in_group(key_data, group_name, search):
    if not key_data or not key_data.key_blocks:
        return 0

    if key_data.skv_selection_source != "GROUP":
        return 0

    tokens = [token.lower() for token in parse_tokens(search)]
    selected = kd_selected_set(key_data)

    mapping = kd_group_map(key_data)
    fallback = get_fallback_group_name(key_data)

    return sum(
        kb.name in selected
        for kb in key_data.key_blocks[1:]
        if mapping.get(kb.name, fallback) == group_name
        and all(token in kb.name.lower() for token in tokens)
    )

def ensure_init_setup_write(obj):
    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return
    if not has_group_storage(key_data):
        return
    if getattr(key_data, "library", None) is not None:
        return

    cleanup_legacy_init_group(key_data)

    if len(key_data.skv_groups) == 0:
        g = key_data.skv_groups.add()
        g.name = INIT_GROUP_NAME
        try:
            g.last_name = INIT_GROUP_NAME
        except Exception:
            pass

    try:
        if key_data.skv_group_index < 0 or key_data.skv_group_index >= len(key_data.skv_groups):
            key_data.skv_group_index = 0
    except Exception:
        pass

    names = group_names(key_data)
    fallback_group = get_fallback_group_name(key_data)

    valid_kb_names = {kb.name for kb in key_data.key_blocks}
    kd_prune_group_map(key_data, valid_kb_names)

    for kb in key_data.key_blocks:
        cur = kd_get_group(key_data, kb.name)
        if cur not in names:
            kd_set_group(key_data, kb.name, fallback_group)

    for kb in key_data.key_blocks:
        try:
            if "skv_group" in kb:
                del kb["skv_group"]
        except Exception:
            pass


def _is_basis_name(key_data, name: str) -> bool:
    try:
        if not key_data or not key_data.key_blocks:
            return False
        return key_data.key_blocks[0].name == name
    except Exception:
        return name == "Basis"


_SKV_SHAPE_KEY_LIST_SYNC_GUARD = False


def skv_shape_key_list_sync_active() -> bool:
    return _SKV_SHAPE_KEY_LIST_SYNC_GUARD


def skv_find_shape_key_index(key_data, key_name: str) -> int:
    if not key_data or not getattr(key_data, "key_blocks", None) or not key_name:
        return -1

    for i, kb in enumerate(key_data.key_blocks):
        if kb.name == key_name:
            return i

    return -1


def skv_get_active_shape_key_name(obj) -> str:
    if not obj or getattr(obj, "type", None) != "MESH":
        return ""

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return ""

    idx = int(getattr(obj, "active_shape_key_index", -1))
    if 0 <= idx < len(key_data.key_blocks):
        return key_data.key_blocks[idx].name

    return ""


def skv_set_active_shape_key_by_name(obj, key_name: str) -> bool:
    if not obj or getattr(obj, "type", None) != "MESH" or not key_name:
        return False

    key_data = get_shape_key_data(obj)
    idx = skv_find_shape_key_index(key_data, key_name)

    if idx < 0:
        return False

    try:
        obj.active_shape_key_index = idx
        return True
    except Exception:
        return False


def skv_sync_shape_key_list_indices(context, obj, key_name: str = "", set_blender_active: bool = True) -> None:
    """
    Synchronize active rows of addon UILists from one active shape key name.

    This synchronizes:
    - Shape Keys in group
    - Favorites
    - Keyframed Shape Keys
    - Shape Keys in active preset
    """
    global _SKV_SHAPE_KEY_LIST_SYNC_GUARD

    if _SKV_SHAPE_KEY_LIST_SYNC_GUARD:
        return

    if not context or not obj or getattr(obj, "type", None) != "MESH":
        return

    scene = getattr(context, "scene", None)
    props = getattr(scene, "skv_props", None) if scene else None
    if not props:
        return

    key_data = get_shape_key_data(obj)
    if not key_data or not getattr(key_data, "key_blocks", None):
        return

    if not key_name:
        key_name = skv_get_active_shape_key_name(obj)

    if not key_name or skv_find_shape_key_index(key_data, key_name) < 0:
        if getattr(key_data, "key_blocks", None):
            key_name = key_data.key_blocks[0].name
        else:
            return

    key_index = skv_find_shape_key_index(key_data, key_name)
    if key_index < 0:
        return

    _SKV_SHAPE_KEY_LIST_SYNC_GUARD = True
    try:
        if set_blender_active or int(getattr(obj, "active_shape_key_index", -1)) < 0:
            skv_set_active_shape_key_by_name(obj, key_name)

        try:
            key_group = kd_get_group(key_data, key_name)
            if hasattr(key_data, "skv_groups") and key_group:
                for group_index, group in enumerate(key_data.skv_groups):
                    if group.name == key_group:
                        key_data.skv_group_index = group_index
                        break
        except Exception:
            pass

        try:
            props.keys_index = key_index
        except Exception:
            pass

        props.favorites_index = (
            key_index
            if key_name in kd_favorite_names(key_data)
            else -1
        )

        try:
            props.keyframed_index = (
                key_index
                if key_name in kd_keyframed_names(key_data)
                else -1
            )
        except Exception:
            pass

        try:
            if hasattr(scene, "skv_global_presets") and hasattr(scene, "skv_global_preset_index"):
                preset_index = int(scene.skv_global_preset_index)
                if 0 <= preset_index < len(scene.skv_global_presets):
                    gpreset = scene.skv_global_presets[preset_index]
                    item_index = -1
                    for i, it in enumerate(gpreset.items):
                        if it.object_name == obj.name and it.key_name == key_name:
                            item_index = i
                            break
                    gpreset.items_index = item_index
        except Exception:
            pass

        tag_redraw_view3d(context)
    finally:
        _SKV_SHAPE_KEY_LIST_SYNC_GUARD = False

def kd_favorite_names(key_data):
    if not key_data or not getattr(key_data, "key_blocks", None):
        return set()

    valid = {kb.name for kb in key_data.key_blocks[1:]}
    return {
        item.name
        for item in getattr(key_data, "skv_favorites", ())
        if item.name in valid
    }


def kd_prune_favorites(key_data):
    if not key_data or key_data.library is not None:
        return

    entries = getattr(key_data, "skv_favorites", None)
    if entries is None:
        return

    valid = kd_favorite_names(key_data)

    for index in range(len(entries) - 1, -1, -1):
        name = entries[index].name
        if name not in valid:
            if key_data.skv_selection_source == "FAVORITES":
                kd_set_selected(key_data, name, False)
            entries.remove(index)


def skv_list_key_names(
    key_data,
    props,
    source="GROUP",
    use_search=True,
):
    if not key_data or not getattr(key_data, "key_blocks", None):
        return []

    if source == "GROUP":
        group = get_selected_group_name(key_data)
        mapping = kd_group_map(key_data)
        fallback = get_fallback_group_name(key_data)

        members = {
            kb.name
            for kb in key_data.key_blocks[1:]
            if mapping.get(kb.name, fallback) == group
        }
        search = props.search

    elif source == "FAVORITES":
        members = kd_favorite_names(key_data)
        search = props.favorites_search

    elif source == "KEYFRAMED":
        members = kd_keyframed_names(key_data)
        search = props.keyframed_search

    else:
        return []

    tokens = (
        [token.lower() for token in parse_tokens(search)]
        if use_search
        else []
    )

    return [
        kb.name
        for kb in key_data.key_blocks[1:]
        if kb.name in members
        and all(token in kb.name.lower() for token in tokens)
    ]


def kd_activate_selection_source(key_data, source):
    # Keep selection owned by exactly one list.
    if key_data.skv_selection_source != source:
        kd_clear_selected(key_data)
        key_data.skv_selection_source = source

def _skv_iter_action_bindings(anim):
    if anim.action is not None:
        yield anim.action, getattr(anim, "action_slot", None)

    def walk(strips):
        for strip in strips:
            action = getattr(strip, "action", None)

            if action is not None:
                yield action, getattr(strip, "action_slot", None)

            yield from walk(getattr(strip, "strips", ()))

    for track in anim.nla_tracks:
        yield from walk(track.strips)


def _skv_iter_bound_fcurves(action, slot):
    if getattr(action, "is_action_layered", False):
        # Never inspect another datablock's slot.
        if slot is None:
            return

        for layer in action.layers:
            for strip in layer.strips:
                channelbag = getattr(strip, "channelbag", None)
                if channelbag is None:
                    continue

                bag = channelbag(slot, ensure=False)
                if bag is not None:
                    yield from bag.fcurves
    else:
        yield from getattr(action, "fcurves", ())


def kd_keyframed_names(key_data):
    if not key_data or not getattr(key_data, "key_blocks", None):
        return set()

    keys = list(key_data.key_blocks)[1:]
    valid_names = {kb.name for kb in keys}

    # Enabled Auto Keyframe keeps a key listed without any keyframes.
    result = {
        item.name
        for item in getattr(key_data, "skv_auto_keyframes", ())
        if item.enabled and item.name in valid_names
    }

    anim = getattr(key_data, "animation_data", None)
    if anim is None:
        return result

    paths = {
        kb.path_from_id("value"): kb.name
        for kb in keys
    }
    seen = set()

    for action, slot in _skv_iter_action_bindings(anim):
        binding = (
            action.as_pointer(),
            slot.as_pointer() if slot is not None else 0,
        )
        if binding in seen:
            continue
        seen.add(binding)

        for fc in _skv_iter_bound_fcurves(action, slot):
            name = paths.get(fc.data_path)

            if name is not None and len(fc.keyframe_points) > 0:
                result.add(name)

    return result

SKV_ROW_SELECTED = 1 << 0
SKV_ROW_FAVORITE = 1 << 1
SKV_ROW_AUTO_KEYFRAME = 1 << 2


def skv_shape_key_filter_flags(ui_list, context, data, propname):
    if data is None:
        return [], []

    source = {
        "SKV_UL_favorites": "FAVORITES",
        "SKV_UL_keyframed_keys": "KEYFRAMED",
    }.get(ui_list.bl_idname, "GROUP")

    visible = set(
        skv_list_key_names(
            data,
            context.scene.skv_props,
            source,
        )
    )
    selected = (
        {
            item.name
            for item in getattr(data, "skv_selected", ())
        }
        if data.skv_selection_source == source
        else set()
    )
    favorites = {
        item.name
        for item in getattr(data, "skv_favorites", ())
    }
    enabled = {
        item.name
        for item in getattr(data, "skv_auto_keyframes", ())
        if item.enabled
    }

    flags = []
    for kb in getattr(data, propname):
        name = kb.name
        flag = (
            ui_list.bitflag_filter_item
            if name in visible else 0
        )

        if name in selected:
            flag |= SKV_ROW_SELECTED
        if name in favorites:
            flag |= SKV_ROW_FAVORITE
        if name in enabled:
            flag |= SKV_ROW_AUTO_KEYFRAME

        flags.append(flag)

    return flags, []