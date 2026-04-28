import { app } from "../../scripts/app.js";

const NODE_TYPE = "JupyterFunction";
const SLOT_PREFIX = "arg_";
const NAME_PREFIX = "argname_";
const TMP_SLOT_PREFIX = "__t_arg_";
const TMP_NAME_PREFIX = "__t_argname_";
const ANY_TYPE = "*";
const NAME_PLACEHOLDER =
    "The name of the keyword argument (or the positional argument if left blank)";
const TAG = "[comfyui_jupyter:function]";

// --- helpers ----------------------------------------------------------------

function findWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function getArgPairs(node) {
    const out = [];
    for (const slot of node.inputs ?? []) {
        if (typeof slot.name !== "string") continue;
        if (!slot.name.startsWith(SLOT_PREFIX)) continue;
        const idx = parseInt(slot.name.slice(SLOT_PREFIX.length), 10);
        if (Number.isNaN(idx)) continue;
        const widget = findWidget(node, `${NAME_PREFIX}${idx}`);
        out.push({ slot, widget, oldIdx: idx, hasLink: slot.link != null });
    }
    return out;
}

// Recompute every arg slot's display label so the `args[k]` placeholders
// reflect the *positional* index (i.e. the count of un-named slots above), not
// the slot's internal index. Modern ComfyUI reads `localized_name` for socket
// rendering.
function relabelAllSlots(node) {
    let positional = 0;
    for (const { slot, widget } of getArgPairs(node)) {
        const typed = (widget?.value ?? "").trim();
        if (typed) {
            slot.localized_name = typed;
        } else {
            slot.localized_name = `args[${positional}]`;
            positional++;
        }
    }
}

function attachArgWidgetCallback(node, widget) {
    widget.callback = () => {
        relabelAllSlots(node);
        node.setDirtyCanvas(true, true);
    };
}

function addArgNameWidget(node, idx) {
    const widget = node.addWidget("text", `${NAME_PREFIX}${idx}`, "", () => {}, {});
    widget.options = widget.options || {};
    widget.options.placeholder = NAME_PLACEHOLDER;
    attachArgWidgetCallback(node, widget);
    return widget;
}

// Plain `addInput` keeps the socket in the legacy slot column. Widget
// association via `widget: {name: ...}` is unsupported for dynamic inputs in
// the current Vue node renderer.
function addArgInput(node, idx) {
    node.addInput(`${SLOT_PREFIX}${idx}`, ANY_TYPE);
    return node.inputs[node.inputs.length - 1];
}

function appendEmptyPair(node, idx) {
    addArgNameWidget(node, idx);
    addArgInput(node, idx);
}

function removeArgPair(node, pair) {
    const slotIdx = (node.inputs ?? []).indexOf(pair.slot);
    if (slotIdx >= 0) node.removeInput(slotIdx);
    if (pair.widget) {
        const widgetIdx = (node.widgets ?? []).indexOf(pair.widget);
        if (widgetIdx >= 0) node.widgets.splice(widgetIdx, 1);
    }
}

function normalizeArgs(node) {
    const pairs = getArgPairs(node);

    // Identify trailing empty (last unlinked) and remove other unlinked.
    let trailingPos = -1;
    for (let i = pairs.length - 1; i >= 0; i--) {
        if (!pairs[i].hasLink) {
            trailingPos = i;
            break;
        }
    }
    const toRemove = pairs.filter((p, i) => !p.hasLink && i !== trailingPos);
    for (const p of toRemove) removeArgPair(node, p);

    // Renumber survivors contiguously via a two-phase rename to avoid name
    // collisions while updating sockets and widgets.
    const survivors = getArgPairs(node);
    survivors.forEach((s, i) => {
        s.slot.name = `${TMP_SLOT_PREFIX}${i}`;
        if (s.widget) s.widget.name = `${TMP_NAME_PREFIX}${i}`;
    });
    survivors.forEach((s, i) => {
        s.slot.name = `${SLOT_PREFIX}${i}`;
        if (s.widget) s.widget.name = `${NAME_PREFIX}${i}`;
    });

    // Ensure a trailing empty pair exists.
    const last = survivors[survivors.length - 1];
    if (!last || last.slot.link != null) {
        appendEmptyPair(node, survivors.length);
    }

    relabelAllSlots(node);
}

// --- registration -----------------------------------------------------------

app.registerExtension({
    name: "jupyter.function",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            normalizeArgs(this);
        };

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (
            slotType,
            _slot,
            _isConnecting,
            _link,
            _ioSlot,
        ) {
            onConnectionsChange?.apply(this, arguments);
            // 1 = LiteGraph.INPUT
            if (slotType !== 1) return;
            queueMicrotask(() => {
                normalizeArgs(this);
                this.setDirtyCanvas(true, true);
            });
        };

        // Pre-add `argname_*` widgets to match saved arg sockets BEFORE the
        // default configure assigns widgets_values by index.
        const origConfigure = nodeType.prototype.configure;
        nodeType.prototype.configure = function (data) {
            for (const inp of data?.inputs ?? []) {
                if (typeof inp?.name !== "string") continue;
                if (!inp.name.startsWith(SLOT_PREFIX)) continue;
                const idx = parseInt(inp.name.slice(SLOT_PREFIX.length), 10);
                if (Number.isNaN(idx)) continue;
                if (!findWidget(this, `${NAME_PREFIX}${idx}`)) {
                    addArgNameWidget(this, idx);
                }
            }
            const result = origConfigure?.apply(this, arguments);
            queueMicrotask(() => {
                normalizeArgs(this);
                this.setDirtyCanvas(true, true);
            });
            return result;
        };
    },
});

console.log(TAG, "extension registered");
