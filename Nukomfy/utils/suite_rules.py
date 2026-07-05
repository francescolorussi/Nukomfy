"""Suite knob relation rules.

Single source of truth for cross-knob behaviours of the Nukomfy custom
nodes (NukomfyRead / NukomfyWrite / ...). Both the Workflow Editor
(client-side table) and the gizmo runtime (knobChanged callback) read
from here, so a new rule lands in one place and propagates everywhere.

Currently encodes:
  - SUITE_VISIBILITY_RULES: boolean trigger -> dependent widgets that
    grey out when the trigger is False. Used for
    apply_color_transform -> input_transform / output_transform on
    NukomfyRead / NukomfyWrite.
"""


# Schema: {node_type: {trigger_widget_name: [dependent_widget_names]}}
SUITE_VISIBILITY_RULES = {
    'NukomfyRead':  {'apply_color_transform': ['input_transform']},
    'NukomfyWrite': {'apply_color_transform': ['output_transform']},
}


def suite_dependents(node_type, trigger_widget_name):
    """Return the list of dependent widget_names for (node_type,
    trigger), or an empty list if no rule applies."""
    return SUITE_VISIBILITY_RULES.get(node_type, {}).get(
        trigger_widget_name, [])


def suite_group_widgets(node_type, widget_name):
    """Return the set of widget_names that share an EN-link group with
    `widget_name` on a node of type `node_type`, or an empty set if
    this widget is not part of any rule."""
    rules = SUITE_VISIBILITY_RULES.get(node_type, {})
    for trigger_wn, deps in rules.items():
        if widget_name == trigger_wn or widget_name in deps:
            return {trigger_wn} | set(deps)
    return set()


# COMFY_DYNAMICCOMBO_V3 masters whose sub-input rows must rebuild live
# in the Workflow Editor when the master combo value changes. This set
# names the Suite top-level masters explicitly; any other V3 master
# (Suite-nested or third-party generic) also rebuilds, detected
# structurally via _v3_is_dynamic_master by the editor cascade hook.
SUITE_V3_REBUILD_TRIGGERS = {
    'NukomfyWrite': {'file_type'},
}


def is_v3_rebuild_trigger(node_type, widget_name):
    """True when this widget is a Suite top-level V3 master that should
    rebuild its nested sub-rows live on combo change. Generic and
    Suite-nested V3 masters are handled separately by the editor via
    the _v3_is_dynamic_master flag."""
    return widget_name in SUITE_V3_REBUILD_TRIGGERS.get(node_type, set())
