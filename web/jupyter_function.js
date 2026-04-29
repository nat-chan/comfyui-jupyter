import { app } from "../../scripts/app.js";

const NODE_TYPE = "JupyterFunction";
const SLOT_PREFIX = "arg_";
const NAME_PREFIX = "argname_";
const HEADER_NAME = "_kwargs_header";
const ANY_TYPE = "*";

// Inject the stylesheet exactly once. import.meta.url resolves to the URL of
// this very script, so the sibling `.css` is fetched from the same web dir
// regardless of where ComfyUI mounts the extension.
(() => {
    const href = new URL("./jupyter_function.css", import.meta.url).href;
    if (document.querySelector(`link[data-jupyter-function="1"]`)) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.dataset.jupyterFunction = "1";
    document.head.appendChild(link);
})();

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
// reflect the *positional* index (the count of un-named slots above), not the
// slot's internal index. Each paired widget's `label` is updated in lockstep
// so its visible numbering matches the socket. Modern ComfyUI reads
// `slot.localized_name` for sockets and `widget.label || widget.name` for
// widgets; internal `widget.name` (`argname_<slot_idx>`) stays stable for
// serialization.
function relabelAllSlots(node) {
    let positional = 0;
    for (const { slot, widget } of getArgPairs(node)) {
        const typed = (widget?.value ?? "").trim();
        let label;
        if (typed) {
            label = typed;
        } else {
            label = `args[${positional}]`;
            positional++;
        }
        slot.localized_name = label;
        if (widget) {
            widget.label = label;
            // DOM widget renders its own label (the Vue layout doesn't supply
            // one for `WidgetDOM`), so push the text into the inline span.
            widget._setLabelText?.(label);
        }
    }
}

// Custom DOM widget for the kwarg-name textbox. Two design points:
//   1. We render the label INSIDE the DOM element. The Vue node template only
//      adds a label row for built-in widgets (via `WidgetLayoutField`); for
//      DOM widgets it just mounts our element with no label, so we own that
//      ourselves.
//   2. The framework's `updateComputedDisabled` (overridden below) is what
//      grays the widget when the paired input slot connects — but only for
//      built-in / generic widgets. We pin our argname widgets to
//      `computedDisabled = false` so the textbox stays editable regardless of
//      slot link state.
function addArgNameWidget(node, idx) {
    const root = document.createElement("div");
    root.className = "comfy-jupyter-argname-root";

    const labelEl = document.createElement("span");
    labelEl.className = "comfy-jupyter-argname-label";
    root.appendChild(labelEl);

    const input = document.createElement("input");
    input.type = "text";
    input.className = "comfy-jupyter-argname-input";
    root.appendChild(input);

    let stored = "";
    const widget = node.addDOMWidget(
        `${NAME_PREFIX}${idx}`,
        "jupyter-argname",
        root,
        {
            getValue: () => stored,
            setValue: (v) => {
                stored = typeof v === "string" ? v : "";
                input.value = stored;
            },
            getMinHeight: () => 28,
            getMaxHeight: () => 28,
            hideOnZoom: false,
        },
    );
    // Setter the relabel pass calls to update the inline label text.
    widget._setLabelText = (text) => {
        labelEl.textContent = text;
    };
    widget.callback = () => {
        relabelAllSlots(node);
        node.setDirtyCanvas(true, true);
    };

    input.addEventListener("input", () => {
        stored = input.value;
        relabelAllSlots(node);
        node.setDirtyCanvas(true, true);
    });

    return widget;
}

// Pair the input slot with its argname widget via `widget: {name: ...}` so the
// Vue renderer (Nodes 2.0) hangs the socket dot off the widget row.
// `extractVueNodeData` populates `widget.slotMetadata` by looking up
// `input.widget.name` in the widget list, and `WidgetLayoutField` renders an
// inline socket dot whenever that metadata is set. The widget MUST already be
// in `node.widgets` when the input is added — otherwise the lookup fails and
// the socket falls back to the (empty) legacy slot column rendering.
function addArgInput(node, idx) {
    node.addInput(`${SLOT_PREFIX}${idx}`, ANY_TYPE, {
        widget: { name: `${NAME_PREFIX}${idx}` },
    });
    return node.inputs[node.inputs.length - 1];
}

function appendEmptyPair(node, idx) {
    addArgNameWidget(node, idx);
    addArgInput(node, idx);
}

// Read-only DOM widget that labels the two argname columns ("input" /
// "argument name (blank is positional)"). Inserted once per node, between the
// schema widgets and the first argname row. Marked `serialize: false` so it
// doesn't shift the `widgets_values` index on save/load.
function addKwargsHeader(node) {
    if (findWidget(node, HEADER_NAME)) return;

    const root = document.createElement("div");
    root.className = "comfy-jupyter-header-root";
    const left = document.createElement("span");
    left.className = "comfy-jupyter-header-cell";
    left.textContent = "input";
    const right = document.createElement("span");
    right.className = "comfy-jupyter-header-cell";
    right.textContent = "arg name, blank is positional";
    root.appendChild(left);
    root.appendChild(right);

    const widget = node.addDOMWidget(HEADER_NAME, "jupyter-kwargs-header", root, {
        getValue: () => "",
        setValue: () => {},
        getMinHeight: () => 18,
        getMaxHeight: () => 18,
        hideOnZoom: false,
    });
    widget.serialize = false;
    return widget;
}


function removeArgPair(node, pair) {
    const slotIdx = (node.inputs ?? []).indexOf(pair.slot);
    if (slotIdx >= 0) node.removeInput(slotIdx);
    if (pair.widget) {
        const widgetIdx = (node.widgets ?? []).indexOf(pair.widget);
        if (widgetIdx >= 0) node.widgets.splice(widgetIdx, 1);
        // Splicing alone leaves the DOM-widget element registered in the
        // legacy `DomWidgets` overlay (via `widgetStates`) and parented in the
        // DOM. The overlay then keeps rendering the stale element absolutely
        // — visible as a ghost row in canvas mode, and as the *un-updated*
        // label/value in Vue mode where it overlays the inline render.
        // `widget.onRemove()` unregisters from `widgetStates`; detaching the
        // element removes it from any parent container.
        pair.widget.onRemove?.();
        pair.widget.element?.remove?.();
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

    // Renumber survivors contiguously. Single-pass is safe because survivors
    // are walked in node.inputs array order and the new indices are 0..N-1
    // monotonically, so a target name (`arg_i`/`argname_i`) is always free by
    // the time we assign it. We also keep `slot.widget.name` in sync — without
    // it, the reactive widget<->input lookup in `extractVueNodeData` breaks
    // and the renamed widget retains a stale `slotMetadata.linked = true` from
    // refreshNodeSlots, leaving the textbox grayed out after a disconnect.
    const survivors = getArgPairs(node);
    survivors.forEach((s, i) => {
        s.slot.name = `${SLOT_PREFIX}${i}`;
        if (s.widget) s.widget.name = `${NAME_PREFIX}${i}`;
        if (s.slot.widget) s.slot.widget.name = `${NAME_PREFIX}${i}`;
    });

    // Ensure a trailing empty pair exists.
    const last = survivors[survivors.length - 1];
    if (!last || last.slot.link != null) {
        appendEmptyPair(node, survivors.length);
    }

    relabelAllSlots(node);

    // Force the Vue side to recompute `widget.slotMetadata` after our
    // structural mutations. The graph's `node:slot-links:changed` event is
    // what normally drives `refreshNodeSlots`, and its handler ignores
    // `slotIndex`, so any synthetic dispatch revalidates every widget on this
    // node. Without this, a widget renamed across the disconnect path keeps
    // the disabled state set during the previous "linked" phase.
    node.graph?.trigger?.("node:slot-links:changed", {
        nodeId: node.id,
        slotType: 1, // LiteGraph.INPUT
        slotIndex: 0,
        connected: false,
    });
}

// --- registration -----------------------------------------------------------

app.registerExtension({
    name: "jupyter.function",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_TYPE) return;

        // LiteGraph's `updateComputedDisabled` writes
        // `widget.computedDisabled = widget.disabled || (paired slot is linked)`
        // every draw cycle, and the legacy `DomWidget` overlay reads that and
        // applies opacity 0.5 + pointer-events: none. The kwarg-name textbox
        // must stay editable regardless of socket link state, so re-pin our
        // argname widgets to `computedDisabled = false` after the framework's
        // pass.
        const origUpdateComputedDisabled = nodeType.prototype.updateComputedDisabled;
        nodeType.prototype.updateComputedDisabled = function () {
            origUpdateComputedDisabled?.apply(this, arguments);
            for (const w of this.widgets ?? []) {
                if (typeof w.name === "string" && w.name.startsWith(NAME_PREFIX)) {
                    w.computedDisabled = false;
                }
            }
        };

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            addKwargsHeader(this);
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
        // default configure assigns widgets_values by index. The header widget
        // is `serialize: false` so it's transparent to the index counting.
        const origConfigure = nodeType.prototype.configure;
        nodeType.prototype.configure = function (data) {
            addKwargsHeader(this);
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
            // Restore widget association for arg sockets — `origConfigure`
            // splices saved input data into `this.inputs` and that data does
            // not carry `widget: {name: ...}` (we don't serialize it).
            for (const slot of this.inputs ?? []) {
                if (typeof slot.name !== "string") continue;
                if (!slot.name.startsWith(SLOT_PREFIX)) continue;
                const idx = parseInt(slot.name.slice(SLOT_PREFIX.length), 10);
                if (Number.isNaN(idx)) continue;
                if (!slot.widget) slot.widget = { name: `${NAME_PREFIX}${idx}` };
            }
            queueMicrotask(() => {
                normalizeArgs(this);
                this.setDirtyCanvas(true, true);
            });
            return result;
        };
    },
});
