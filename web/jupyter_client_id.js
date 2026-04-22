import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// ワークフロー実行対象外の仮想ノード。自分のブラウザの sid (api.clientId) を
// widget に表示し、選択してコピーできるようにする。
// Python 側の tools.queue_prompt(sid=...) で対象ブラウザを指定する際の参照用。
app.registerExtension({
    name: "jupyter.client_id",
    registerCustomNodes() {
        class JupyterClientIdNode extends LGraphNode {
            constructor(title) {
                super(title);
                this.isVirtualNode = true;
                this.serialize_widgets = false;

                const widget = this.addWidget(
                    "text",
                    "sid",
                    api.clientId ?? "",
                    null,
                    {},
                );
                // api.clientId は初回の status WS メッセージで確定するため、
                // 以降の status 受信でも最新値に追従する
                api.addEventListener("status", () => {
                    widget.value = api.clientId ?? "";
                    this.setDirtyCanvas(true, false);
                });
            }
        }

        JupyterClientIdNode.title = "Jupyter Client ID";
        JupyterClientIdNode.category = "comfyui-jupyter";

        LiteGraph.registerNodeType("Jupyter Client ID", JupyterClientIdNode);
    },
});
