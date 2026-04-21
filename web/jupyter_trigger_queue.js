import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

api.addEventListener("comfyui_jupyter/trigger_queue", async () => {
    await app.queuePrompt(0);
});

app.registerExtension({
    name: "jupyter.trigger_queue",
});
